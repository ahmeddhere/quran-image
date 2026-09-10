"""Rasterise a :class:`~quran_image.layout.PagePlan` to a transparent PNG.

The Perl code built a **palette** ``GD::Image`` with a fully-transparent white
background (index 0) and drew anti-aliased black text with ``setAntiAliased``.
libgd's palette anti-aliasing snaps every edge pixel to one of 8 opaque grey
levels (``0, 31, 63, 95, 127, 159, 191, 223``); anything lighter resolves to
the transparent white index.  The reference PNGs are therefore tiny indexed
images with a ``tRNS`` of a single transparent entry - and that is exactly
what the Flutter app ships.

We reproduce that:

1. rasterise every glyph at 96x94 dpi and ``max``-composite the 8-bit coverage
   (overlapping edges must not darken - GD draws with ``gdAntiAliased`` which
   is not additive);
2. quantise coverage ``c`` the way libgd does:  ``g = 255-c``;
   ``bin = int(g/32 + 0.5)``;  ``bin==8`` -> transparent, else opaque grey
   ``max(0, bin*32-1)``;
3. emit a mode ``P`` PNG (palette = white + 8 greys) with ``transparency=0``.

That palette path is **byte-exact parity with the legacy asset, not the best
image we can make**.  FreeType hands us 256 levels of coverage; the libgd
quantisation throws away all but 9 of them, clips every pixel under ~6 %
coverage to fully transparent, and - because a palette PNG's ``tRNS`` makes
index 0 transparent and every other index fully opaque - leaves *no partial
alpha whatsoever*.  Anti-aliasing survives only as opaque grey, which is
correct on white and wrong on anything else; on diagonals and on the thin
curves of the tashkeel it reads as a stair-stepped edge.

So ``mode="alpha"`` (the default) keeps FreeType's coverage intact as a real
alpha channel, using PNG's *indexed* alpha: 256 black palette entries and a
256-byte ``tRNS`` where ``tRNS[i] == i``, drawn with ``index == coverage``.
Every pixel is one byte, exactly as in the legacy asset, but the byte now means
alpha on a 256-level ramp instead of one of 9 quantisation bins.  It composites
correctly over any background - cream, sepia, dark mode - and has no
stair-stepping.  ``tRNS`` with a per-entry alpha table is core PNG, decoded by
Skia/Flutter, every browser and Pillow alike.

``mode="palette"`` is kept for reference-parity checks and for clients that
need the historical bytes; ``mode="rgba"`` is the same coverage in a plain
4-channel RGBA image for callers that want to post-process it.

Set ``QURAN_RENDER_MODE=palette`` to serve the legacy output instead; the mode
is folded into the asset version, so switching rotates every cache key.
"""
from __future__ import annotations

import io
import os

import numpy as np
from PIL import Image

from . import gdcompat
from .layout import PagePlan

# libgd palette anti-alias ramp (see module docstring)
_GREY_LEVELS = [0, 31, 63, 95, 127, 159, 191, 223]  # bin 0..7  (bin 8 == transparent)
_PALETTE = [255, 255, 255]  # index 0: transparent white
for _g in _GREY_LEVELS:
    _PALETTE += [_g, _g, _g]
_PALETTE += [0, 0, 0] * (256 - 9)  # pad to 256 RGB triples

# full-alpha path: every entry black, tRNS[i] == i, so a pixel's index *is* its
# alpha.  One byte per pixel like the legacy asset, but 256 levels not 9.
_ALPHA_PALETTE = [0, 0, 0] * 256
_ALPHA_TRNS = bytes(range(256))

RENDER_MODES = ("alpha", "palette", "rgba")


def default_render_mode() -> str:
    """Render mode for the server, from ``QURAN_RENDER_MODE`` (default ``alpha``)."""
    mode = (os.environ.get("QURAN_RENDER_MODE") or "alpha").strip().lower()
    if mode not in RENDER_MODES:
        raise ValueError(f"QURAN_RENDER_MODE={mode!r}; use one of {RENDER_MODES}")
    return mode


def _coverage(plan: PagePlan) -> np.ndarray:
    w, h = plan.width, plan.height
    cov = np.zeros((h, w), dtype=np.uint8)
    for op in plan.ops:
        gb = gdcompat.glyph_bitmap(op.font_path, op.code, op.ptsize)
        if gb.width == 0 or gb.rows == 0:
            continue
        x0, y0 = gdcompat.draw_origin_px(op.x, op.y, gb)
        gx0, gy0 = max(0, -x0), max(0, -y0)
        gx1, gy1 = min(gb.width, w - x0), min(gb.rows, h - y0)
        if gx1 <= gx0 or gy1 <= gy0:
            continue
        glyph = np.frombuffer(gb.buffer, dtype=np.uint8).reshape(gb.rows, gb.width)
        dst = cov[y0 + gy0 : y0 + gy1, x0 + gx0 : x0 + gx1]
        np.maximum(dst, glyph[gy0:gy1, gx0:gx1], out=dst)
    return cov


def _quantise_to_bins(cov: np.ndarray) -> np.ndarray:
    """coverage -> libgd bin index 0..8 (8 == transparent)."""
    g = 255 - cov.astype(np.int16)
    bins = ((g / 32.0) + 0.5).astype(np.int16)  # C (int)(x + 0.5)
    np.clip(bins, 0, 8, out=bins)
    return bins.astype(np.uint8)


def render_page(plan: PagePlan, mode: str = "alpha") -> Image.Image:
    """Rasterise ``plan``.  See the module docstring for the mode trade-off."""
    if mode not in RENDER_MODES:
        raise ValueError(f"unknown render mode {mode!r}; use one of {RENDER_MODES}")
    cov = _coverage(plan)

    if mode == "alpha":
        # index == coverage == alpha; all 256 levels survive to the wire
        img = Image.fromarray(cov, mode="P")
        img.putpalette(_ALPHA_PALETTE)
        img.info["transparency"] = _ALPHA_TRNS
        return img

    if mode == "rgba":
        rgba = np.zeros((*cov.shape, 4), dtype=np.uint8)
        rgba[..., 3] = cov
        return Image.fromarray(rgba, mode="RGBA")

    bins = _quantise_to_bins(cov)
    # bin 8 -> palette index 0 (transparent); bin b(0..7) -> index b+1
    idx = np.where(bins == 8, 0, bins + 1).astype(np.uint8)
    img = Image.fromarray(idx, mode="P")
    img.putpalette(_PALETTE)
    img.info["transparency"] = 0
    return img


# --------------------------------------------------------------------------- #
# in-memory encoding  -  the server never touches the filesystem for this
# --------------------------------------------------------------------------- #
_CONTENT_TYPE = {"png": "image/png"}


def encode_image(img: Image.Image, fmt: str = "png", *, optimize: bool = True) -> tuple[bytes, str]:
    """Encode ``img`` to ``(bytes, content_type)`` for an HTTP response.

    ``png`` is the only format - lossless, so the 8-bit alpha ramp on every
    stroke and tashkeel edge survives exactly as rendered.  ``LA`` / ``RGBA``
    images keep their full alpha channel; a mode-``P`` image gets its ``tRNS``
    back.  The Mushaf layout is untouched - only the container changes.
    """
    fmt = fmt.lower()
    if fmt != "png":
        raise ValueError(f"unsupported image format {fmt!r} (png only)")
    buf = io.BytesIO()
    params = {"format": "PNG", "optimize": optimize, "compress_level": 9}
    if img.mode == "P":
        # index 0 for the legacy palette, the 256-byte ramp for the alpha mode
        params["transparency"] = img.info.get("transparency", 0)
    img.save(buf, **params)
    return buf.getvalue(), _CONTENT_TYPE[fmt]
