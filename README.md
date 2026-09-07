# quran-image

**On-demand Qur'an Mushaf page-image rendering server.**

A stateless [FastAPI](https://fastapi.tiangolo.com/) service that renders
**Madani Mushaf pages on demand, at the pixel width the asking device needs**,
from the King Fahd Complex "QCF v1" fonts — and, from the same layout pass, a
JSON of each page's **per-word pixel boxes** for word-level highlighting,
tap-selection and notes. Results are cached in RAM and on disk.

No Perl, no MySQL, no pre-rendered image set — the repository ships only the
source assets (fonts + a small layout database) and computes every page image
from them.

<p align="left">
  <img alt="Python" src="https://img.shields.io/badge/python-%E2%89%A5%203.10-blue">
  <img alt="Framework" src="https://img.shields.io/badge/FastAPI-stateless-009688">
  <img alt="Tests" src="https://img.shields.io/badge/tests-52%20passing-brightgreen">
  <img alt="Style" src="https://img.shields.io/badge/lint-ruff-orange">
  <img alt="Licence" src="https://img.shields.io/badge/licence-AGPL--3.0-blue">
</p>

---

## Contents

- [Why](#why)
- [How it works](#how-it-works)
- [Getting started](#getting-started)
- [API](#api)
- [Configuration](#configuration)
- [Project layout](#project-layout)
- [Fidelity](#fidelity)
- [Testing](#testing)
- [Deployment](#deployment)
- [Client integration](#client-integration)
- [Acknowledgements & licence](#acknowledgements--licence)

---

## Why

The Mushaf page is a fixed golden-ratio portrait rectangle — the calligraphic
layout is never altered. The only thing a device controls is the **pixel
width** the page is rasterised at. Shipping one pre-rendered image set means
one resolution for every screen; shipping many means a huge asset bundle.

Instead this service keeps the ~100 MB of source fonts + a 9 MB layout DB and
renders each `⟨page, width, format⟩` the first time it is asked for, then caches
it (RAM → disk). One width-parametric algorithm; given a page and a width the
output is fully determined and, at width 1260, **byte-for-byte identical** to
the historically shipped asset.

## How it works

```
┌────────────┐   GET /v1/pages/5?w=1170&fmt=webp&v=<ver>             ┌──────────────────────────┐
│   device   │ ───────────────────────────────────────────────────▶ │   FastAPI  (stateless)   │
│  w = logical                                                      │                          │
│      × dpr  │   200  image/webp  + ETag + Content-Location        │  RAM LRU ─▶ disk cache    │
│            │ ◀─────────────────────────────────────────────────── │      │ miss               │
│  (device    │                                                     │  single-flight (1/key)   │
│   caches    │   re-fetch = If-None-Match ─▶ 304                   │      │                     │
│   the file) │                                                     │  ProcessPool ─▶ build_page│
└────────────┘                                                      │       + render + encode   │
                                                                    └──────────────┬───────────┘
                                                     source assets (loaded once per pool process):
                                                     data/layout.sqlite · data/gdtext_metrics.json · fonts/QCF_*.TTF
```

1. **Width negotiation** — the request sends `?w=<physical_px>` (or `sw`+`dpr`);
   the width snaps *up* to the next rung of a fixed 18-rung ladder, so the cache
   holds ~18 variants per page and the device only ever downscales (crisp, never
   blurry).
2. **Layout** (`quran_image/layout.py`) — pure geometry: a running baseline
   walks down the page, each line is measured for horizontal centring, glyphs
   are laid left-to-right. Emits draw ops + a `WordBox` per ayah-line glyph.
3. **GD ⇄ FreeType parity** (`quran_image/gdcompat.py`) — reproduces libgd's
   resolution-independent 300 dpi metric arithmetic exactly, which is why the
   layout matches GD and not merely "looks Arabic". Full derivation in
   [`docs/calibration_notes.md`](docs/calibration_notes.md).
4. **Render** (`quran_image/render.py`) — `max`-composites FreeType's 8-bit
   coverage, quantises it the libgd way, emits a tiny mode-`P` PNG (`tRNS`) or a
   lossless WebP (~10–20 % smaller, byte-identical decoded).
5. **Serve** (`quran_image/service.py` + `server.py`) — RAM LRU → disk cache →
   single-flight coalescing → process pool. A cache miss that has *some* other
   rung of the page cached returns the nearest one immediately and renders the
   exact rung in the background; only a true cold start blocks on one render.

## Getting started

### 1. What it needs

| requirement | notes |
|---|---|
| **Python ≥ 3.10** | standard library covers `sqlite3`, `argparse`, `concurrent.futures` |
| **Runtime packages** | `Pillow`, `freetype-py`, `numpy`, `fastapi`, `uvicorn` — `pip install -r requirements.txt` |
| **`fonts/QCF_P001..P604.TTF` + `QCF_BSML.TTF`** | King Fahd Complex "QCF v1" fonts (in `fonts/`) |
| **`data/gdtext_metrics.json`** | GD::Text metric table (in `data/`) |
| **`data/layout.sqlite`** | built once from the SQL dump — step 2 |

### 2. Install + build the layout DB (once)

```bash
git clone https://github.com/<your-username>/quran-image.git
cd quran-image
python -m pip install -r requirements.txt

# builds data/layout.sqlite from the mysqldump in ../quran.com-images-master/sql
# (skip if data/layout.sqlite is already present)
python scripts/import_layout.py --sql-dir /path/to/quran.com-images/sql
```

### 3. Start the server

```bash
# development — single process, render pool auto-sized to the CPU count
python -m quran_image.server --port 8080

# development — also pre-render pages 1–20 at the common widths
python -m quran_image.server --port 8080 --warm 1..20

# production — multiple API workers
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
curl "http://localhost:8080/v1/pages/5/layout?w=1170"     # per-word pixel boxes
```

Interactive API docs at `http://localhost:8080/docs`.

## API

| endpoint | purpose |
|---|---|
| `GET /v1/manifest` | bootstrap: `asset_version`, page range, formats, the width ladder, caching rules |
| `GET /v1/pages/{page}` · `HEAD` | render/serve one page image (`w` \| `sw`+`dpr`, `fmt=png\|webp`, `max_w`, `v`) |
| `POST /v1/pages/{page}` | same, taking raw screen metrics as a JSON body |
| `GET /v1/pages/{page}/layout` · `HEAD` | the page's per-word pixel boxes, in the image's coordinate space |
| `GET /v1/stats` | render / hit / coalesce / fallback counters + derived rates |
| `GET /healthz` | load-balancer probe + asset status |

**Image response** carries a strong `ETag`, `Content-Location` (canonical
version-stamped URL), `Cache-Control: …immutable` when `v` matches the current
`asset_version`, and `X-Cache: HIT-MEM | HIT-DISK | MISS | FALLBACK`.
`If-None-Match` → `304`.

**Layout response:**

```json
{
  "page": 3, "width": 1080, "height": 1747, "ptsize": 51,
  "words": [
    { "sura": 2, "ayah": 6, "word": 1, "line": 1,
      "min_x": 900, "max_x": 1010, "min_y": 27, "max_y": 120 }
  ]
}
```

One entry per ayah-line glyph. `word` is the 1-based position of the word within
its ayah in recitation order; the ayah-number roundel and pause/sajdah marks
carry `word: 0`. Scale every box by `rendered_size / (width, height)` to map it
onto the displayed image.

The full request/response contract — every header, error code (`400`/`404`/
`422`/`503`), and the nearest-rung `FALLBACK` + background-backfill behaviour —
is described in the interactive docs at `/docs` and in the route handlers in
`quran_image/server.py`.

## Configuration

All optional — every asset path auto-resolves to the repo layout.

| env var | default | meaning |
|---|---|---|
| `QURAN_CACHE_DIR` | `./cache` | server-side image cache (mount a shared volume for multi-node) |
| `QURAN_WORKERS` | CPU count | render process-pool size |
| `QURAN_BG_WORKERS` | = `QURAN_WORKERS` | background backfill thread-pool size |
| `QURAN_BG_MAX_QUEUED` | `64` | cap on in-flight background jobs |
| `QURAN_DISK_CACHE_BYTES` | 2 GiB | disk-cache eviction threshold (LRU by atime) |
| `QURAN_FONTS_DIR` | `./fonts` | QCF `*.TTF` directory |
| `QURAN_DB` | `./data/layout.sqlite` | layout database |
| `QURAN_METRICS` | `./data/gdtext_metrics.json` | GD::Text metric table |
| `QURAN_ASSET_VERSION` | *(content hash)* | pin the cache-key version to a release tag so every node/device agrees |

## Project layout

```
quran_image/
  server.py        FastAPI app + routes            service.py    RenderService (cache tiers + process pool + backfill)
  imagecache.py    MemoryLRU / DiskCache / SingleFlight
  dimensions.py    screen metrics → RenderSpec      assets.py     asset bundle + content version
  render.py        coverage → image + encode        layout.py     width-parametric page geometry
  gdcompat.py      libgd / GD::Text on FreeType     db.py         layout.sqlite reader
scripts/import_layout.py   SQL dump → data/layout.sqlite (one-time build)
data/            layout.sqlite, gdtext_metrics.json
fonts/           QCF_P001..P604 + QCF_BSML
tests/           test_server.py, test_pipeline.py, refs/
docs/            calibration_notes.md
```

Layers depend strictly downward: `server → service → {imagecache, render, layout}
→ {gdcompat, db}`. The layout pass touches no pixels, so it is unit-tested and
diffed against the reference output directly.

## Fidelity

At width 1260 the output is **byte-for-byte identical** to the historically
shipped asset — `tests/test_server.py::test_real_render_width_1260_matches_reference`
checks pages 1 and 50 against `tests/refs/`. Dimensions, line positions,
centring and ayah-number placement are exact at every width; only `width`
changes the geometry.

Against the original Perl + FreeType 2.12 container, ≈0.003–0.025 % of edge
pixels differed by one 32-level anti-alias step — a rasteriser-version effect
(freetype-py bundles FreeType 2.13), not a layout difference. Bounding-box
output is byte-exact.

## Testing

```bash
python -m pip install -r requirements-dev.txt   # pytest, httpx
python -m pytest -q
```

The API-layer tests inject a fake renderer and need no assets. The layout and
real-render tests skip automatically when `data/layout.sqlite` or the fonts are
absent.

## Deployment

The API process holds no per-request state. Run N uvicorn nodes behind a load
balancer, point `QURAN_CACHE_DIR` at a shared filesystem (writes are atomic;
the render step re-checks the cache first, so cross-node duplicate work is a
narrow race window). A CDN in front + the `immutable`, version-stamped URLs
mean most devices never reach the origin twice.

- **No auth** — deploy on a private network or behind an authenticating
  gateway / CDN.
- All inputs are constrained: `page` is bounds-checked, `w`/`sw`/`dpr` are
  clamped, `fmt` is regex-limited, `v` is used only for a cache-control
  decision. No client string reaches the filesystem path. No `eval`, shell, or
  network egress.

## Client integration

A client should:

1. compute `physical_w = logical_w × devicePixelRatio` and snap it to the
   manifest ladder locally, so the request already asks for the rung it will
   get;
2. per page, fetch the image and its `/layout` in parallel
   (`GET /v1/pages/{page}?w=&fmt=webp&v=` and `GET /v1/pages/{page}/layout?w=&v=`);
3. write both to disk immediately, keyed by `w` + `asset_version`, storing the
   image `ETag`;
4. on later opens read the local files, revalidating in the background with
   `If-None-Match` → `304`;
5. treat an `asset_version` change as a full cache rotation — every key changes
   at once, so the old directory can be pruned.

## Acknowledgements & licence

**Server code** — [GNU AGPL-3.0](LICENSE). It is copyleft *and* covers network
use: if you run a modified version of this service so that others can reach it
over a network, you must offer those users the modified source. Unmodified
private use and self-hosting are unrestricted.

To apply the notice to your own copy, add near the top of the source files:

```
quran-image — on-demand Qur'an Mushaf page-image rendering server
Copyright (C) 2026 ahmeddhere

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version. See <https://www.gnu.org/licenses/>.
```

**Bundled assets** — the QCF v1 fonts in `fonts/` and the glyph-layout data in
`data/` are assets of the
[King Fahd Glorious Qur'an Printing Complex](https://qurancomplex.gov.sa/),
redistributed via the
[`quran.com-images`](https://github.com/quran/quran.com-images) project. They
are **not** covered by this repository's AGPL licence and carry their own terms
— review them before redistributing.
