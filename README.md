# Qur'an Mushaf page-image rendering server

A FastAPI service that renders **Madani Mushaf pages on demand at a device's
screen resolution** from the King Fahd Complex "QCF v1" fonts, and caches the
result (RAM + disk). No Perl, no MySQL, no pre-rendered image set.

## Getting started

### 1. What it needs

| requirement | notes |
|---|---|
| **Python ≥ 3.10** | standard library only for `sqlite3`, `argparse`, `concurrent.futures` |
| **Runtime packages** | `Pillow`, `freetype-py`, `numpy`, `fastapi`, `uvicorn` — `pip install -r requirements.txt` |
| **`fonts/QCF_P001..P604.TTF` + `QCF_BSML.TTF`** | the King Fahd Complex "QCF v1" fonts (already in `fonts/`) |
| **`data/gdtext_metrics.json`** | GD::Text metric table (already in `data/`) |
| **`data/layout.sqlite`** | built once from the SQL dump — see step 2 |

No database server, no Perl, no pre-rendered images.

### 2. Install + build the layout DB (once)

```bash
cd quran-image
python -m pip install -r requirements.txt

# builds data/layout.sqlite from ../quran.com-images-master/sql
# skip if data/layout.sqlite already exists
python scripts/import_layout.py
```

### 3. Start the server

```bash
# development (single process, auto-picks CPU count for the render pool)
python -m quran_image.server --port 8080

# development, pre-rendering pages 1–20 at the common widths
python -m quran_image.server --port 8080 --warm 1..20

# production (multiple API workers)
uvicorn quran_image.server:app --host 0.0.0.0 --port 8080 --workers 4
```

On **Windows PowerShell**, set env vars before the command:

```powershell
$env:QURAN_WORKERS = "8"
uvicorn quran_image.server:app --host 0.0.0.0 --port 8080 --workers 4
```

### 4. Verify

```bash
curl http://localhost:8080/healthz                        # {"status":"ok", ...}
curl "http://localhost:8080/v1/pages/5?w=1170&fmt=webp" -o page5.webp
curl "http://localhost:8080/v1/pages/5/layout?w=1170"     # its per-word pixel boxes
```

Interactive API docs at `http://localhost:8080/docs`.

**Full API, deployment, env-var reference, and the device-cache contract:
[`SERVER.md`](SERVER.md).**

---

## How rendering works

One width-parametric algorithm, ported step-for-step from the reference libgd +
GD::Text pipeline. Given a page and a width it is fully determined.

### Data — `scripts/import_layout.py` → `data/layout.sqlite`

| table | rows | role |
|---|---|---|
| `glyph_type` | 16 | glyph classes: `word`, `end` (ayah number), `pause`, `sura`, `header-box`, `bismillah-*`, … |
| `glyph` | 98 139 | `font_file` + `glyph_code` (a codepoint) + `page_number` for every glyph |
| `glyph_page_line` | 88 811 | every glyph placed on a page: `(page, line, position)` + `line_type ∈ {sura, ayah, bismillah}` |
| `glyph_ayah` | 88 246 | glyph → `(sura, ayah, position)` |

Each page `N` has its own font `QCF_P{N:03}.TTF` in which codepoint `64336+k`
(U+FB50 block) is the k-th whole-word ligature on that page. `QCF_BSML.TTF`
holds the sura-name, bismillah and ornament glyphs. The
`sql/03-basmallah-shaddah.sql` patch (suras 95/97) is applied during import.

### Layout — `quran_image/layout.py` (pure geometry, no pixels)

* `height = int(width · φ)` (φ = golden ratio); `ptsize = int(width / 21)` —
  **page 270 uses `/22.5`**; `margin_top = ptsize/2`.
* A running baseline `coord_y` walks down the page. Per line: measure the line's
  bounding box for horizontal centring; clamp the first line under the top
  margin; lay glyphs left-to-right advancing by each glyph's measured right edge;
  **sura lines** draw the `header-box` ornament (`ptsize·1.8`) behind the name
  and raise the text by `line_height/7`; **pages 1–2** push every non-sura glyph
  down 100 px (the large opening spread); advance
  `coord_y += (φ if page ≤ 2 else 2)·char_up − char_down`.
* Emits an ordered list of `DrawOp(font, codepoint, ptsize, x, y)` plus a
  `WordBox` per ayah-line glyph — `(sura, ayah, word, line)` + pixel rect, where
  `word` is the 1-based recitation index (`0` for the ayah-number marker and the
  pause/sajdah glyphs; numbering comes from `db.word_index_map`, computed over
  each ayah's full glyph run). `build_layout()` serves these as the
  `GET /v1/pages/{page}/layout` JSON the reader draws word highlights, notes and
  tap-selection from.

### GD ⇄ FreeType parity — `quran_image/gdcompat.py`

libgd computes every advance and control-box in a resolution-independent
**300 dpi** space with hinted `FT_LOAD_DEFAULT` metrics, scales to the target dpi
and **truncates to int** (`libgd/src/gdft.c`). `gdcompat.string_brect()`
reproduces that arithmetic exactly — this is why the layout matches GD and not
merely "looks Arabic". The `GD::Text` `char_up`/`char_down`/`space` values are
captured in `data/gdtext_metrics.json` (`_live_text_metrics` is the fallback for
unlisted widths). Full derivation: [`docs/calibration_notes.md`](docs/calibration_notes.md).

### Render — `quran_image/render.py`

`max`-composites FreeType's 8-bit coverage (overlapping edges must not darken),
then quantises the libgd way: `g = 255−c; bin = int(g/32 + 0.5); bin==8 →
transparent, else grey max(0, bin·32−1)`. Output is a mode-`P` PNG with a `tRNS`
of one entry (or lossless `exact` WebP, ~10–20 % smaller, byte-identical decoded).

---

## Fidelity

At width 1260 the output is **byte-for-byte identical** to the historically
shipped asset — `tests/test_server.py::test_real_render_width_1260_matches_reference`
checks pages 1 and 50 against `tests/refs/`. Dimensions, line positions, centring
and ayah-number placement are exact at every width; only `width` changes the
geometry. (Against the original Perl + FreeType 2.12 container, ≈0.003–0.025 % of
edge pixels differed by one 32-level AA step — a rasteriser-version effect from
freetype-py bundling FreeType 2.13, not a layout difference.)

---

## Layout

```
quran_image/
  server.py        FastAPI app + routes            service.py    RenderService (cache + pool)
  imagecache.py    MemoryLRU / DiskCache / SingleFlight
  dimensions.py    screen metrics → RenderSpec      assets.py     asset bundle + content version
  render.py        coverage → image + encode        layout.py     width-parametric page geometry
  gdcompat.py      libgd / GD::Text in FreeType     db.py         layout.sqlite reader
scripts/import_layout.py   SQL dump → data/layout.sqlite (one-time build)
data/            layout.sqlite, gdtext_metrics.json     fonts/   QCF_P001..P604 + QCF_BSML
tests/           test_server.py, test_pipeline.py, refs/
docs/            calibration_notes.md
```

## Tests

```bash
python -m pip install -r requirements-dev.txt   # pytest, httpx
python -m pytest -q
```

The API-layer tests use a fake renderer and need no assets; the layout and
real-render tests skip automatically when `data/layout.sqlite` or the fonts are
absent.

Prerequisites and run instructions: [Getting started](#getting-started) above.
The QCF fonts and SQL dump are King Fahd Complex assets — see the upstream
`quran.com-images` licence.
