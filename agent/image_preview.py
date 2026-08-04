"""Image thumbnail rendering for terminal surfaces.

Generalizes the pet sprite pipeline (``agent/pet/render.py``) from spritesheets
to arbitrary images, so any linked/attached image can be previewed inline.

Design notes
------------
* The pet module's encoders (``_encode_kitty`` / ``_encode_iterm`` /
  ``_encode_sixel`` / ``_encode_unicode``) are already image-agnostic: they take
  a PIL frame, not a sprite. This module reuses them rather than reimplementing
  four protocols.
* ``agent.pet.render.resolve_mode`` is a *policy* layer that returns ``off`` for
  a non-TTY stream. That is correct for auto-display (never dump 200 KB of
  escape bytes into a logfile) but wrong as a capability check — the encoders
  work fine without a TTY. Callers that know their surface can display graphics
  pass ``mode=`` explicitly.
* Thumbnails are bounded by terminal *cells*, not pixels, because the consumer
  is a text grid. A cell is ~2:1 tall, so a cell box maps to a pixel box using
  ``CELL_ASPECT``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Terminal cells are roughly twice as tall as they are wide.
CELL_ASPECT = 2.0

# Default preview box in terminal cells.
DEFAULT_COLS = 40
DEFAULT_ROWS = 20

# Refuse absurd inputs rather than trying to decode a 2 GB TIFF.
MAX_SOURCE_BYTES = 64 * 1024 * 1024

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".avif"}


class ThumbnailError(Exception):
    """Raised when an image cannot be turned into a preview."""


def is_image_path(path: str | Path) -> bool:
    """Cheap extension check — does NOT touch the filesystem."""
    return Path(path).suffix.lower() in IMAGE_SUFFIXES


def _load(path: str | Path):
    from PIL import Image

    p = Path(path)
    if not p.is_file():
        raise ThumbnailError(f"not a file: {p}")

    size = p.stat().st_size
    if size > MAX_SOURCE_BYTES:
        raise ThumbnailError(f"image too large: {size} bytes (max {MAX_SOURCE_BYTES})")
    if size == 0:
        raise ThumbnailError(f"empty file: {p}")

    try:
        img = Image.open(p)
        img.load()
    except Exception as exc:  # noqa: BLE001 — PIL raises many unrelated types
        raise ThumbnailError(f"cannot decode {p.name}: {exc}") from exc

    # Honor EXIF orientation; a sideways preview is a bug users notice.
    try:
        from PIL import ImageOps

        rotated = ImageOps.exif_transpose(img)
        if rotated is not None:
            img = rotated
    except Exception:
        pass

    return img.convert("RGBA")


def fit_cells(width: int, height: int, *, max_cols: int, max_rows: int) -> tuple[int, int]:
    """Fit a pixel box into a cell box, preserving aspect ratio.

    Returns ``(cols, rows)``, each at least 1. Accounts for the ~2:1 cell
    aspect so a square image comes back roughly square on screen.
    """
    if width <= 0 or height <= 0:
        return (1, 1)

    # Cell-space aspect: how many cells tall per cell wide.
    ratio = (height / width) / CELL_ASPECT

    # Fit width-first, then correct if that overflows the row budget. Doing it
    # in this order (rather than capping cols up front) is what lets a portrait
    # image come back narrower than the box instead of pinned to max_cols.
    cols = max_cols
    rows = max(1, round(cols * ratio))

    if rows > max_rows:
        rows = max_rows
        cols = max(1, round(rows / ratio)) if ratio > 0 else max_cols

    return (max(1, min(cols, max_cols)), max(1, min(rows, max_rows)))


def render_thumbnail(
    path: str | Path,
    *,
    mode: str = "auto",
    max_cols: int = DEFAULT_COLS,
    max_rows: int = DEFAULT_ROWS,
) -> str:
    """Return escape/text output previewing *path*, sized to a cell box.

    ``mode`` is one of ``auto`` / ``kitty`` / ``iterm`` / ``sixel`` /
    ``unicode``. ``auto`` detects from the environment WITHOUT requiring a TTY
    (see module docstring), falling back to ``unicode`` half-blocks, which work
    in any truecolor terminal.

    Raises ``ThumbnailError`` on unreadable/undecodable input.
    """
    from agent.pet import render as pet_render

    if mode == "auto":
        mode = pet_render.detect_terminal_graphics()
    if mode == "off":
        return ""

    img = _load(path)
    cols, rows = fit_cells(img.width, img.height, max_cols=max_cols, max_rows=max_rows)

    if mode == "unicode":
        return pet_render._encode_unicode(img, target_cols=cols)

    # Pixel-space downscale for the graphics protocols: sending a 12 MP source
    # to draw in a 40x20 cell box wastes bandwidth and terminal memory.
    target_w = max(1, cols * 10)
    try:
        from PIL import Image as _Image

        target_h = max(1, int(target_w * img.height / max(1, img.width)))
        img = img.resize((target_w, target_h), _Image.Resampling.LANCZOS)
    except Exception:
        pass

    if mode == "kitty":
        return pet_render._encode_kitty(img, cell_cols=cols, cell_rows=rows)
    if mode == "iterm":
        return pet_render._encode_iterm(img, cell_cols=cols, cell_rows=rows)
    if mode == "sixel":
        return pet_render._encode_sixel(img)

    return pet_render._encode_unicode(img, target_cols=cols)


def thumbnail_cells(
    path: str | Path,
    *,
    max_cols: int = DEFAULT_COLS,
    max_rows: int = DEFAULT_ROWS,
) -> list[list[tuple]]:
    """Return half-block cell data for structured (Ink) consumers.

    Mirrors the pet module's ``cells`` API: rows of ``(top_rgba, bottom_rgba)``
    so a React/Ink renderer can emit its own width-counted <Text> nodes instead
    of raw escapes it cannot measure.
    """
    from agent.pet import render as pet_render

    img = _load(path)
    cols, _rows = fit_cells(img.width, img.height, max_cols=max_cols, max_rows=max_rows)
    return pet_render._downscale_cells(img, target_cols=cols)


def describe(path: str | Path) -> dict:
    """Metadata for a preview caption. Never raises."""
    info: dict = {"name": Path(path).name}
    try:
        p = Path(path)
        info["bytes"] = p.stat().st_size
        img = _load(p)
        info["width"], info["height"] = img.width, img.height
        info["format"] = (img.format or Path(path).suffix.lstrip(".")).upper()
    except Exception as exc:  # noqa: BLE001
        info["error"] = str(exc)
    return info
