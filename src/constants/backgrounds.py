from __future__ import annotations

import hashlib
import os
import random
import tempfile
import threading
from pathlib import Path
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESOURCE_DIR = PROJECT_ROOT / "res"
QUERY_TOOL_MAPS_DIR = RESOURCE_DIR / "query_tool_assets" / "maps"
BACKGROUND_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})

def _read_env_int(name: str, default: int) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)


# ── 预模糊背景磁盘缓存 ──
# 背景生成是纯 CPU：LANCZOS 拟合 + 调色 + 高斯模糊（1400x2500 画布约 130~260ms/次）。
# 模糊结果熵极低，PNG 仅约 60KB，因此用磁盘换内存与 CPU：命中读取约 19ms。
# 键包含源图 mtime/size 与参数，改图或改渲染参数会自动失效。
RENDER_BG_CACHE_VERSION = 1
RENDER_BG_CACHE_DIR = PROJECT_ROOT / "cache" / "render_bg"
RENDER_BG_CACHE_ENABLED = os.getenv("OVERSTATS_RENDER_BG_CACHE", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
RENDER_BG_CACHE_MAX_BYTES = max(64, _read_env_int("OVERSTATS_RENDER_BG_CACHE_MB", 512)) * 1024 * 1024
RENDER_BG_HEIGHT_QUANTUM = 256
_PRUNE_LOCK = threading.Lock()
_WRITE_STATE = {"writes": 0, "bytes": -1}


def list_map_background_paths(directory: Path | None = None) -> list[Path]:
    root = Path(directory) if directory is not None else QUERY_TOOL_MAPS_DIR
    if not root.exists() or not root.is_dir():
        return []
    return sorted(
        path
        for path in root.iterdir()
        if path.is_file() and path.suffix.lower() in BACKGROUND_IMAGE_SUFFIXES
    )


def pick_random_map_background_path(*, directory: Path | None = None, rng: Any | None = None) -> Path | None:
    candidates = list_map_background_paths(directory)
    if not candidates:
        return None
    chooser = rng if rng is not None else random
    return Path(chooser.choice(candidates))


def _quantize_height(height: int) -> int:
    quantum = RENDER_BG_HEIGHT_QUANTUM
    return max(quantum, ((int(height) + quantum - 1) // quantum) * quantum)


def _background_cache_key(
    source: Path,
    size: Sequence[int],
    *,
    blur_radius: int,
    brightness: float,
    color: float,
    overlay: tuple[int, int, int, int] | None,
) -> str:
    try:
        stat = source.stat()
        stamp = f"{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        stamp = "0:0"
    payload = "|".join(
        [
            f"v{RENDER_BG_CACHE_VERSION}",
            str(source),
            stamp,
            f"{int(size[0])}x{int(size[1])}",
            f"blur={int(blur_radius)}",
            f"brightness={float(brightness):.4f}",
            f"color={float(color):.4f}",
            f"overlay={tuple(int(v) for v in overlay) if overlay is not None else 'none'}",
        ]
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:32]


def _background_cache_path(key: str) -> Path:
    return RENDER_BG_CACHE_DIR / f"{key}.png"


def _read_cached_background(key: str, size: Sequence[int]) -> Any | None:
    from PIL import Image

    path = _background_cache_path(key)
    try:
        with Image.open(path) as image:
            cached = image.convert("RGBA")
    except Exception:
        return None
    width, height = int(size[0]), int(size[1])
    if cached.width < width or cached.height < height:
        return None
    if cached.width == width and cached.height == height:
        return cached
    # 量化为更高的画布，这里裁回精确尺寸（约 2ms，远低于重新模糊）。
    return cached.crop((0, 0, width, height))


def _write_cached_background(key: str, image: Any) -> None:
    path = _background_cache_path(key)
    try:
        RENDER_BG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if path.exists():
            return
        fd, temp_path = tempfile.mkstemp(prefix=f"{key}.", suffix=".tmp", dir=str(RENDER_BG_CACHE_DIR))
        try:
            with os.fdopen(fd, "wb") as file:
                image.save(file, format="PNG", compress_level=6)
            Path(temp_path).replace(path)
        except Exception:
            try:
                Path(temp_path).unlink(missing_ok=True)
            except OSError:
                pass
            return
    except Exception:
        return
    _schedule_prune()


def _schedule_prune() -> None:
    """每 32 次写入检查一次目录体积，按 mtime 淘汰到 80% 上限以内。"""
    with _PRUNE_LOCK:
        _WRITE_STATE["writes"] += 1
        if _WRITE_STATE["writes"] % 32 != 0:
            return
    prune_background_cache()


def prune_background_cache(max_bytes: int | None = None) -> int:
    """按 mtime 从旧到新淘汰，返回删除的文件数。"""
    limit = int(max_bytes if max_bytes is not None else RENDER_BG_CACHE_MAX_BYTES)
    try:
        entries = [
            (path.stat().st_mtime, path.stat().st_size, path)
            for path in RENDER_BG_CACHE_DIR.glob("*.png")
            if path.is_file()
        ]
    except Exception:
        return 0
    total = sum(size for _, size, _ in entries)
    if total <= limit:
        return 0
    removed = 0
    target = int(limit * 0.8)
    for _, size, path in sorted(entries):
        if total <= target:
            break
        try:
            path.unlink()
            total -= size
            removed += 1
        except OSError:
            continue
    _WRITE_STATE["bytes"] = total
    return removed


def build_random_map_background(
    size: Sequence[int],
    *,
    blur_radius: int = 0,
    overlay: tuple[int, int, int, int] | None = None,
    brightness: float = 0.82,
    color: float = 0.9,
    directory: Path | None = None,
    rng: Any | None = None,
) -> Any | None:
    try:
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    except ModuleNotFoundError:
        return None

    try:
        width = int(size[0])
        height = int(size[1])
    except Exception:
        return None
    if width <= 0 or height <= 0:
        return None

    background_path = pick_random_map_background_path(directory=directory, rng=rng)
    if background_path is None:
        return None

    # 模糊是唯一的 CPU 大头，只对模糊背景做磁盘缓存（输出 PNG 也最小）。
    cache_key = None
    compute_size = (width, height)
    if RENDER_BG_CACHE_ENABLED and int(blur_radius or 0) > 0:
        compute_size = (width, _quantize_height(height))
        cache_key = _background_cache_key(
            background_path,
            compute_size,
            blur_radius=int(blur_radius or 0),
            brightness=brightness,
            color=color,
            overlay=overlay,
        )
        cached = _read_cached_background(cache_key, (width, height))
        if cached is not None:
            return cached

    try:
        with Image.open(background_path) as raw_image:
            background = raw_image.convert("RGBA")
    except Exception:
        return None

    try:
        background = ImageOps.fit(
            background,
            compute_size,
            method=_resampling_lanczos(),
            centering=(0.5, 0.5),
        )
    except Exception:
        try:
            background = background.resize(compute_size, _resampling_lanczos())
        except Exception:
            return None

    if color != 1.0:
        try:
            background = ImageEnhance.Color(background).enhance(float(color))
        except Exception:
            pass
    if brightness != 1.0:
        try:
            background = ImageEnhance.Brightness(background).enhance(float(brightness))
        except Exception:
            pass
    if int(blur_radius or 0) > 0:
        background = background.filter(ImageFilter.GaussianBlur(radius=int(blur_radius)))
    if overlay is not None:
        # 原地合成，避免再分配一张整图（1400x2500 约 13.4MB / 13ms）。
        try:
            background.alpha_composite(Image.new("RGBA", compute_size, overlay))
        except Exception:
            background = Image.alpha_composite(background, Image.new("RGBA", compute_size, overlay))

    if cache_key is not None:
        try:
            _write_cached_background(cache_key, background)
        except Exception:
            pass
        if (background.width, background.height) != (width, height):
            return background.crop((0, 0, width, height))
    return background


def _resampling_lanczos() -> Any:
    from PIL import Image

    resampling = getattr(Image, "Resampling", Image)
    return getattr(resampling, "LANCZOS")
