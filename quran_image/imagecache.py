"""Server-side image cache: RAM LRU in front of a size-bounded disk store,
plus request coalescing so a thundering herd renders a page only once.

Layout on disk (one pair of files per entry, key with ``/`` -> ``~``)::

    <root>/<version>~<page>~<width>~<fmt>.img     the encoded bytes
    <root>/<version>~<page>~<width>~<fmt>.json    {content_type, etag, bytes, ts}

Everything here is process-safe (threads) and tolerant of a second process or
node writing the same key concurrently (atomic ``os.replace``); the disk store
is therefore shareable over NFS/EFS for a multi-node deployment.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from typing import Callable

# (bytes, content_type, etag)
Payload = tuple[bytes, str, str]


class MemoryLRU:
    """Bounded by both entry count and total bytes."""

    def __init__(self, max_items: int = 96, max_bytes: int = 96 * 1024 * 1024):
        self.max_items = max_items
        self.max_bytes = max_bytes
        self._d: "OrderedDict[str, Payload]" = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key: str) -> Payload | None:
        with self._lock:
            v = self._d.get(key)
            if v is not None:
                self._d.move_to_end(key)
            return v

    def put(self, key: str, value: Payload) -> None:
        n = len(value[0])
        if n > self.max_bytes:
            return
        with self._lock:
            if key in self._d:
                self._bytes -= len(self._d.pop(key)[0])
            self._d[key] = value
            self._bytes += n
            while self._d and (
                len(self._d) > self.max_items or self._bytes > self.max_bytes
            ):
                _, old = self._d.popitem(last=False)
                self._bytes -= len(old[0])

    def stats(self) -> dict:
        with self._lock:
            return {"items": len(self._d), "bytes": self._bytes}


class DiskCache:
    """LRU-by-atime file store with an opportunistic size cap."""

    def __init__(self, root: str, max_bytes: int = 2 * 1024**3):
        self.root = root
        self.max_bytes = max_bytes
        self._evict_lock = threading.Lock()
        self._puts_since_sweep = 0
        self._ready = False

    def _ensure_root(self) -> None:
        if not self._ready:
            os.makedirs(self.root, exist_ok=True)
            self._ready = True

    def _base(self, key: str) -> str:
        return os.path.join(self.root, key.replace("/", "~"))

    def exists(self, key: str) -> bool:
        """Cheap presence probe - no file bodies read.  Used to find which
        ladder rungs are already cached for a page without loading them."""
        return os.path.exists(self._base(key) + ".img")

    def get(self, key: str) -> Payload | None:
        base = self._base(key)
        try:
            with open(base + ".json", "r", encoding="utf-8") as fh:
                meta = json.load(fh)
            with open(base + ".img", "rb") as fh:
                data = fh.read()
        except (OSError, ValueError):
            return None
        try:  # LRU touch; best-effort
            os.utime(base + ".img", None)
        except OSError:
            pass
        return data, meta["content_type"], meta["etag"]

    def put(self, key: str, payload: Payload) -> None:
        data, content_type, etag = payload
        self._ensure_root()
        base = self._base(key)
        tmp = f"{base}.img.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, base + ".img")
            tmp_meta = tmp + ".json"
            with open(tmp_meta, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "content_type": content_type,
                        "etag": etag,
                        "bytes": len(data),
                        "ts": time.time(),
                    },
                    fh,
                )
            os.replace(tmp_meta, base + ".json")
        except OSError:
            for p in (tmp, tmp + ".json"):
                try:
                    os.remove(p)
                except OSError:
                    pass
            return

        self._puts_since_sweep += 1
        if self._puts_since_sweep >= 64:
            self._puts_since_sweep = 0
            self.sweep()

    def sweep(self) -> None:
        """Delete least-recently-used entries until under ``max_bytes``."""
        if not self._evict_lock.acquire(blocking=False):
            return
        try:
            entries = []
            total = 0
            with os.scandir(self.root) as it:
                for e in it:
                    if not e.name.endswith(".img"):
                        continue
                    try:
                        st = e.stat()
                    except OSError:
                        continue
                    entries.append((st.st_atime, e.path, st.st_size))
                    total += st.st_size
            if total <= self.max_bytes:
                return
            entries.sort()  # oldest atime first
            for _, path, size in entries:
                if total <= self.max_bytes:
                    break
                for p in (path, path[:-4] + ".json"):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                total -= size
        finally:
            self._evict_lock.release()

    def stats(self) -> dict:
        n = b = 0
        try:
            with os.scandir(self.root) as it:
                for e in it:
                    if e.name.endswith(".img"):
                        n += 1
                        try:
                            b += e.stat().st_size
                        except OSError:
                            pass
        except OSError:
            pass
        return {"items": n, "bytes": b, "max_bytes": self.max_bytes}


class SingleFlight:
    """Collapse concurrent calls for the same key into one execution."""

    def __init__(self):
        self._inflight: dict[str, "_Call"] = {}
        self._guard = threading.Lock()

    def do(self, key: str, fn: Callable[[], Payload]) -> tuple[Payload, bool]:
        with self._guard:
            call = self._inflight.get(key)
            if call is not None:
                leader = False
            else:
                call = self._inflight[key] = _Call()
                leader = True

        if not leader:
            call.done.wait()
            if call.error:
                raise call.error
            return call.result, False  # type: ignore[return-value]

        try:
            call.result = fn()
            return call.result, True
        except BaseException as e:  # noqa: BLE001 - re-raised to every waiter
            call.error = e
            raise
        finally:
            call.done.set()
            with self._guard:
                self._inflight.pop(key, None)

    @property
    def inflight(self) -> int:
        return len(self._inflight)


class _Call:
    __slots__ = ("done", "result", "error")

    def __init__(self):
        self.done = threading.Event()
        self.result: Payload | None = None
        self.error: BaseException | None = None
