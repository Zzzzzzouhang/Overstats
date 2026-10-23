from __future__ import annotations

import asyncio
import gc
import io
import os
from contextlib import asynccontextmanager
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


__all__ = [
    "finalize_rendered_image",
    "run_render",
    "stage_slot",
    "configure_stage_gate",
    "get_stage_limit",
    "get_stage_semaphore",
    "get_global_render_limit",
    "get_global_render_semaphore",
]


# ── 二段闸门：渲染 / AI 等待阶段 ────────────────────────────
#
# 背景：OverstatsCoreService 的所有模块 coroutine 都跑在同一个
# AsyncRunner 事件循环线程上。PIL 渲染是 CPU 密集操作，若直接在循环
# 线程内同步执行，会阻塞该线程上的全部其他协程（上游 API 请求、其他
# 渲染、DB 写入），高峰期表现为所有请求一起变慢。
#
# 并发模型（抓取与渲染/AI 剥离）：
#   - 抓取阶段：受 DashenRequestQueue 槽位控制（默认 2，见 config
#     DASHEN_MAX_CONCURRENT_REQUESTS / OVERSTATS_DASHEN_MAX_CONCURRENT_REQUESTS）。
#   - 渲染阶段与"等单局 AI 返回"阶段：不占用抓取槽位，统一走本闸门，
#     容量 = 抓取槽位 × OVERSTATS_STAGE_MULTIPLIER（默认 3，即 6）。
#     可用 OVERSTATS_STAGE_CONCURRENCY 直接指定绝对容量；
#     旧变量 OVERSTATS_RENDER_CONCURRENCY 显式设置时仍然优先（兼容）。
#
# 闸门按事件循环维度懒创建（不同 loop 不共享）。


def _read_env_int(name: str, default: int) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


STAGE_MULTIPLIER = max(1, _read_env_int("OVERSTATS_STAGE_MULTIPLIER", 3))
STAGE_CONCURRENCY_OVERRIDE = max(0, _read_env_int("OVERSTATS_STAGE_CONCURRENCY", 0))
_LEGACY_RENDER_CONCURRENCY = max(0, _read_env_int("OVERSTATS_RENDER_CONCURRENCY", 0))

# 抓取槽位数：create_server 启动时用真实队列槽位数覆盖；
# 直接使用模块（未走 create_server）时回退到 config 默认值。
_fetch_slot_limit = max(
    1,
    _read_env_int(
        "OVERSTATS_DASHEN_MAX_CONCURRENT_REQUESTS",
        _read_env_int("OVERSHOP_DASHEN_MAX_CONCURRENT", 2),
    ),
)


def configure_stage_gate(fetch_slots: int) -> None:
    """由 create_server 调用，使渲染/AI 闸门容量与真实抓取槽位数对齐。"""
    global _fetch_slot_limit
    _fetch_slot_limit = max(1, int(fetch_slots or 1))


def get_stage_limit() -> int:
    """渲染 + 等 AI 阶段的并发容量（= 抓取槽位 × 3，可被环境变量覆盖）。"""
    if STAGE_CONCURRENCY_OVERRIDE > 0:
        return STAGE_CONCURRENCY_OVERRIDE
    if _LEGACY_RENDER_CONCURRENCY > 0:
        return _LEGACY_RENDER_CONCURRENCY
    return max(1, _fetch_slot_limit * STAGE_MULTIPLIER)


_STAGE_SEMAPHORE_BY_LOOP: dict = {}


def get_stage_semaphore() -> asyncio.Semaphore:
    """按事件循环维度返回渲染/AI 阶段信号量（不同 loop 不共享）。"""
    loop = asyncio.get_running_loop()
    semaphore = _STAGE_SEMAPHORE_BY_LOOP.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(get_stage_limit())
        _STAGE_SEMAPHORE_BY_LOOP[loop] = semaphore
    return semaphore


# 兼容旧名（summary runtime 等历史调用点）。
def get_global_render_semaphore() -> asyncio.Semaphore:
    return get_stage_semaphore()


def get_global_render_limit() -> int:
    return get_stage_limit()


@asynccontextmanager
async def stage_slot():
    """渲染 / 等 AI 阶段槽位。

    抓取阶段（DashenRequestQueue 槽内）结束后调用，渲染 PIL 与等待
    单局 AI 返回都在此槽内执行，互不占用抓取槽位。
    """
    async with get_stage_semaphore():
        yield


async def run_render(func, *args, **kwargs):
    """在 executor 中执行同步渲染函数，受渲染/AI 阶段闸门约束。

    用法（async service 内）：
        image = await run_render(render_xxx, arg1, kw=arg2)
    """
    loop = asyncio.get_running_loop()
    async with stage_slot():
        return await loop.run_in_executor(None, partial(func, *args, **kwargs))
