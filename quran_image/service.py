"""``RenderService`` - the glue between the HTTP layer and the pipeline.

    request  ->  RAM LRU  ->  disk cache  ->  single-flight  ->  process pool
                  (hit)        (hit, warm     (one render      (build_page +
                                RAM)           per key)         render_page +
                                                                encode)

Design goals from the brief:

* **fast generation** - a warm ``LayoutDB`` and per-worker FreeType glyph
  caches are kept alive in each pool process; only the first request for a
  given page pays the cold cost.
* **low memory** - rendering happens in separate processes (the ~w*h uint8
  coverage buffer and the PIL image are freed when the worker returns) and the
  number of simultaneous renders is capped; the API process only ever holds
  encoded bytes.
* **concurrency across devices** - the cache key is the on-disk relative path
  ``<canonical_width>/<page>.<fmt>`` (e.g. ``1080/42.png``); different screen
  sizes are independent keys that render in parallel across the pool, while
  identical requests coalesce.  ``asset_version`` is not part of the path - it
  still drives HTTP immutability via the ``v`` query param, so rotate
  ``QURAN_CACHE_DIR`` (or clear it) when the source assets change.
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import threading
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from typing import Callable

from .assets import AssetBundle
from .dimensions import PHI, WIDTH_LADDER, RenderSpec
from .imagecache import DiskCache, MemoryLRU, Payload, SingleFlight

# --------------------------------------------------------------------------- #
# pool worker  -  one warm LayoutDB + FreeType caches per process
# --------------------------------------------------------------------------- #
_W: dict = {}


def _worker_init(db_path: str, fonts_dir: str) -> None:
    from .db import LayoutDB
    from .render import default_render_mode

    _W["db"] = LayoutDB(db_path, fonts_dir)
    _W["render_mode"] = default_render_mode()


def _worker_render(page: int, width: int, fmt: str) -> Payload:
    from .layout import build_page
    from .render import encode_image, render_page

    plan = build_page(_W["db"], page, width)
    img = render_page(plan, mode=_W["render_mode"])
    data, content_type = encode_image(img, fmt)
    etag = '"' + hashlib.sha256(data).hexdigest()[:32] + '"'
    return data, content_type, etag


def _worker_layout(page: int, width: int) -> Payload:
    import json

    from .layout import build_layout

    doc = build_layout(_W["db"], page, width)
    data = json.dumps(doc, separators=(",", ":")).encode("utf-8")
    etag = '"' + hashlib.sha256(data).hexdigest()[:32] + '"'
    return data, "application/json", etag


# --------------------------------------------------------------------------- #
class RenderService:
    def __init__(
        self,
        bundle: AssetBundle,
        *,
        cache_dir: str,
        workers: int = 0,
        max_concurrent_renders: int = 0,
        mem_items: int = 96,
        mem_bytes: int = 96 * 1024 * 1024,
        disk_max_bytes: int = 2 * 1024**3,
        bg_workers: int = 0,
        bg_max_queued: int = 64,
        render_fn: Callable[[int, int, str], Payload] | None = None,
        layout_fn: Callable[[int, int], Payload] | None = None,
    ):
        self.bundle = bundle
        self.mem = MemoryLRU(max_items=mem_items, max_bytes=mem_bytes)
        self.disk = DiskCache(cache_dir, max_bytes=disk_max_bytes)
        self._sf = SingleFlight()
        self._workers = workers or (os.cpu_count() or 2)
        self._sema = threading.BoundedSemaphore(
            max_concurrent_renders or max(2, self._workers)
        )
        self._pool: ProcessPoolExecutor | None = None
        self._render_fn = render_fn  # test / in-process override
        self._layout_fn = layout_fn  # test / in-process override
        self._counter = Counter()
        self._lock = threading.Lock()

        # -- background backfill: render the *exact* rung a device asked for
        #    after it has already been handed the nearest cached one, so the
        #    client request never blocks on a render (see ``get_or_fallback``).
        #    The CPU work still runs in ``self._pool`` and is still bounded by
        #    ``self._sema``; a bg thread only blocks on ``.result()``.
        self._bg_max_queued = bg_max_queued
        self._bg = ThreadPoolExecutor(
            max_workers=bg_workers or self._workers,
            thread_name_prefix="bg-render",
        )
        self._bg_inflight: set[str] = set()  # dedupe queued jobs; guarded by _lock

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> None:
        if (
            self._render_fn is not None
            or self._layout_fn is not None
            or self._pool is not None
        ):
            return
        if not self.bundle.ready:
            return
        self._pool = ProcessPoolExecutor(
            max_workers=self._workers,
            initializer=_worker_init,
            initargs=(self.bundle.db_path, self.bundle.fonts_dir),
        )

    def close(self) -> None:
        self._bg.shutdown(wait=False, cancel_futures=True)
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None

    @property
    def renderable(self) -> bool:
        return (
            self._render_fn is not None
            or self._layout_fn is not None
            or self._pool is not None
        )

    # -- keys -------------------------------------------------------------- #
    # A key is the entry's path relative to the cache dir:
    #     cache/<canonical_width>/<page>.<fmt>          image
    #     cache/<canonical_width>/<page>.layout.json    per-word geometry
    # It is the same string for the RAM LRU and the disk store.
    def cache_key(self, page: int, spec: RenderSpec) -> str:
        return f"{spec.width}/{page}.{spec.fmt}"

    def layout_key(self, page: int, spec: RenderSpec) -> str:
        # ``fmt`` is irrelevant to the geometry; only width changes it.
        return f"{spec.width}/{page}.layout.json"

    # -- main entry point -------------------------------------------------- #
    def get(self, page: int, spec: RenderSpec) -> tuple[Payload, str]:
        """Return ``((bytes, content_type, etag), cache_state)``.

        ``cache_state`` is one of ``HIT-MEM`` / ``HIT-DISK`` / ``MISS``.
        """
        key = self.cache_key(page, spec)

        hit = self.mem.get(key)
        if hit is not None:
            self._bump("hit_mem")
            return hit, "HIT-MEM"

        hit = self.disk.get(key)
        if hit is not None:
            self._bump("hit_disk")
            self.mem.put(key, hit)
            return hit, "HIT-DISK"

        payload, was_leader = self._sf.do(key, lambda: self._render(page, spec, key))
        self._bump("miss" if was_leader else "coalesced")
        return payload, "MISS"

    def get_layout(self, page: int, spec: RenderSpec) -> tuple[Payload, str]:
        """The page's word geometry as ``((json_bytes, "application/json", etag),
        cache_state)`` - same cache tiers and coalescing as :meth:`get`."""
        key = self.layout_key(page, spec)

        hit = self.mem.get(key)
        if hit is not None:
            self._bump("layout_hit_mem")
            return hit, "HIT-MEM"

        hit = self.disk.get(key)
        if hit is not None:
            self._bump("layout_hit_disk")
            self.mem.put(key, hit)
            return hit, "HIT-DISK"

        payload, was_leader = self._sf.do(
            key, lambda: self._render_layout(page, spec, key)
        )
        self._bump("layout_miss" if was_leader else "layout_coalesced")
        return payload, "MISS"

    # -- fallback + background backfill ----------------------------------- #
    def get_or_fallback(
        self, page: int, spec: RenderSpec
    ) -> tuple[Payload, str, RenderSpec]:
        """Like :meth:`get`, but never blocks the client on a render.

        Returns ``(payload, cache_state, served_spec)``.  ``served_spec`` equals
        ``spec`` except on ``FALLBACK``, where it describes the nearer rung that
        was actually returned.

        * exact rung cached  -> ``HIT-MEM`` / ``HIT-DISK`` (fast path)
        * exact rung missing but *some* rung for this page is cached ->
          return the nearest one immediately (``FALLBACK``) and render the
          exact rung in the background
        * no rung cached at all (true cold start) -> block once (``MISS``)
        """
        key = self.cache_key(page, spec)

        hit = self.mem.get(key)
        if hit is not None:
            self._bump("hit_mem")
            return hit, "HIT-MEM", spec

        hit = self.disk.get(key)
        if hit is not None:
            self._bump("hit_disk")
            self.mem.put(key, hit)
            return hit, "HIT-DISK", spec

        near = self._nearest_available(page, spec, self.cache_key)
        if near is not None:
            cand, ckey = near
            payload = self.mem.get(ckey) or self.disk.get(ckey)
            if payload is not None:
                self.mem.put(ckey, payload)
                self._enqueue_bg(page, spec, key, self._render)
                self._record_fallback("fallback", spec.width, cand.width)
                return payload, "FALLBACK", cand

        payload, was_leader = self._sf.do(key, lambda: self._render(page, spec, key))
        self._bump("miss" if was_leader else "coalesced")
        return payload, "MISS", spec

    def get_layout_or_fallback(
        self, page: int, spec: RenderSpec
    ) -> tuple[Payload, str, RenderSpec]:
        """:meth:`get_layout` with the same never-block-the-client contract as
        :meth:`get_or_fallback`.  A fallback layout JSON is self-consistent (its
        body carries the width its boxes are in) and the client scales boxes to
        the display rectangle proportionally, so a transient image/layout width
        mismatch is at most a sub-pixel highlight offset until the backfill lands.
        """
        key = self.layout_key(page, spec)

        hit = self.mem.get(key)
        if hit is not None:
            self._bump("layout_hit_mem")
            return hit, "HIT-MEM", spec

        hit = self.disk.get(key)
        if hit is not None:
            self._bump("layout_hit_disk")
            self.mem.put(key, hit)
            return hit, "HIT-DISK", spec

        near = self._nearest_available(page, spec, self.layout_key)
        if near is not None:
            cand, ckey = near
            payload = self.mem.get(ckey) or self.disk.get(ckey)
            if payload is not None:
                self.mem.put(ckey, payload)
                self._enqueue_bg(page, spec, key, self._render_layout)
                self._record_fallback("layout_fallback", spec.width, cand.width)
                return payload, "FALLBACK", cand

        payload, was_leader = self._sf.do(
            key, lambda: self._render_layout(page, spec, key)
        )
        self._bump("layout_miss" if was_leader else "layout_coalesced")
        return payload, "MISS", spec

    # -- helpers for the fallback path ----------------------------------- #
    @staticmethod
    def _pref_order(target: int) -> list[int]:
        """Ladder rungs ranked as a cold-cache stand-in for ``target``: the
        closest rung by absolute pixel distance first, a larger rung winning an
        exact tie (the device then only downscales).  Unlike normal rung
        selection this does *not* deliberately snap upward - a far-larger rung
        is used only when no nearer rung is cached, so a 1080 miss never serves
        a 4x-heavier 2048 image when a 1010 or 1260 rung is warm."""
        return sorted(WIDTH_LADDER, key=lambda w: (abs(w - target), -w))

    @staticmethod
    def _snap_spec(spec: RenderSpec, width: int) -> RenderSpec:
        return dataclasses.replace(spec, width=width, height=int(width * PHI))

    def _nearest_available(
        self,
        page: int,
        spec: RenderSpec,
        key_fn: Callable[[int, RenderSpec], str],
    ) -> tuple[RenderSpec, str] | None:
        for w in self._pref_order(spec.width):
            if w == spec.width:
                continue
            cand = self._snap_spec(spec, w)
            k = key_fn(page, cand)
            if self.mem.get(k) is not None or self.disk.exists(k):
                return cand, k
        return None

    def _enqueue_bg(
        self,
        page: int,
        spec: RenderSpec,
        key: str,
        worker: Callable[[int, RenderSpec, str], Payload],
    ) -> None:
        with self._lock:
            done = self.disk.exists(key)
            if (
                done
                or key in self._bg_inflight
                or len(self._bg_inflight) >= self._bg_max_queued
            ):
                self._counter["bg_skip_done" if done else "bg_dropped"] += 1
                return
            self._bg_inflight.add(key)
            self._counter["bg_enqueued"] += 1
        self._bg.submit(self._bg_run, page, spec, key, worker)

    def _bg_run(
        self,
        page: int,
        spec: RenderSpec,
        key: str,
        worker: Callable[[int, RenderSpec, str], Payload],
    ) -> None:
        t0 = time.perf_counter()
        try:
            self._sf.do(key, lambda: worker(page, spec, key))
            dt_ms = int((time.perf_counter() - t0) * 1000)
            with self._lock:
                self._counter["bg_rendered"] += 1
                self._counter["bg_render_ms_sum"] += dt_ms
                self._counter["bg_render_n"] += 1
        except Exception:  # noqa: BLE001 - exact rung just gets retried next request
            self._bump("bg_failed")
        finally:
            with self._lock:
                self._bg_inflight.discard(key)

    def _record_fallback(self, name: str, target: int, served: int) -> None:
        with self._lock:
            self._counter[name] += 1
            self._counter["fallback_distance_sum"] += abs(served - target)
            self._counter["fallback_distance_n"] += 1

    # -- rendering --------------------------------------------------------- #
    def _render(self, page: int, spec: RenderSpec, key: str) -> Payload:
        # another node/process may have produced it since our cache checks
        hit = self.disk.get(key)
        if hit is not None:
            self.mem.put(key, hit)
            return hit

        if not self.renderable:
            raise RenderUnavailable(
                "source assets not available: " + "; ".join(self.bundle.missing())
            )

        acquired = self._sema.acquire(timeout=30)
        if not acquired:
            raise RenderBusy("render queue saturated")
        try:
            self._bump("render")
            if self._render_fn is not None:
                payload = self._render_fn(page, spec.width, spec.fmt)
            else:
                payload = self._pool.submit(  # type: ignore[union-attr]
                    _worker_render, page, spec.width, spec.fmt
                ).result()
        finally:
            self._sema.release()

        self.disk.put(key, payload)
        self.mem.put(key, payload)
        return payload

    def _render_layout(self, page: int, spec: RenderSpec, key: str) -> Payload:
        hit = self.disk.get(key)  # another node may have produced it meanwhile
        if hit is not None:
            self.mem.put(key, hit)
            return hit

        if not self.renderable:
            raise RenderUnavailable(
                "source assets not available: " + "; ".join(self.bundle.missing())
            )

        acquired = self._sema.acquire(timeout=30)
        if not acquired:
            raise RenderBusy("render queue saturated")
        try:
            self._bump("layout_build")
            if self._layout_fn is not None:
                payload = self._layout_fn(page, spec.width)
            elif self._pool is not None:
                payload = self._pool.submit(
                    _worker_layout, page, spec.width
                ).result()
            else:
                raise RenderUnavailable("layout rendering is not available")
        finally:
            self._sema.release()

        self.disk.put(key, payload)
        self.mem.put(key, payload)
        return payload

    # -- warming --------------------------------------------------------- #
    def warm(self, pages: list[int], specs: list[RenderSpec]) -> int:
        done = 0
        for p in pages:
            for s in specs:
                try:
                    self.get(p, s)
                    done += 1
                except Exception:  # noqa: BLE001 - best effort
                    pass
        return done

    # -- introspection --------------------------------------------------- #
    def _bump(self, name: str) -> None:
        with self._lock:
            self._counter[name] += 1

    def stats(self) -> dict:
        with self._lock:
            counts = dict(self._counter)
            bg_inflight = len(self._bg_inflight)
        hits = counts.get("hit_mem", 0) + counts.get("hit_disk", 0)
        served = hits + counts.get("fallback", 0) + counts.get("miss", 0)
        fb_n = counts.get("fallback_distance_n", 0)
        bg_n = counts.get("bg_render_n", 0)
        return {
            "asset_version": self.bundle.version,
            "renderable": self.renderable,
            "workers": self._workers if self._pool else 0,
            "inflight": self._sf.inflight,
            "bg_inflight": bg_inflight,
            "counters": counts,
            "derived": {
                "cache_hit_rate": round(hits / served, 4) if served else None,
                "mean_fallback_distance_px": (
                    round(counts["fallback_distance_sum"] / fb_n, 1) if fb_n else None
                ),
                "mean_bg_render_ms": (
                    round(counts["bg_render_ms_sum"] / bg_n, 1) if bg_n else None
                ),
            },
            "mem_cache": self.mem.stats(),
            "disk_cache": self.disk.stats(),
        }


class RenderUnavailable(RuntimeError):
    """Assets missing - the server cannot render anything."""


class RenderBusy(RuntimeError):
    """Too many concurrent renders - client should retry."""
