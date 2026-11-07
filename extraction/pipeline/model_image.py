"""Fit a page render within the Claude image limits: at most 8000 px per side, and
at most 10 MB once base64-encoded. The stored page image is never reduced; only
the copy sent to the model is, and only when the stored render exceeds a limit."""
from __future__ import annotations

import struct

from pipeline.pdf import render_png

MAX_SIDE_PX = 8000
MAX_BASE64_BYTES = 10 * 1024 * 1024
_SHRINK_STEP = 0.85
_MAX_SHRINK_STEPS = 6
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_dimensions(png: bytes) -> tuple[int, int]:
    if png[:8] != _PNG_SIGNATURE:
        raise ValueError("page render is not a PNG")
    width, height = struct.unpack(">II", png[16:24])
    return width, height


def fits_model_limits(png: bytes) -> bool:
    width, height = _png_dimensions(png)
    base64_len = 4 * ((len(png) + 2) // 3)
    return max(width, height) <= MAX_SIDE_PX and base64_len <= MAX_BASE64_BYTES


def model_image(doc, page: int, png: bytes, dpi: int) -> tuple[bytes, int]:
    """Return (png, dpi) to send to the model. Reuses the stored render when it fits."""
    if fits_model_limits(png):
        return png, dpi

    rect = doc[page - 1].rect
    side_px = max(rect.width, rect.height) / 72 * dpi
    factor = min(_SHRINK_STEP, MAX_SIDE_PX / side_px)
    for _ in range(_MAX_SHRINK_STEPS):
        model_dpi = max(1, int(dpi * factor))
        candidate = render_png(doc, page, model_dpi)
        if fits_model_limits(candidate):
            return candidate, model_dpi
        factor *= _SHRINK_STEP
    raise RuntimeError(f"page {page} cannot be fitted within Claude image limits at any DPI")
