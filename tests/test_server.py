"""Tests for the on-demand render server (dimensions, cache, API).

The API-layer tests inject a fake ``render_fn`` so they need neither the QCF
fonts nor the layout DB.  The two integration tests at the bottom do a real
render and are skipped automatically when the assets are absent.
"""
from __future__ import annotations

import hashlib
import math
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quran_image.assets import load_bundle  # noqa: E402
from quran_image.dimensions import (  # noqa: E402
    MAX_WIDTH,
    MIN_WIDTH,
    PHI,
    SNAP_DOWN_TOLERANCE,
    WIDTH_LADDER,
    negotiate,
    snap_width,
)
from quran_image.imagecache import DiskCache, MemoryLRU, SingleFlight  # noqa: E402
from quran_image.service import RenderBusy, RenderService  # noqa: E402


# --------------------------------------------------------------------------- #
# dimension negotiation
# --------------------------------------------------------------------------- #
def test_snap_width_rounds_up_and_clamps():
    assert snap_width(1) == MIN_WIDTH
    assert snap_width(10_000) == MAX_WIDTH
    assert snap_width(1000) == 1010          # normal request snaps to next larger rung
    assert snap_width(1102) == 1260          # up to next rung
    assert snap_width(1080) == 1080          # exact rung stays


def test_snap_width_tolerates_a_hair_over_a_rung():
    # a request that overshoots a rung by <=2% snaps down to it rather than
    # jumping a whole step (412 dp * 2.625 = 1082 px -> the 1080 rung, not 1260)
    assert snap_width(1082) == 1080
    assert negotiate(screen_width_px=412, dpr=2.625).width == 1080
    # the band is [rung, rung * (1 + tolerance)], widths ceil'd to whole px first
    edge = math.floor(1080 * (1 + SNAP_DOWN_TOLERANCE))              # 1101
    assert snap_width(edge) == 1080
    assert snap_width(edge + 1) == 1260                              # just past it
    # the tolerance is narrow enough not to swallow an honest mid-ladder request
    assert snap_width(1000) == 1010
    assert snap_width(1150) == 1260


def test_width_ladder_is_geometric_and_keeps_the_legacy_rung():
    assert list(WIDTH_LADDER) == sorted(WIDTH_LADDER)          # ascending
    assert len(set(WIDTH_LADDER)) == len(WIDTH_LADDER)         # no duplicates
    assert 1260 in WIDTH_LADDER                                # legacy rung pinned
    assert 1080 in WIDTH_LADDER                                # 1080p panels pinned
    assert WIDTH_LADDER[0] == MIN_WIDTH
    assert WIDTH_LADDER[-1] == MAX_WIDTH == 2048               # upper limit unchanged
    ratios = [b / a for a, b in zip(WIDTH_LADDER, WIDTH_LADDER[1:])]
    # ~10-12% growth per rung and no near-duplicate cluster
    # (the old ladder had 1242 -> 1260 -> 1290, ratios ~1.014)
    assert all(1.05 < r < 1.20 for r in ratios)
    assert 1242 not in WIDTH_LADDER and 1290 not in WIDTH_LADDER


def test_negotiate_from_screen_metrics():
    spec = negotiate(screen_width_px=360, dpr=3, fmt="webp")
    assert spec.width == 1080
    assert spec.height == int(1080 * PHI)
    assert spec.fmt == "webp"
    assert spec.requested_width == 1080
    assert spec.key == "1080/webp"


def test_negotiate_explicit_width_and_default():
    assert negotiate(w=1010).width == 1010
    assert negotiate().width == negotiate(w=1080).width  # DEFAULT_WIDTH
    assert negotiate(screen_width_px=400, dpr=2, max_width=700).width == 740


def test_negotiate_rejects_bad_input():
    with pytest.raises(ValueError):
        negotiate(fmt="jpg")
    with pytest.raises(ValueError):
        negotiate(w=0)
    with pytest.raises(ValueError):
        negotiate(w=float("inf"))


# --------------------------------------------------------------------------- #
# caches
# --------------------------------------------------------------------------- #
def test_memory_lru_evicts_by_count_and_bytes():
    lru = MemoryLRU(max_items=2, max_bytes=10_000)
    lru.put("a", (b"x", "image/png", '"a"'))
    lru.put("b", (b"y", "image/png", '"b"'))
    lru.get("a")                       # make 'a' most-recent
    lru.put("c", (b"z", "image/png", '"c"'))
    assert lru.get("b") is None        # 'b' was least-recent -> evicted
    assert lru.get("a") is not None and lru.get("c") is not None

    lru2 = MemoryLRU(max_items=99, max_bytes=8)
    lru2.put("big", (b"12345", "x", '"1"'))
    lru2.put("big2", (b"12345", "x", '"2"'))
    assert lru2.stats()["bytes"] <= 8


def test_disk_cache_roundtrip_and_sweep(tmp_path):
    dc = DiskCache(str(tmp_path), max_bytes=2500)
    for i in range(6):
        dc.put(f"720/{i}.png", (b"a" * 1000, "image/png", f'"{i}"'))
        time.sleep(0.01)
    # files land in the plain width/page tree, no sidecar
    assert os.path.isfile(tmp_path / "720" / "5.png")
    assert not any(p.suffix == ".json" for p in (tmp_path / "720").iterdir())
    got = dc.get("720/5.png")
    assert got is not None and got[0] == b"a" * 1000
    assert got[1] == "image/png"
    # content_type from the extension, strong ETag recomputed from the bytes
    assert got[2] == '"' + hashlib.sha256(b"a" * 1000).hexdigest()[:32] + '"'
    dc.sweep()
    assert dc.stats()["bytes"] <= 2500
    assert dc.get("720/0.png") is None   # oldest gone


def test_single_flight_coalesces(tmp_path):
    sf = SingleFlight()
    calls = []

    def slow():
        calls.append(1)
        time.sleep(0.3)
        return (b"payload", "image/png", '"e"')

    results = []
    threads = [
        threading.Thread(target=lambda: results.append(sf.do("k", slow)))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1                       # rendered once
    assert all(r[0] == (b"payload", "image/png", '"e"') for r in results)
    assert sum(1 for r in results if r[1]) == 1  # exactly one leader


# --------------------------------------------------------------------------- #
# RenderService with a fake renderer
# --------------------------------------------------------------------------- #
# the etag formula mirrors quran_image.service (and DiskCache, which recomputes
# it on a disk hit) so a MISS payload and a later HIT-DISK payload compare equal
def _etag(data: bytes) -> str:
    return '"' + hashlib.sha256(data).hexdigest()[:32] + '"'


def _fake_render_fn(counter):
    def fn(page, width, fmt):
        counter.append((page, width, fmt))
        data = f"IMG:{page}:{width}:{fmt}".encode()
        return data, f"image/{fmt}", _etag(data)

    return fn


def _fake_layout_fn(counter):
    def fn(page, width):
        counter.append((page, width))
        data = f'{{"page":{page},"width":{width},"words":[]}}'.encode()
        return data, "application/json", _etag(data)

    return fn


def _svc(tmp_path, counter, **kw):
    bundle = load_bundle()
    return RenderService(
        bundle, cache_dir=str(tmp_path), render_fn=_fake_render_fn(counter), **kw
    )


def test_service_miss_then_memory_then_disk(tmp_path):
    calls = []
    svc = _svc(tmp_path, calls)
    spec = negotiate(w=1080, fmt="png")

    (data1, _, etag1), state1 = svc.get(3, spec)
    assert state1 == "MISS" and data1 == b"IMG:3:1080:png"  # w=1080 is a rung

    _, state2 = svc.get(3, spec)
    assert state2 == "HIT-MEM"

    # a fresh service (cold RAM) still hits the shared disk cache
    svc2 = _svc(tmp_path, [])
    (data3, _, etag3), state3 = svc2.get(3, spec)
    assert state3 == "HIT-DISK" and data3 == data1 and etag3 == etag1
    assert len(calls) == 1  # rendered exactly once across both services


def test_service_different_widths_are_independent(tmp_path):
    calls = []
    svc = _svc(tmp_path, calls)
    svc.get(3, negotiate(w=720))
    svc.get(3, negotiate(w=1080))
    svc.get(3, negotiate(w=1080, fmt="webp"))
    assert len(calls) == 3
    assert {c[1:] for c in calls} == {(740, "png"), (1080, "png"), (1080, "webp")}


def _wait(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met within timeout")


def test_pref_order_ranks_by_distance_then_prefers_larger():
    order = RenderService._pref_order(1080)
    assert order[0] == 1080                          # exact rung first
    assert order[1] == 1010 and order[2] == 910      # 70px vs 170px away
    assert order.index(1010) < order.index(2048)     # a near-smaller rung beats
    #                                                  a far-larger one
    assert set(order) == set(WIDTH_LADDER)
    # a width exactly between two rungs -> the larger of the two wins the tie
    assert RenderService._pref_order(1190)[0] == 1260


def test_get_or_fallback_prefers_closest_cached_rung_then_backfills(tmp_path):
    calls = []
    svc = _svc(tmp_path, calls)
    svc.get(3, negotiate(w=910))                     # a near rung below the target
    svc.get(3, negotiate(w=2048))                    # a far rung above the target

    payload, state, served = svc.get_or_fallback(3, negotiate(w=1080))
    assert state == "FALLBACK"
    assert served.width == 910                       # closest cached, not 2048
    assert payload[0] == b"IMG:3:910:png"

    key = svc.cache_key(3, negotiate(w=1080))
    _wait(lambda: svc.disk.exists(key))              # background render lands
    p2, s2, _ = svc.get_or_fallback(3, negotiate(w=1080))
    assert s2 in ("HIT-MEM", "HIT-DISK")
    assert p2[0] == b"IMG:3:1080:png"
    assert calls.count((3, 1080, "png")) == 1


def test_get_or_fallback_picks_larger_rung_when_it_is_closer(tmp_path):
    calls = []
    svc = _svc(tmp_path, calls)
    svc.get(3, negotiate(w=540))                     # far below
    svc.get(3, negotiate(w=1260))                    # just above target
    _, state, served = svc.get_or_fallback(3, negotiate(w=1080))
    assert state == "FALLBACK" and served.width == 1260  # 180px vs 540px away


def test_get_or_fallback_uses_far_larger_rung_as_last_resort(tmp_path):
    calls = []
    svc = _svc(tmp_path, calls)
    svc.get(3, negotiate(w=2048))                    # nothing closer is cached
    _, state, served = svc.get_or_fallback(3, negotiate(w=1080))
    assert state == "FALLBACK" and served.width == 2048


def test_get_or_fallback_uses_smaller_when_nothing_larger(tmp_path):
    calls = []
    svc = _svc(tmp_path, calls)
    svc.get(3, negotiate(w=540))
    _, state, served = svc.get_or_fallback(3, negotiate(w=1080))
    assert state == "FALLBACK" and served.width == 540


def test_get_or_fallback_cold_start_blocks(tmp_path):
    calls = []
    svc = _svc(tmp_path, calls)
    payload, state, served = svc.get_or_fallback(3, negotiate(w=1080))
    assert state == "MISS" and served.width == 1080
    assert payload[0] == b"IMG:3:1080:png"


def test_get_or_fallback_dedupes_background_render(tmp_path):
    calls = []

    def slow_fn(page, width, fmt):
        calls.append((page, width, fmt))
        time.sleep(0.3)
        data = f"IMG:{page}:{width}:{fmt}".encode()
        return data, f"image/{fmt}", _etag(data)

    svc = RenderService(load_bundle(), cache_dir=str(tmp_path), render_fn=slow_fn)
    svc.get(3, negotiate(w=1440))                    # prime a fallback rung (-> 1530)

    out = []
    ts = [
        threading.Thread(
            target=lambda: out.append(svc.get_or_fallback(3, negotiate(w=1080)))
        )
        for _ in range(2)
    ]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert all(x[1] == "FALLBACK" for x in out)

    key = svc.cache_key(3, negotiate(w=1080))
    _wait(lambda: svc.disk.exists(key))
    time.sleep(0.1)
    assert calls.count((3, 1080, "png")) == 1        # rendered once, not twice
    assert len(calls) == 2                           # 1530 prime + one 1080 backfill


def test_service_render_busy(tmp_path):
    def blocker(page, width, fmt):
        time.sleep(0.5)
        return b"x", "image/png", '"x"'

    bundle = load_bundle()
    svc = RenderService(
        bundle, cache_dir=str(tmp_path), render_fn=blocker, max_concurrent_renders=1
    )
    svc._sema.acquire()  # simulate the one slot already taken
    with pytest.raises(RenderBusy):
        svc._render(3, negotiate(w=720), "740/3.png")


# --------------------------------------------------------------------------- #
# API layer
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient

    from quran_image.server import create_app

    calls = []
    layout_calls = []
    app = create_app(
        cache_dir=str(tmp_path),
        render_fn=_fake_render_fn(calls),
        layout_fn=_fake_layout_fn(layout_calls),
    )
    with TestClient(app) as c:
        c.render_calls = calls  # type: ignore[attr-defined]
        c.layout_calls = layout_calls  # type: ignore[attr-defined]
        yield c


def test_manifest_shape(client):
    m = client.get("/v1/manifest").json()
    assert m["pages"] == {"min": 1, "max": 604, "count": 604}
    assert m["formats"] == ["png", "webp"]
    assert m["width"]["ladder"][0] == MIN_WIDTH
    assert m["asset_version"]


def test_get_page_headers_and_negotiation(client):
    av = client.get("/v1/manifest").json()["asset_version"]
    r = client.get(f"/v1/pages/3?sw=360&dpr=3&fmt=webp&v={av}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"
    assert r.headers["x-cache"] == "MISS"
    assert r.headers["x-render-width"] == "1080"
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert r.headers["content-location"] == f"/v1/pages/3?w=1080&fmt=webp&v={av}"

    r2 = client.get(f"/v1/pages/3?sw=360&dpr=3&fmt=webp&v={av}")
    assert r2.headers["x-cache"] in ("HIT-MEM", "HIT-DISK")


def test_stale_version_is_not_immutable(client):
    r = client.get("/v1/pages/3?w=1080&v=deadbeef")
    assert "immutable" not in r.headers["cache-control"]
    assert "max-age=86400" in r.headers["cache-control"]


def test_etag_304(client):
    r = client.get("/v1/pages/3?w=1080")
    etag = r.headers["etag"]
    r304 = client.get("/v1/pages/3?w=1080", headers={"If-None-Match": etag})
    assert r304.status_code == 304
    assert r304.content == b""


def test_post_page_with_screen_body(client):
    r = client.post(
        "/v1/pages/10",
        json={"screen": {"width_px": 393, "dpr": 3.0}, "format": "png"},
    )
    assert r.status_code == 200
    assert r.headers["x-render-width"] == "1260"
    assert r.content == b"IMG:10:1260:png"


def test_page_out_of_range(client):
    assert client.get("/v1/pages/605?w=1080").status_code in (404, 422)
    assert client.get("/v1/pages/0?w=1080").status_code in (404, 422)
    assert client.get("/v1/pages/605/layout?w=1080").status_code in (404, 422)


def test_layout_endpoint_headers_and_negotiation(client):
    av = client.get("/v1/manifest").json()["asset_version"]
    r = client.get(f"/v1/pages/3/layout?sw=360&dpr=3&v={av}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["x-cache"] == "MISS"
    assert r.headers["x-render-width"] == "1080"
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert r.headers["content-location"] == f"/v1/pages/3/layout?w=1080&v={av}"
    body = r.json()
    assert body["page"] == 3 and body["width"] == 1080 and "words" in body

    r2 = client.get(f"/v1/pages/3/layout?sw=360&dpr=3&v={av}")
    assert r2.headers["x-cache"] in ("HIT-MEM", "HIT-DISK")
    assert len(client.layout_calls) == 1  # built once, then cached


def test_layout_etag_304(client):
    r = client.get("/v1/pages/3/layout?w=1080")
    etag = r.headers["etag"]
    r304 = client.get("/v1/pages/3/layout?w=1080", headers={"If-None-Match": etag})
    assert r304.status_code == 304
    assert r304.content == b""


def test_head_is_bodyless_with_length(client):
    for path in ("/v1/pages/3?w=1080", "/v1/pages/3/layout?w=1080"):
        h = client.head(path)
        assert h.status_code == 200
        assert h.content == b""
        assert int(h.headers["content-length"]) > 0
        assert h.headers["etag"]


def test_layout_and_image_share_the_negotiated_width(client):
    # the boxes are only valid over the image at the same width, so both
    # endpoints must snap an odd request to the same rung
    img = client.get("/v1/pages/7?w=1000")
    lay = client.get("/v1/pages/7/layout?w=1000")
    assert img.headers["x-render-width"] == lay.headers["x-render-width"]


def test_bad_format_is_400_or_422(client):
    assert client.get("/v1/pages/3?w=1080&fmt=gif").status_code in (400, 422)


def test_concurrent_requests_different_sizes(client):
    av = client.get("/v1/manifest").json()["asset_version"]
    out = []

    def hit(w):
        out.append(client.get(f"/v1/pages/7?w={w}&v={av}").status_code)

    # raw widths chosen so each snaps to a distinct ladder rung
    ts = [threading.Thread(target=hit, args=(w,)) for w in (540, 720, 1000, 1170, 1440)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert out == [200] * 5
    # each width is a distinct key; whichever aren't cold-rendered on the request
    # path get backfilled in the background - so every width is rendered exactly
    # once (cold-start XOR background), never twice.
    _wait(lambda: len(client.render_calls) >= 5)
    assert len(client.render_calls) == 5
    assert {w for _, w, _ in client.render_calls} == {540, 740, 1010, 1260, 1530}


def test_fallback_response_headers_then_exact(client):
    av = client.get("/v1/manifest").json()["asset_version"]
    client.get(f"/v1/pages/8?w=1530&fmt=png&v={av}")  # prime the only nearer rung

    r = client.get(f"/v1/pages/8?w=1080&fmt=png&v={av}")  # -> target rung 1080
    assert r.status_code == 200
    assert r.headers["x-cache"] == "FALLBACK"
    assert r.headers["x-fallback"] == "1"
    assert r.headers["x-target-width"] == "1080"
    assert r.headers["x-render-width"] == "1530"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["content-location"] == f"/v1/pages/8?w=1530&fmt=png&v={av}"
    assert r.content == b"IMG:8:1530:png"

    def exact():
        rr = client.get(f"/v1/pages/8?w=1080&fmt=png&v={av}")
        return rr if rr.headers["x-cache"] != "FALLBACK" else None

    _wait(lambda: exact() is not None)
    rr = client.get(f"/v1/pages/8?w=1080&fmt=png&v={av}")
    assert rr.headers["x-cache"] in ("HIT-MEM", "HIT-DISK")
    assert rr.content == b"IMG:8:1080:png"


def test_layout_fallback_headers(client):
    av = client.get("/v1/manifest").json()["asset_version"]
    client.get(f"/v1/pages/9/layout?w=1530&v={av}")  # prime the only nearer rung

    r = client.get(f"/v1/pages/9/layout?w=1080&v={av}")
    assert r.status_code == 200
    assert r.headers["x-cache"] == "FALLBACK"
    assert r.headers["x-fallback"] == "1"
    assert r.headers["x-render-width"] == "1530"
    assert r.headers["cache-control"] == "no-store"
    assert r.json()["width"] == 1530


# --------------------------------------------------------------------------- #
# integration - real render (skipped without assets)
# --------------------------------------------------------------------------- #
_BUNDLE = load_bundle()
_REFS = os.path.join(os.path.dirname(__file__), "refs")

integration = pytest.mark.skipif(
    not _BUNDLE.ready, reason="QCF fonts / layout.sqlite not present"
)


@integration
@pytest.mark.parametrize("page", [1, 50])
def test_real_render_width_1260_matches_reference(tmp_path, page):
    ref_path = os.path.join(_REFS, f"page{page}_w1260.png")
    if not os.path.isfile(ref_path):
        pytest.skip(f"{ref_path} reference not present")
    svc = RenderService(_BUNDLE, cache_dir=str(tmp_path), workers=1)
    svc.start()
    try:
        (data, ct, _), _ = svc.get(page, negotiate(w=1260, fmt="png"))
    finally:
        svc.close()
    assert ct == "image/png"
    ref = open(ref_path, "rb").read()
    assert hashlib.sha256(data).digest() == hashlib.sha256(ref).digest()


@integration
def test_real_render_arbitrary_width_dimensions(tmp_path):
    import io

    import numpy as np
    from PIL import Image

    svc = RenderService(_BUNDLE, cache_dir=str(tmp_path), workers=1)
    svc.start()
    try:
        (png, ct_png, _), _ = svc.get(2, negotiate(w=910, fmt="png"))
        (webp, ct_webp, _), _ = svc.get(2, negotiate(w=910, fmt="webp"))
    finally:
        svc.close()

    im = Image.open(io.BytesIO(png))
    assert im.size == (910, int(910 * PHI)) and im.mode == "P"
    assert ct_png == "image/png" and ct_webp == "image/webp"

    # webp is lossless: identical decoded pixels, smaller on the wire
    a = np.array(Image.open(io.BytesIO(png)).convert("RGBA"))
    b = np.array(Image.open(io.BytesIO(webp)).convert("RGBA"))
    assert np.array_equal(a, b)
    assert len(webp) < len(png)
