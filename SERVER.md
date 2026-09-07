# On-demand Qur'an page-image service

Keeps only the **source assets** (fonts + layout DB) and renders each Madani
Mushaf page **on request, at the pixel width the asking device needs** — the
page image and, from the same layout pass, a JSON of its per-word pixel boxes
(`/layout`) — then caches the result (RAM → disk). Stateless; scales
horizontally.

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

---

## Design

| requirement | how it is met |
|---|---|
| render for the device's resolution | client sends `?w=<physical_px>` (or `sw`+`dpr`); `w` snaps to a canonical rung, then `build_page(db, page, w)` lays the page out at that width (`ptsize = w/21`, `height = ⌊w·φ⌋`) and rasterises it |
| return immediately | the response **is** the image; a cached rung is served straight from RAM/disk. If the exact rung is missing **but another rung of the same page is cached**, that nearer rung is returned at once (`X-Cache: FALLBACK`, `X-Fallback: 1`) and the exact rung is rendered in a **background** thread pool; the client never waits. Only a *true cold start* — no rung of the page cached at all — blocks on one render (`X-Cache: MISS`). |
| server-side cache | RAM `MemoryLRU` → size-capped `DiskCache`, key `⟨asset_version⟩/⟨page⟩/⟨width⟩/⟨fmt⟩` |
| fast + low memory | warm `LayoutDB` + FreeType glyph caches per pool process; rendering in a **separate process** so the `w×h` coverage buffer is freed on return; the API process only ever holds encoded bytes; `max_concurrent_renders` caps peak RAM |
| visually identical | one width-parametric layout algorithm — only `width` varies; at `w = 1260` the output is **byte-for-byte** the historically-shipped asset (`tests/test_server.py::test_real_render_width_1260_matches_reference`) |
| many devices / sizes, concurrently | each `⟨page,width,fmt⟩` is an independent key rendered in parallel across the pool; identical in-flight requests coalesce (`SingleFlight`); the disk cache uses atomic writes so it is shareable across nodes |
| device-side cache | strong `ETag` + `Content-Location` (canonical, version-stamped URL) + `Cache-Control: immutable` when the request's `v` matches the current `asset_version`; otherwise revalidate with `If-None-Match` → `304` |

### Dimension negotiation

The Mushaf page is always the golden-ratio portrait rectangle — the calligraphic
layout, never altered. The only device-controlled knob is the **pixel width**.
Requests **snap up** to the next rung of a fixed ladder:

```
360 480 540 640 720 810 900 1000 1080 1170 1242 1260 1290 1350 1440 1620 1792 2048
```

* snapping *up* → the device only ever **downscales** what it receives — crisp, never blurry;
* 18 rungs → the cache holds 18 width variants per page, not thousands;
* `1260` is a rung so a device at that width gets a bit-exact copy of the legacy asset.

`GET /v1/manifest` publishes the ladder, page range, formats and `asset_version`.

### Fill-on-demand: nearest-rung fallback + background backfill

The first device to want a given `⟨page, width, fmt⟩` should not pay the whole
render latency. So on a cache miss the service:

1. looks for **any other already-cached rung** of that page — preferring the
   closest rung **≥** the requested width (the device then only downscales, so
   text stays crisp), then the closest rung below;
2. if one exists, returns it **immediately** with `X-Cache: FALLBACK`,
   `X-Fallback: 1`, `X-Target-Width: <requested rung>`, `X-Render-Width: <rung
   actually served>`, `Content-Location` pointing at the served rung's canonical
   URL, and **`Cache-Control: no-store`** — the fallback is transient, so the
   device re-requests the exact width, which is a fast-path hit once the
   background render completes (typically a second or two later);
3. enqueues the exact rung on an in-process background pool
   (`QURAN_BG_WORKERS`, default = render-pool size; at most `QURAN_BG_MAX_QUEUED`
   jobs in flight, else the job is dropped and simply retried on the next
   request). The CPU work still runs in the render `ProcessPool` and is still
   bounded by `max_concurrent_renders`; a background job that can't get a slot
   is retried later. `SingleFlight` + a disk re-check mean the exact rung is
   never rendered twice, even across a concurrent cold-start.

Only a **true cold start** — *no* rung of that page cached anywhere — falls back
to a single blocking render (`X-Cache: MISS`).

`GET /v1/pages/{page}/layout` uses the same fallback + backfill. A fallback
layout JSON is self-consistent (its body carries the `width`/`height` its boxes
are in) and the client scales boxes to the display rectangle proportionally, so
an image/layout width mismatch during the backfill window is at most a
sub-pixel highlight offset.

---

## API

### `GET /v1/manifest`
Bootstrap data: `asset_version`, `pages{min,max,count}`, `formats`,
`width{min,max,default,ladder}`, `aspect_ratio`, caching rules.

### `GET /v1/pages/{page}`  ·  `HEAD` supported

| query | meaning |
|---|---|
| `w` | explicit **physical** render width (px). Wins over `sw`/`dpr`. |
| `sw`, `sh`, `dpr` | logical screen width/height + density; server uses `sw × dpr` |
| `fmt` | `png` (default; tiny palette + `tRNS`) or `webp` (lossless + `exact`, ~10–20 % smaller, decodes to byte-identical RGBA) |
| `max_w` | client-imposed hard cap on render width |
| `v` | `asset_version` from the manifest — when it matches, the response is `Cache-Control: …immutable` |

**`200`** — the image bytes, plus:

```
ETag: "<sha256[:32] of the bytes>"
Cache-Control: public, max-age=31536000, immutable      (v matches; else max-age=86400 + stale-while-revalidate; FALLBACK → no-store)
Content-Location: /v1/pages/5?w=1170&fmt=webp&v=<ver>   canonical URL of the rung actually served
X-Cache: HIT-MEM | HIT-DISK | MISS | FALLBACK
X-Render-Width: 1170   X-Requested-Width: 1082   X-Image-Height: 1893   X-Asset-Version: <ver>
X-Fallback: 1   X-Target-Width: 1242          (only on FALLBACK: exact rung still rendering in the background)
```

`If-None-Match: "<etag>"` → **`304`** (no body). Out-of-range page → `404`;
bad params → `400`/`422`; assets missing → `503`; render queue saturated →
`503` + `Retry-After`.

### `GET /v1/pages/{page}/layout`  ·  `HEAD` supported

The page's **per-word pixel boxes**, in the coordinate space of the image the
same request would return — fetch it alongside the image and scale the boxes by
`rendered_size / (width, height)`. Takes the same width knobs (`w` | `sw`+`dpr`,
`max_w`) and `v`; `fmt` is irrelevant to geometry. Width negotiation is
identical to the image route, so the two always agree on the rung.

```json
{
  "page": 3, "width": 1080, "height": 1747, "ptsize": 51,
  "words": [
    { "sura": 2, "ayah": 6, "word": 1, "line": 1,
      "min_x": 900, "max_x": 1010, "min_y": 27, "max_y": 120 }
  ]
}
```

One entry per glyph on an ayah line. `word` is the 1-based position of the word
within its ayah in recitation order; the ayah-number roundel and the
pause/sajdah marks carry `word: 0`. `min_x ≤ max_x` and `min_y ≤ max_y` always
hold. Same `ETag` / `Cache-Control` / `Content-Location` / `X-Cache` contract as
the image route — including the nearest-rung `FALLBACK` + background backfill —
and `If-None-Match` → `304`.

### `POST /v1/pages/{page}`
Same result, metrics in the body:

```json
{ "screen": { "width_px": 412, "height_px": 915, "dpr": 2.625 }, "format": "webp" }
```
or `{ "width": 1170, "format": "png" }`.

### `GET /v1/stats` · `GET /healthz`
Counters (renders / hits / coalesced / `fallback` / `bg_enqueued` / `bg_rendered`
/ `bg_failed` / `bg_dropped`), a `derived` block (`cache_hit_rate` — should climb
toward 1 as the cache fills; `mean_fallback_distance_px` — how far served rungs
sit from the requested one, a hint that the ladder needs tuning;
`mean_bg_render_ms`), cache sizes, in-flight counts; health + asset status for
load-balancer probes. Interactive docs at `/docs`.

---

## Running it

```bash
pip install -r requirements.txt

# assets auto-resolve to  quran-image/fonts/  and  quran-image/data/layout.sqlite
# (build the DB once, if absent:  python scripts/import_layout.py)

# production
QURAN_CACHE_DIR=/var/cache/quran QURAN_WORKERS=8 \
  uvicorn quran_image.server:app --host 0.0.0.0 --port 8080 --workers 4

# dev, warming the first 20 pages at the common widths
python -m quran_image.server --port 8080 --workers 4 --warm 1..20
```

| env var | default | meaning |
|---|---|---|
| `QURAN_CACHE_DIR` | `./cache` | server-side image cache (mount a shared volume for multi-node) |
| `QURAN_WORKERS` | CPU count | render process-pool size |
| `QURAN_BG_WORKERS` | = `QURAN_WORKERS` | background backfill thread-pool size (renders the exact rung after a FALLBACK) |
| `QURAN_BG_MAX_QUEUED` | 64 | cap on in-flight background jobs; excess are dropped and retried on the next request |
| `QURAN_DISK_CACHE_BYTES` | 2 GiB | disk-cache LRU-by-atime eviction threshold |
| `QURAN_FONTS_DIR` | `quran-image/fonts` | QCF `*.TTF` directory |
| `QURAN_DB` | `quran-image/data/layout.sqlite` | layout database |
| `QURAN_METRICS` | `quran-image/data/gdtext_metrics.json` | GD::Text metric table |
| `QURAN_ASSET_VERSION` | *(content hash)* | pin the cache-key version to a release tag so every node/device agrees |

### Scaling out
The API process holds no per-request state. Run N uvicorn nodes behind a load
balancer, point `QURAN_CACHE_DIR` at a shared filesystem (or swap `DiskCache`
for an object store) — writes are atomic (`tmp` + `os.replace`) and the render
step re-checks the cache before starting, so cross-node duplicate work is limited
to a narrow race window. A CDN in front + the `immutable`, version-stamped URLs
mean most devices never reach the origin twice.

### Security notes
* No auth — deploy on a private network / behind an authenticating gateway or CDN.
* All inputs are constrained: `page` bounds-checked (`404`), `w`/`sw`/`dpr`
  clamped by `negotiate()` (`400` on nonsense), `fmt` regex-limited, `v` used
  only for a cache-control decision (never echoed or path-joined). The cache key
  is built from `asset_version` + validated params, so no client string reaches
  the filesystem path. Rendering runs `build_page` (parameterised SQL) in a
  worker process. No `eval`, shell, or network egress.

---

## Device integration (contract)

The Flutter client (`lib/features/quran/data/`: `quran_image_client.dart`,
`mushaf_page_store.dart`, `page_asset_cache.dart`). What it does:

1. `physical_w = logical_w × devicePixelRatio`; snaps to the ladder locally
   (`PageAssetManifest.snapWidth`) so the request already asks for the rung it
   will get.
2. per page, in parallel:
   `GET /v1/pages/{page}?w={physical_w}&fmt=webp&v={asset_version}` and
   `GET /v1/pages/{page}/layout?w={physical_w}&v={asset_version}`.
3. On `200`, **write both to disk immediately** under
   `mushaf_pages/v<ver>/w<width>/<page>.{webp,layout.json}`, storing the image
   `ETag` in `<page>.meta.json`.
4. Future opens read those files directly (no request); a background
   `If-None-Match` on the image revalidates and a `200` refreshes both.
5. `w` and `v` are in the path, so every width is a distinct directory and an
   `asset_version` change rotates every key at once — the old directory is
   pruned on the next launch.

---

## Module map

| module | role |
|---|---|
| `quran_image/server.py` | FastAPI app — routes (image + `/layout`), headers, `304`, dev entry point |
| `quran_image/service.py` | `RenderService` — cache tiers + process pool + coalescing + warming (`get` / `get_layout`); nearest-rung fallback + background backfill pool (`get_or_fallback` / `get_layout_or_fallback`) |
| `quran_image/imagecache.py` | `MemoryLRU`, `DiskCache` (LRU + size cap), `SingleFlight` |
| `quran_image/dimensions.py` | screen metrics → canonical `RenderSpec`; width ladder; page range |
| `quran_image/assets.py` | locate + content-hash the source asset bundle (`asset_version`) |
| `quran_image/render.py` | `render_page()` (coverage → palette image) + `encode_image()` (PNG / lossless WebP) |
| `quran_image/layout.py` | width-parametric page geometry — `build_page()` (glyph draw ops + `WordBox`es) / `build_layout()` (the `/layout` JSON) |
| `quran_image/gdcompat.py` | FreeType re-implementation of libgd / GD::Text metrics — see [`docs/calibration_notes.md`](docs/calibration_notes.md) |
| `quran_image/db.py` | read page/line/glyph data from `layout.sqlite` |
| `scripts/import_layout.py` | one-time build of `data/layout.sqlite` from the SQL dump |
| `tests/` | `test_server.py` (negotiation, cache, coalescing, API, real-render parity), `test_pipeline.py` (layout + raster primitives) |
