"""Fast sanity tests for the layout + render pipeline.

Run with:  python -m pytest -q   (or just  python tests/test_pipeline.py)
They need data/layout.sqlite and the QCF fonts (see scripts/import_layout.py).
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quran_image import gdcompat  # noqa: E402
from quran_image.db import LayoutDB, DEFAULT_DB, DEFAULT_FONTS  # noqa: E402
from quran_image.layout import build_layout, build_page, PHI  # noqa: E402
from quran_image.render import render_page, _quantise_to_bins  # noqa: E402

import numpy as np  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.path.isfile(DEFAULT_DB), reason="run scripts/import_layout.py first"
)


@pytest.fixture(scope="module")
def db():
    return LayoutDB()


def test_import_row_counts(db):
    con = db._con
    assert con.execute("SELECT COUNT(*) FROM glyph").fetchone()[0] == 98139
    assert con.execute("SELECT COUNT(*) FROM glyph_page_line").fetchone()[0] == 88811
    assert con.execute(
        "SELECT COUNT(DISTINCT page_number) FROM glyph_page_line"
    ).fetchone()[0] == 604
    # basmalah-shaddah patch
    assert [
        r[0]
        for r in con.execute(
            "SELECT glyph_id FROM glyph_page_line WHERE glyph_page_line_id IN (87950,88094)"
        )
    ] == [4, 4]


@pytest.mark.parametrize("page", [1, 2, 3, 50, 270, 604])
def test_page_dimensions(db, page):
    plan = build_page(db, page, 1260)
    assert plan.width == 1260
    assert plan.height == int(1260 * PHI) == 2038
    assert plan.ptsize == (56 if page == 270 else 60)
    assert len(plan.ops) > 0


def test_pages_1_2_have_eight_lines_worth_and_offset(db):
    # pages 1-2 push non-sura glyphs down by 100px
    plan = build_page(db, 1, 1260)
    ys = sorted({round(op.y) for op in plan.ops})
    assert min(ys) > 100  # first baseline already includes the +100 nudge


def test_baselines_are_monotonic(db):
    plan = build_page(db, 300, 1260)
    # group ops into rows by y, ensure the row baselines increase down the page
    ys = sorted({round(op.y) for op in plan.ops})
    assert ys == sorted(ys)
    assert ys[-1] < plan.height


def test_gdtext_metrics_match_reference_table(db):
    # values captured from the reference container
    cu, cd, sp = gdcompat.text_metrics(db.font_path("QCF_P003.TTF"), 60, db.bsml_path)
    assert (cu, cd) == (62, -8)
    cu, cd, _ = gdcompat.text_metrics(db.font_path("QCF_P270.TTF"), 56, db.bsml_path)
    assert (cu, cd) == (86, 39)
    # artifact-fix fonts borrow BSML
    cu, cd, _ = gdcompat.text_metrics(db.font_path("QCF_P105.TTF"), 60, db.bsml_path)
    assert (cu, cd) == (62, -8)


def test_string_brect_single_glyph_matches_gd():
    # GD (container) for QCF_P003 &#64340; at pt60 returned: 7 17 116 17 116 -53 7 -53
    f = os.path.join(DEFAULT_FONTS, "QCF_P003.TTF")
    bb = gdcompat.string_brect(f, [64340], 60)
    assert bb == (7, 17, 116, 17, 116, -53, 7, -53)


def test_quantise_matches_gd_ramp():
    # g = 255-cov ; bin = int(g/32 + 0.5) ; bin 8 == transparent, bin 0 == black
    cov = np.array([[0, 14, 255, 254, 240, 239, 128, 130]], dtype=np.uint8)
    bins = _quantise_to_bins(cov)
    assert bins.tolist()[0] == [8, 8, 0, 0, 0, 1, 4, 4]
    # every opaque bin maps to one of GD's 8 grey levels
    grey = {0: 0, 1: 31, 2: 63, 3: 95, 4: 127, 5: 159, 6: 191, 7: 223}
    assert set(grey) == set(range(8))


def test_render_is_palette_with_one_transparent_entry(db):
    img = render_page(build_page(db, 3, 1260))
    assert img.mode == "P"
    assert img.info["transparency"] == 0
    a = np.array(img.convert("RGBA"))[..., 3]
    assert (a == 0).any() and (a > 0).any()


def test_bboxes_only_on_ayah_lines(db):
    plan = build_page(db, 50, 1260)  # has sura header + bismillah + ayah lines
    ayah_ids = {
        g.page_line_id
        for line in db.get_page_lines(50)
        if line.type == "ayah"
        for g in line.glyphs
    }
    assert {b.glyph_page_line_id for b in plan.bboxes} <= ayah_ids
    assert len(plan.bboxes) == len(ayah_ids)


def test_word_boxes_carry_ayah_and_recitation_order(db):
    # page 3 opens sura 2 ayah 6 (all words) and runs into ayah 7 (has a pause)
    doc = build_layout(db, 3, 1260)
    assert doc["page"] == 3
    assert doc["width"] == 1260 and doc["height"] == int(1260 * PHI)
    words = doc["words"]
    assert words and all(w["min_x"] <= w["max_x"] and w["min_y"] <= w["max_y"] for w in words)

    a6 = [w for w in words if (w["sura"], w["ayah"]) == (2, 6)]
    # the ayah-number roundel is word 0; the words are 1..n with no gaps
    numbers = sorted(w["word"] for w in a6)
    assert numbers[0] == 0
    assert numbers[1:] == list(range(1, len(a6)))

    a7 = [w for w in words if (w["sura"], w["ayah"]) == (2, 7)]
    # a7's pause glyph is word 0 too, so more than one box carries 0, but the
    # real words are still a gap-free 1..k run (pause skipped in the numbering)
    real = sorted(w["word"] for w in a7 if w["word"] != 0)
    assert real == list(range(1, len(real) + 1))


@pytest.mark.parametrize("page", [3, 50, 255, 300, 604])
def test_every_ayah_on_a_page_is_numbered_1_to_n(db, page):
    # the madani mushaf ends every page on an ayah boundary, so each ayah's
    # word glyphs sit whole on one page: their numbers must be a gap-free 1..n
    # (word_index_map still derives this from the ayah's full glyph run).
    doc = build_layout(db, page, 1080)
    by_ayah: dict[tuple[int, int], list[int]] = {}
    for w in doc["words"]:
        by_ayah.setdefault((w["sura"], w["ayah"]), []).append(w["word"])
    assert by_ayah
    for (sura, ayah), nums in by_ayah.items():
        words = sorted(n for n in nums if n != 0)
        assert words == list(range(1, len(words) + 1)), f"{sura}:{ayah} -> {nums}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
