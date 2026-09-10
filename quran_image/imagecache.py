"""Server-side image cache: RAM LRU in front of a size-bounded disk store,
plus request coalescing so a thundering herd renders a page only once.

Layout on disk - a plain width-partitioned tree, one file per entry, no
sidecar::

    <root>/<canonical_width>/<page>.png          an encoded PNG page image
    <root>/<canonical_width>/<page>.webp         the same page as lossless WebP
    <root>/<canonical_width>/<page>.layout.json  that page's per-word geometry

The key handed to :class:`DiskCache` is exactly that ``/``-separated relative
path (``"1120/42.png"``); the ``<width>/`` directory is created on demand.
``content_type`` is inferred from the extension and the strong ``ETag`` is
recomputed from the bytes with the same formula the renderer uses, so an entry
needs no companion metadata file.

Everything here is process-safe (threads) and tolerant of a second process or
node writing the same key concurrently (atomic ``os.replace``); the disk store
is therefore shareable over NFS/EFS for a multi-node deployment.
"""
from __future__ import annotations

import hashlib
import os
import threading
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
    """Width-partitioned file store with an opportunistic size cap.

    One file per entry at ``<root>/<key>`` (``key`` is a ``/``-separated
    relative path such as ``"1120/42.png"``); no sidecar.  ``content_type`` is
    inferred from the extension and the strong ``ETag`` is recomputed from the
    bytes, so a disk entry carries no metadata of its own.  LRU ordering is by
    mtime, which :meth:`get` refreshes on every read.
    """

    _CONTENT_TYPES = {
        ".png": "image/png",
        ".webp": "image/webp",
        ".json": "application/json",
    }

    def __init__(self, root: str, max_bytes: int = 2 * 1024**3):
        self.root = root
        self.max_bytes = max_bytes
        self._evict_lock = threading.Lock()
        self._puts_since_sweep = 0

    def _path(self, key: str) -> str:
        return os.path.join(self.root, *key.split("/"))

    @classmethod
    def _content_type(cls, key: str) -> str:
        _, ext = os.path.splitext(key)
        return cls._CONTENT_TYPES.get(ext.lower(), "application/octet-stream")

    @staticmethod
    def _etag(data: bytes) -> str:
        return '"' + hashlib.sha256(data).hexdigest()[:32] + '"'

    def exists(self, key: str) -> bool:
        """Cheap presence probe - no file bodies read.  Used to find which
        ladder rungs are already cached for a page without loading them."""
        return os.path.exists(self._path(key))

    def get(self, key: str) -> Payload | None:
        path = self._path(key)
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            return None
        try:  # LRU touch; best-effort
            os.utime(path, None)
        except OSError:
            pass
        return data, self._content_type(key), self._etag(data)

    def put(self, key: str, payload: Payload) -> None:
        data = payload[0]
        path = self._path(key)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return

        self._puts_since_sweep += 1
        if self._puts_since_sweep >= 64:
            self._puts_since_sweep = 0
            self.sweep()

    def _iter_files(self):
        for dirpath, _, names in os.walk(self.root):
            for n in names:
                if not n.endswith(".tmp"):
                    yield os.path.join(dirpath, n)

    def sweep(self) -> None:
        """Delete least-recently-used entries until under ``max_bytes``."""
        if not self._evict_lock.acquire(blocking=False):
            return
        try:
            entries = []
            total = 0
            for path in self._iter_files():
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                entries.append((st.st_mtime, path, st.st_size))
                total += st.st_size
            if total <= self.max_bytes:
                return
            entries.sort()  # oldest mtime first
            for _, path, size in entries:
                if total <= self.max_bytes:
                    break
                try:
                    os.remove(path)
                    total -= size
                except OSError:
                    pass
        finally:
            self._evict_lock.release()

    def stats(self) -> dict:
        n = b = 0
        for path in self._iter_files():
            try:
                b += os.stat(path).st_size
                n += 1
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
