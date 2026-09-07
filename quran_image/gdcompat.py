"""FreeType re-implementation of the slice of libgd + GD::Text the Perl
Qur'an renderer relied on.

Three primitives are reproduced:

``string_brect(font, codepoints, ptsize, hdpi, vdpi)``
    == ``GD::Image->stringFT`` bounding rectangle.  libgd computes every
    advance / control-box in a resolution-independent **300 dpi**
    (``METRIC_RES``) 26.6 space with hinted ``FT_LOAD_DEFAULT`` metrics, then
    scales the 8 corners to the requested dpi and truncates to ``int``
    (``libgd/src/gdft.c``).

``text_metrics(font, ptsize)``
    == ``GD::Text->get('char_up','char_down','space')``.  GD::Text renders a
    fixed ASCII test string (U+0021..U+007E) and takes ``char_up=-bb[7]``,
    ``char_down=bb[1]``.  In practice libgd resolves those code points
    through the font's *first* Unicode cmap subtable, which for the QCF v1
    fonts maps them to a handful of near-empty low glyphs, so the result is
    essentially font-independent.  Rather than re-derive libgd's exact cmap
    pick we use values captured directly from the reference container
    (``data/gdtext_metrics.json``) and fall back to a live computation.

``glyph_bitmap(font, codepoint, ptsize, hdpi=96, vdpi=94)``
    == the actual ``$image->stringFT(..., {resolution=>'96,94'})`` draw:
    render the glyph at 96x94 dpi, 8-bit coverage.

Only angle 0 and kerning-free fonts are handled (the QCF v1 fonts have no
``kern`` table; the Perl draw passes ``kerning => 0``).
"""
from __future__ import annotations

import functools
import json
import math
import os
import re
from dataclasses import dataclass

import freetype

METRIC_RES = 300      # libgd METRIC_RES
GD_RESOLUTION = 96    # libgd default h/v dpi (no {resolution} option)
DRAW_HDPI = 96        # the Perl draw passes resolution => '96,94'
DRAW_VDPI = 94

FT_LOAD_DEFAULT = freetype.FT_LOAD_DEFAULT
FT_LOAD_RENDER = freetype.FT_LOAD_RENDER
try:
    FT_ENCODING_UNICODE = freetype.FT_ENCODING_UNICODE
except AttributeError:  # older freetype-py
    FT_ENCODING_UNICODE = freetype.FT_ENCODINGS["FT_ENCODING_UNICODE"]

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_METRICS_JSON = os.path.join(_DATA_DIR, "gdtext_metrics.json")

# Perl: if ($font =~ /P105|P237|P552|P554/) -> borrow BSML's char_up/char_down
_ARTIFACT_FONT_RE = re.compile(r"P(?:105|237|552|554)")
_BSML = "QCF_BSML.TTF"


def c_int(x: float) -> int:
    """C ``(int)`` cast - truncate toward zero."""
    return math.trunc(x)


# --------------------------------------------------------------------------- #
# face cache  (charmap chosen exactly like gdft.c: first FT_ENCODING_UNICODE)
# --------------------------------------------------------------------------- #
@functools.lru_cache(maxsize=None)
def _face(path: str) -> freetype.Face:
    face = freetype.Face(path)
    for cm in face.charmaps:
        if cm.encoding == FT_ENCODING_UNICODE:
            face.set_charmap(cm)
            break
    return face


def _set_size(face: freetype.Face, ptsize: float, hdpi: int, vdpi: int) -> None:
    # libgd: FT_Set_Char_Size(face, 0, (FT_F26Dot6)(ptsize*64), hdpi, vdpi)
    face.set_char_size(0, int(ptsize * 64), hdpi, vdpi)


# --------------------------------------------------------------------------- #
# glyph metrics in the 300 dpi metric space (hinted, FT_LOAD_DEFAULT)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _GMetrics:
    hori_advance: int
    hori_bearing_x: int
    hori_bearing_y: int
    width: int
    height: int


@functools.lru_cache(maxsize=400_000)
def _glyph_metrics_300(path: str, codepoint: int, ptsize_q: int) -> _GMetrics:
    face = _face(path)
    _set_size(face, ptsize_q / 64.0, METRIC_RES, METRIC_RES)
    gi = face.get_char_index(codepoint)  # 0 == .notdef, matches GD
    face.load_glyph(gi, FT_LOAD_DEFAULT)
    m = face.glyph.metrics
    return _GMetrics(m.horiAdvance, m.horiBearingX, m.horiBearingY, m.width, m.height)


# --------------------------------------------------------------------------- #
# string_brect  -  GD::Image->stringFT bounding rectangle
# --------------------------------------------------------------------------- #
def string_brect(
    font_path: str,
    codepoints,
    ptsize: float,
    hdpi: int = GD_RESOLUTION,
    vdpi: int = GD_RESOLUTION,
    x: float = 0.0,
    y: float = 0.0,
):
    """The 8-element ``brect`` GD returns: [0,1] LL, [2,3] LR, [4,5] UR, [6,7] UL."""
    codepoints = list(codepoints)
    if not codepoints:
        return (0, 0, 0, 0, 0, 0, 0, 0)
    ptsize_q = int(ptsize * 64)

    pen = 0
    tmin_x = tmin_y = tmax_x = tmax_y = 0
    for i, cp in enumerate(codepoints):
        gm = _glyph_metrics_300(font_path, cp, ptsize_q)
        gmin_x = pen + gm.hori_bearing_x
        gmin_y = -gm.hori_bearing_y
        gmax_x = pen + gm.hori_advance
        gmax_y = gmin_y + gm.height
        if i == 0:
            tmin_x, tmin_y, tmax_x, tmax_y = gmin_x, gmin_y, gmax_x, gmax_y
        else:
            tmin_x = min(tmin_x, gmin_x)
            tmin_y = min(tmin_y, gmin_y)
            tmax_x = max(tmax_x, gmax_x)
            tmax_y = max(tmax_y, gmax_y)
        pen += gm.hori_advance

    sx = hdpi / (64.0 * METRIC_RES)
    sy = vdpi / (64.0 * METRIC_RES)
    return (
        c_int(x + tmin_x * sx),  # 0
        c_int(y + tmax_y * sy),  # 1
        c_int(x + tmax_x * sx),  # 2
        c_int(y + tmax_y * sy),  # 3
        c_int(x + tmax_x * sx),  # 4
        c_int(y + tmin_y * sy),  # 5
        c_int(x + tmin_x * sx),  # 6
        c_int(y + tmin_y * sy),  # 7
    )


# --------------------------------------------------------------------------- #
# GD::Text char_up / char_down / space
# --------------------------------------------------------------------------- #
_TEST_CODES = list(range(0x21, 0x7F))  # GD::Text test string on a modern Perl
_SPACE_STRING, _N_SPACES = re.subn(
    r"(.{5})(.{5})", r"\1 \2", "".join(chr(c) for c in _TEST_CODES)
)


@functools.lru_cache(maxsize=1)
def _baked_metrics() -> dict:
    try:
        with open(_METRICS_JSON, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def _live_text_metrics(font_path: str, ptsize: float):
    bb1 = string_brect(font_path, _TEST_CODES, ptsize)
    bb2 = string_brect(font_path, [ord(c) for c in _SPACE_STRING], ptsize)
    char_up = -bb1[7]
    char_down = bb1[1]
    space = int(round(((bb2[2] - bb2[0]) - (bb1[2] - bb1[0])) / _N_SPACES))
    return char_up, char_down, space


@functools.lru_cache(maxsize=8192)
def text_metrics(font_path: str, ptsize: float, bsml_path: str | None = None):
    """(char_up, char_down, space) == GD::Text::_recalc for this font/ptsize."""
    baked = _baked_metrics()
    name = os.path.basename(font_path)
    key = f"{name}|{ptsize:g}"

    if _ARTIFACT_FONT_RE.search(name):
        # Perl: borrow char_up/char_down from the page default font (BSML)
        bsml_key = f"{_BSML}|{ptsize:g}"
        if bsml_key in baked:
            cu, cd, _ = baked[bsml_key]
            _, _, sp = baked.get(key, (0, 0, cu))  # keep own space if known
            return cu, cd, sp
        b_up, b_dn, _ = _live_text_metrics(bsml_path or font_path, ptsize)
        _, _, sp = _live_text_metrics(font_path, ptsize)
        return b_up, b_dn, sp

    if key in baked:
        return tuple(baked[key])
    return _live_text_metrics(font_path, ptsize)


# --------------------------------------------------------------------------- #
# glyph rasterisation  -  $image->stringFT(...,{resolution=>'96,94'})
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GlyphBitmap:
    buffer: bytes
    width: int
    rows: int
    left: int
    top: int


@functools.lru_cache(maxsize=400_000)
def glyph_bitmap(
    font_path: str,
    codepoint: int,
    ptsize: float,
    hdpi: int = DRAW_HDPI,
    vdpi: int = DRAW_VDPI,
) -> GlyphBitmap:
    face = _face(font_path)
    _set_size(face, ptsize, hdpi, vdpi)
    gi = face.get_char_index(codepoint)
    face.load_glyph(gi, FT_LOAD_DEFAULT | FT_LOAD_RENDER)
    bm = face.glyph.bitmap
    return GlyphBitmap(
        buffer=bytes(bm.buffer),
        width=bm.width,
        rows=bm.rows,
        left=face.glyph.bitmap_left,
        top=face.glyph.bitmap_top,
    )


def draw_origin_px(coord_x: float, coord_y: float, gb: GlyphBitmap) -> tuple[int, int]:
    """Top-left pixel where GD blits this glyph (pen == 0, glyphs drawn singly):
        ix = (int)(coord_x + bitmap_left)
        iy = (int)(coord_y - bitmap_top)
    """
    return c_int(coord_x + gb.left), c_int(coord_y - gb.top)
