"""Port of ``Quran::Image::Page::create`` (``lib/Quran/Image/Page.pm``).

Pure geometry: given a page number and a target width it returns a
:class:`PagePlan` - the image size plus an ordered list of glyph draw
operations and the per-ayah bounding boxes.  No pixels are touched here, so
the algorithm can be unit-tested and diffed against the Perl output.

Every step mirrors the Perl code; comments quote the original where it is
not obvious.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import gdcompat
from .db import LayoutDB

# Bump when the layout geometry or the raster pipeline changes in a way that
# alters output pixels - it is folded into the asset/cache version so every
# server- and device-side cache entry is invalidated automatically.
LAYOUT_VERSION = 1

PHI = (math.sqrt(5) + 1) / 2  # Quran::Image::PHI
DEFAULT_FONTFACTOR = 21
PAGE_FONTFACTOR = {270: 22.5}  # "page 270 font is slightly larger"
HEADER_BOX_PTSIZE_SCALE = 1.8
PAGES_1_2_GLYPH_Y_OFFSET = 100


@dataclass
class DrawOp:
    font_path: str
    code: int          # codepoint
    ptsize: float
    x: float           # GD pen origin x  (coord_x)
    y: float           # GD pen origin y  (coord_y, the baseline)


@dataclass
class WordBox:
    """The pixel box of one glyph on an ayah line, in the coordinate space of
    the page image rendered at this width.

    ``word`` is the 1-based position of the word within its ayah, in
    recitation order; the ayah-number marker and the pause / sajdah glyphs
    carry ``word == 0``.  One :class:`WordBox` is emitted per ayah-line glyph
    (see :func:`build_page`).
    """

    glyph_page_line_id: int
    sura: int
    ayah: int
    word: int
    line: int
    min_x: int
    max_x: int
    min_y: int
    max_y: int


@dataclass
class PagePlan:
    page: int
    width: int
    height: int
    ptsize: int
    ops: list[DrawOp] = field(default_factory=list)
    bboxes: list[WordBox] = field(default_factory=list)


@dataclass
class _Box:
    coord_x: float = 0.0
    coord_y: float = 0.0
    min_x: float = 0.0
    max_x: float = 0.0
    min_y: float = 0.0
    max_y: float = 0.0
    space: float = 0.0
    char_down: float = 0.0
    char_up: float = 0.0
    width: float = 0.0
    height: float = 0.0


class _PageCtx:
    """Mutable per-page state, mirroring the Perl ``$page`` hash."""

    def __init__(self, number: int, width: int, page_font: str):
        self.number = number
        self.width = width
        self.height = int(width * PHI)  # GD::Image->new truncates
        factor = PAGE_FONTFACTOR.get(number, DEFAULT_FONTFACTOR)
        self.ptsize = int(width / factor)
        self.margin_top = self.ptsize / 2
        self.coord_y = self.margin_top
        self.coord_x = 0.0
        self.font = page_font  # FONT_DEFAULT == QCF_BSML.TTF


def _resolve(glyph_font, line_font, page_font):
    return glyph_font or line_font or page_font


def _get_box(
    db: LayoutDB,
    page: _PageCtx,
    *,
    text_codes: list[int],
    line_font: str,
    glyph_font: str | None = None,
    glyph_ptsize: float | None = None,
) -> _Box:
    """``Quran::Image::Page::_get_box`` - bounding box + font metrics."""
    font = _resolve(glyph_font, line_font, page.font)
    ptsize = glyph_ptsize or page.ptsize  # line.ptsize is never set in the Perl

    char_up, char_down, space = gdcompat.text_metrics(font, ptsize, db.bsml_path)

    bb = gdcompat.string_brect(font, text_codes, ptsize)  # GD default 96x96 dpi
    min_x = min(bb[0], bb[6])
    max_x = max(bb[4], bb[2])
    min_y = min(bb[7], bb[5])
    max_y = max(bb[1], bb[3])

    width = max_x                     # Perl: $width = $max_x  (NOT max_x - min_x)
    height = max_y - min_y
    coord_x = (page.width - width) / 2  # horizontal centring

    return _Box(
        coord_x=coord_x,
        coord_y=page.coord_y,
        min_x=min_x,
        max_x=max_x,
        min_y=min_y,
        max_y=max_y,
        space=space,
        char_down=char_down,
        char_up=char_up,
        width=width,
        height=height,
    )


def _get_max_box(a: _Box, b: _Box) -> _Box:
    """Running union, exactly as ``_get_max_box`` (only 10 fields survive)."""
    return _Box(
        min_x=min(a.min_x, b.min_x),
        min_y=min(a.min_y, b.min_y),
        char_down=min(a.char_down, b.char_down),
        coord_x=min(a.coord_x, b.coord_x),
        coord_y=min(a.coord_y, b.coord_y),
        max_x=max(a.max_x, b.max_x),
        max_y=max(a.max_y, b.max_y),
        space=max(a.space, b.space),
        char_up=max(a.char_up, b.char_up),
        width=max(a.width, b.width),
        height=max(a.height, b.height),
    )


def _c_int(x: float) -> int:
    return math.trunc(x)


def build_page(db: LayoutDB, page_number: int, width: int) -> PagePlan:
    page = _PageCtx(page_number, width, db.bsml_path)
    plan = PagePlan(
        page=page_number,
        width=page.width,
        height=page.height,
        ptsize=page.ptsize,
    )

    lines = db.get_page_lines(page_number)
    word_index = db.word_index_map(page_number)

    for line in lines:
        line_box = _get_box(
            db, page, text_codes=line.codes, line_font=line.font
        )

        # "clamp the first line under the top margin"
        if page.coord_y <= page.margin_top and line_box.min_y < 0:
            page.coord_y -= line_box.min_y

        previous_w = 0.0
        page.coord_x = 0.0

        for glyph in line.glyphs:
            g_box = _get_box(
                db, page, text_codes=[glyph.code], line_font=line.font
            )

            use_coord_y = False
            y_offset = 0
            if line.type != "sura" and page_number in (1, 2):
                use_coord_y = True
                g_box.coord_y += PAGES_1_2_GLYPH_Y_OFFSET
                y_offset = PAGES_1_2_GLYPH_Y_OFFSET

            # sura header decorative box (drawn behind the sura name)
            if glyph.position == 1 and line.type == "sura":
                hb = db.get_ornament_glyph("header-box")
                hb_ptsize = page.ptsize * HEADER_BOX_PTSIZE_SCALE
                hb_box = _get_box(
                    db,
                    page,
                    text_codes=[hb.code],
                    line_font=line.font,        # BSML for a sura line
                    glyph_ptsize=hb_ptsize,
                )
                hb_box.coord_y = page.coord_y - hb_box.char_down
                plan.ops.append(
                    DrawOp(
                        font_path=_resolve(None, line.font, page.font),
                        code=hb.code,
                        ptsize=hb_ptsize,
                        x=hb_box.coord_x,   # use_coords => centred
                        y=hb_box.coord_y,
                    )
                )

            # advance pen
            page.coord_x = (
                page.coord_x + previous_w if page.coord_x else line_box.coord_x
            )
            previous_w = g_box.max_x

            draw_x = page.coord_x
            draw_y = page.coord_y

            if line.type == "sura":
                use_coord_y = True
                g_box.coord_y = page.coord_y + line_box.height / 7

            if use_coord_y:
                draw_y = g_box.coord_y

            # per-ayah bounding box (Perl: set_page_line_bbox, ayah lines only)
            if line.type == "ayah":
                min_x = _c_int(page.coord_x + g_box.min_x)
                max_x = _c_int(min_x + (g_box.max_x - g_box.min_x) + 0.5)
                min_y = _c_int(page.coord_y + g_box.min_y)
                max_y = _c_int(min_y + (g_box.max_y - g_box.min_y) + 0.5)
                if y_offset:
                    min_y += y_offset
                    max_y += y_offset
                # a few glyphs (some pause marks) carry negative-width control
                # boxes in the font; keep the invariant the client relies on.
                max_x = max(max_x, min_x)
                max_y = max(max_y, min_y)
                plan.bboxes.append(
                    WordBox(
                        glyph_page_line_id=glyph.page_line_id,
                        sura=glyph.sura or 0,
                        ayah=glyph.ayah or 0,
                        word=word_index.get(
                            (glyph.sura, glyph.ayah, glyph.ayah_pos), 0
                        ),
                        line=line.number,
                        min_x=min_x,
                        max_x=max_x,
                        min_y=min_y,
                        max_y=max_y,
                    )
                )

            plan.ops.append(
                DrawOp(
                    font_path=_resolve(None, line.font, page.font),
                    code=glyph.code,
                    ptsize=page.ptsize,
                    x=draw_x,
                    y=draw_y,
                )
            )

            line_box = _get_max_box(g_box, line_box)

        # advance baseline to the next line
        page.coord_y -= line_box.char_down
        k = PHI if page_number in (1, 2) else 2
        page.coord_y += k * line_box.char_up

    return plan


def build_layout(db: LayoutDB, page_number: int, width: int) -> dict:
    """The page's word geometry as a JSON-serialisable dict.

    Same coordinate space as the rendered image at ``width`` - the client
    scales every box by ``rendered_size / (width, height)``.  ``min_x <= max_x``
    and ``min_y <= max_y`` hold for every box by construction.
    """
    plan = build_page(db, page_number, width)
    return {
        "page": plan.page,
        "width": plan.width,
        "height": plan.height,
        "ptsize": plan.ptsize,
        "words": [
            {
                "sura": b.sura,
                "ayah": b.ayah,
                "word": b.word,
                "line": b.line,
                "min_x": b.min_x,
                "max_x": b.max_x,
                "min_y": b.min_y,
                "max_y": b.max_y,
            }
            for b in plan.bboxes
        ],
    }
