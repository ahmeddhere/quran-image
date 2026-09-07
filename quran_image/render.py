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

``mode="rgba"`` is available for callers that want a straight coverage->alpha
image instead.
"""
from __future__ import annotations

import io

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


def render_page(plan: PagePlan, mode: str = "palette") -> Image.Image:
    cov = _coverage(plan)

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
_CONTENT_TYPE = {"png": "image/png", "webp": "image/webp"}


def encode_image(img: Image.Image, fmt: str = "png", *, optimize: bool = True) -> tuple[bytes, str]:
    """Encode ``img`` to ``(bytes, content_type)`` for an HTTP response.

    ``png``  - a tiny mode-``P`` + ``tRNS`` file (transparent white + 8 greys).
    ``webp`` - lossless (+ ``exact``) WebP built from the same coverage;
               ~10-20 % smaller on the wire, decodes to byte-identical RGBA.

    Both keep the Mushaf layout untouched - only the container changes.
    """
    fmt = fmt.lower()
    buf = io.BytesIO()
    if fmt == "png":
        params = {"format": "PNG", "optimize": optimize, "compress_level": 9}
        if img.mode == "P":
            params["transparency"] = 0
        img.save(buf, **params)
    elif fmt == "webp":
        # lossless so the 8-level AA ramp and the alpha survive exactly;
        # method=4 is the latency/size sweet spot.  ``convert`` leaves fully
        # transparent pixels as (0,0,0,0); force them to white so a raw RGB
        # read matches the palette PNG too (the composite is identical either
        # way).  Result: byte-for-byte the same decoded RGBA as the PNG.
        rgba = np.array(img.convert("RGBA"))
        rgba[rgba[..., 3] == 0, :3] = 255
        Image.fromarray(rgba, "RGBA").save(
            buf, format="WEBP", lossless=True, quality=100, method=4, exact=True
        )
    else:
        raise ValueError(f"unsupported image format {fmt!r} (png|webp)")
    return buf.getvalue(), _CONTENT_TYPE[fmt]
