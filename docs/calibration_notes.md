# Calibration notes — matching libgd / GD::Text

Everything here was derived by running the original Perl pipeline in Docker
(`libgd 2.3.3`, `libgd-text-perl 0.86`, `FreeType 2.12.1`) and reading the
values back out.

## 1. Image geometry

* `width` — CLI (this migration: **1260**).
* `height = int(width * PHI)` where `PHI = (√5+1)/2`. GD truncates the float in
  `GD::Image->new`. width 1260 → **2038**. Same for every page including 270.
* `ptsize = int(width / fontfactor)`, `fontfactor = 21`, **page 270 → 22.5**
  (`int(1260/21)=60`, `int(1260/22.5)=56`). `fontdelta` is a dead variable (=1).
* `margin_top = ptsize / 2`.

## 2. libgd's metric space (`libgd/src/gdft.c`)

`gdImageStringFTEx` tracks **all** advances / control-boxes in a
resolution-independent **300 dpi** space (`#define METRIC_RES 300`), 26.6 fixed
point, using **hinted** `FT_LOAD_DEFAULT` glyph metrics
(`slot->metrics.horiAdvance / horiBearingX / horiBearingY / height`). The
returned `brect` is

```
scalex = hdpi / (64 * 300)          scaley = vdpi / (64 * 300)
brect[k] = (int)( origin + total_{min,max}.{x,y} * scale{x,y} )   # truncation
```

* `GD::Image->stringFT` with no `{resolution}` → `hdpi = vdpi = GD_RESOLUTION = 96`.
* The Perl draw call passes `resolution => '96,94'` → glyph bitmaps rasterised
  at 96×94, blitted at `(int)(x + bitmap_left), (int)(y - bitmap_top)`
  (pen = 0, because each glyph is drawn by its own `stringFT` call).

`gdcompat.string_brect()` / `gdcompat.glyph_bitmap()` implement exactly this.
No kerning: the QCF v1 fonts have no `kern` table (`FT_HAS_KERNING` = false).

## 3. `GD::Text` char_up / char_down / space

`GD::Text::_recalc` renders a **fixed** test string and takes
`char_up = -bb[7]`, `char_down = bb[1]`, `space` from a spaced-vs-unspaced
width delta. On the container's Perl the test string is `chr(0x21..0x7E)`
(94 chars, `n_spaces = 9`).

**Key finding:** libgd resolves those code points through the font's *first*
`FT_ENCODING_UNICODE` cmap subtable (charmap index 0, platform 0). For the QCF
fonts that subtable maps `0x21..0x7E` onto a handful of tiny/near-empty low
glyphs, so `char_up/char_down` come out **essentially font-independent**:

| ptsize | char_up | char_down | space |
|---|---|---|---|
| 60  | 62  | −8  | 3 |
| 56  | 57  | −8  | 3 |
| 108 (= 60·1.8, header-box) | 111 | −15 | 6 |

Exceptions (real glyphs *are* found): `P217` (space only), and genuinely
`P270` → `(92, 42, 3)` @60 / `(86, 39, 3)` @56 / `(166, 76, 6)` @108. The Perl
code separately patches `P105 / P237 / P552 / P554` to borrow `QCF_BSML`'s
values (font-export artefact) — reproduced.

Rather than re-derive libgd's cmap pick we captured every
`(font, ptsize)` pair straight from the container into
**`data/gdtext_metrics.json`** (`scripts/` one-liner, see git history) and look
it up; `gdcompat._live_text_metrics` is the fallback for unlisted widths.

## 4. Palette anti-alias ramp (`render.py`)

The reference PNGs are mode `P`: index 0 = transparent white, plus up to 8
opaque grey levels `{0, 31, 63, 95, 127, 159, 191, 223}`. libgd's palette AA
snaps a coverage `c` as

```
g   = 255 - c
bin = int(g / 32 + 0.5)          # 0..8
bin == 8            -> transparent (index 0)
else               -> opaque grey  max(0, bin*32 - 1)
```

## 5. What does NOT match, and why

Residual: ≈ 0.02–0.03 % of pixels differ by a single 32-level bin at glyph
edges. Cause: **freetype-py bundles FreeType 2.13**, the reference container
has **2.12** — the two rasterise the QCF TrueType hinting slightly
differently, so a few edge samples land on the other side of a quantisation
step. Layout, dimensions, centring, ayah-number placement and bounding boxes
are unaffected (bbox output is byte-exact). To eliminate it entirely, build
freetype-py against FreeType 2.12.1.

## 6. Bounding boxes

`glyph_page_line_bbox` rows are byte-exact vs the MySQL table **except** the
Perl code drops the first box per run (its first `set_page_line_bbox` call only
prepares statements). This port keeps that box; document, don't replicate.
