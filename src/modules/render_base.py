from __future__ import annotations

import gc
import io
import os
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


__all__ = ["finalize_rendered_image"]
