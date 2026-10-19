from __future__ import annotations

import asyncio
import gc
import io
import os
from functools import partial
from io import BytesIO
from pathlib import Path
from typing import Union

try:
    from PIL import Image
except ModuleNotFoundError:  # pragma: no cover - PIL is a hard dependency
    from PIL import Image  # type: ignore

ImageSource = Union[str, "os.PathLike[str]", bytes, bytearray]


def load_image_rgba(source: ImageSource) -> "Image.Image":
    """Open an image and return an independent RGBA copy with the source released.

    Prefer this over ``Image.open(src).convert("RGBA")``: the latter discards the
    file-backed ``Image`` and only closes its OS file handle when the object is
    garbage collected, which under concurrency leaks file descriptors. Here the
    source is opened via ``with`` and the result is an explicit ``.copy()`` that no
    longer references the source, so handles are released promptly.

    Accepts a filesystem path or raw bytes (wrapped in ``BytesIO``).
    """
    if isinstance(source, (bytes, bytearray)):
        opener: "Image.Image" = Image.open(io.BytesIO(source))
    else:
        opener = Image.open(str(Path(source)))
    with opener:
        return opener.convert("RGBA").copy()


def finalize_rendered_image(
    image: "Image.Image",
    *,
    gc_collect: bool = False,
    format: str = "PNG",
    keep_alpha: bool = False,
    jpeg_quality: int = 85,
    jpeg_subsampling: int = 1,
) -> bytes:
    """Finalize a composed canvas into encoded image bytes while freeing memory.

    Render functions build large full-size in-memory canvases. Converting to RGB
    (3 bytes/px instead of 4) and explicitly dropping the source canvas + pixels
    before returning keeps the peak memory of a single request lower and lets the
    GC reclaim promptly, which matters when several heavy renders overlap.

    - ``format="PNG"`` (default) drops the alpha channel unless ``keep_alpha`` is
      set, matching the project's existing ``.convert("RGB").save(..., "PNG")``.
    - ``format="JPEG"`` always converts to RGB (JPEG has no alpha).

    Returns raw encoded bytes; callers wrap this in their module's
    ``RenderedImage``.
    """
    fmt = str(format or "PNG").upper()
    if fmt == "JPEG":
        save_image = image.convert("RGB")
    elif keep_alpha:
        save_image = image
    else:
        save_image = image.convert("RGB")
    del image

    out = BytesIO()
    try:
        if fmt == "JPEG":
            try:
                save_image.save(
                    out,
                    format="JPEG",
                    quality=int(jpeg_quality),
                    subsampling=int(jpeg_subsampling),
                    optimize=True,
                )
            except OSError:
                save_image.save(
                    out,
                    format="JPEG",
                    quality=max(65, int(jpeg_quality) - 6),
                    subsampling=int(jpeg_subsampling),
                    optimize=True,
                )
        else:
            save_image.save(out, format="PNG", optimize=True)
    finally:
        del save_image

    data = out.getvalue()
    del out
    if gc_collect:
        gc.collect()
    return data


__all__ = ["finalize_rendered_image", "run_render", "get_global_render_limit", "get_global_render_semaphore"]


# ── 全局渲染并发闸门 + executor 卸载 ────────────────────────────
#
# 背景：OverstatsCoreService 的所有模块 coroutine 都跑在同一个
# AsyncRunner 事件循环线程上。PIL 渲染是 CPU 密集操作，若直接在循环
# 线程内同步执行，会阻塞该线程上的全部其他协程（上游 API 请求、其他
# 渲染、DB 写入），高峰期表现为所有请求一起变慢。
#
# run_render() 把同步渲染函数卸载到默认线程池执行，并受全局渲染并发
# 闸门约束（默认同时 2 个，环境变量 OVERSTATS_RENDER_CONCURRENCY 可调）。
# 渲染期间事件循环可继续处理其他请求的网络 IO。

def _read_env_int(name: str, default: int) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


GLOBAL_RENDER_CONCURRENCY = max(
    1,
    _read_env_int("OVERSTATS_RENDER_CONCURRENCY", _read_env_int("OVERSHOP_RENDER_CONCURRENCY", 2)),
)
_GLOBAL_RENDER_SEMAPHORE_BY_LOOP: dict = {}


def get_global_render_limit() -> int:
    return GLOBAL_RENDER_CONCURRENCY


def get_global_render_semaphore() -> asyncio.Semaphore:
    """按事件循环维度返回渲染并发信号量（不同 loop 不共享）。"""
    loop = asyncio.get_running_loop()
    semaphore = _GLOBAL_RENDER_SEMAPHORE_BY_LOOP.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(GLOBAL_RENDER_CONCURRENCY)
        _GLOBAL_RENDER_SEMAPHORE_BY_LOOP[loop] = semaphore
    return semaphore


async def run_render(func, *args, **kwargs):
    """在 executor 中执行同步渲染函数，受全局渲染闸门约束。

    用法（async service 内）：
        image = await run_render(render_xxx, arg1, kw=arg2)
    """
    loop = asyncio.get_running_loop()
    async with get_global_render_semaphore():
        return await loop.run_in_executor(None, partial(func, *args, **kwargs))
