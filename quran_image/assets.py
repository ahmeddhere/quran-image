"""Locate the *source assets* the server needs to generate page images and
stamp them with a content version.

The server is stateless: everything it needs to render any page at any width
is a small, fixed asset bundle -

    data/layout.sqlite            glyph -> (page, line, position) placement
    data/gdtext_metrics.json      the four GD::Text metrics captured once
    <fonts>/QCF_*.TTF             the King Fahd Complex "QCF v1" outline fonts

None of that depends on the requested dimensions, so it is loaded once per
process and reused for every device.  :func:`load_bundle` also hashes the
bundle (file sizes + mtimes + the layout-code version) into a short
``version`` string; it is echoed to the device as the ``v`` query param and
gates HTTP immutability, so shipping new fonts or bumping
``layout.LAYOUT_VERSION`` makes clients revalidate.  The server-side disk
cache is a plain ``<width>/<page>.<fmt>`` tree with no version component -
rotate ``QURAN_CACHE_DIR`` when the assets change.

Locations resolve from (first hit wins):
    explicit argument  ->  environment variable  ->  repo-relative default
"""
from __future__ import annotations

import functools
import hashlib
import os
import re
from dataclasses import dataclass

from .db import DEFAULT_DB, DEFAULT_FONTS
from .layout import LAYOUT_VERSION

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

DEFAULT_METRICS = os.environ.get(
    "QURAN_METRICS", os.path.join(_ROOT, "data", "gdtext_metrics.json")
)


def _safe_listdir(path: str) -> list[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []


@dataclass(frozen=True)
class AssetBundle:
    db_path: str
    fonts_dir: str
    metrics_path: str
    version: str  # 16-hex-char content hash; leads every cache key

    @property
    def ready(self) -> bool:
        """True once every asset needed to render is present."""
        return (
            os.path.isfile(self.db_path)
            and os.path.isdir(self.fonts_dir)
            and any(
                f.lower().endswith(".ttf") for f in _safe_listdir(self.fonts_dir)
            )
        )

    def missing(self) -> list[str]:
        out = []
        if not os.path.isfile(self.db_path):
            out.append(f"layout db: {self.db_path}")
        if not os.path.isdir(self.fonts_dir):
            out.append(f"fonts dir: {self.fonts_dir}")
        elif not any(f.lower().endswith(".ttf") for f in _safe_listdir(self.fonts_dir)):
            out.append(f"no *.TTF in fonts dir: {self.fonts_dir}")
        return out


def _stamp(h: "hashlib._Hash", label: str, path: str) -> None:
    try:
        st = os.stat(path)
        h.update(f"|{label}:{st.st_size}:{int(st.st_mtime)}".encode())
    except OSError:
        h.update(f"|{label}:absent".encode())


@functools.lru_cache(maxsize=8)
def load_bundle(
    db_path: str | None = None,
    fonts_dir: str | None = None,
    metrics_path: str | None = None,
) -> AssetBundle:
    db_path = os.path.abspath(db_path or DEFAULT_DB)
    fonts_dir = os.path.abspath(fonts_dir or DEFAULT_FONTS)
    metrics_path = os.path.abspath(metrics_path or DEFAULT_METRICS)

    # In a multi-node deployment, file mtimes can differ between checkouts and
    # split the cache key across nodes (never wrong, just wasteful).  Pin
    # QURAN_ASSET_VERSION to a release tag / content digest to keep every node
    # and every device on one key.
    pinned = os.environ.get("QURAN_ASSET_VERSION")
    if pinned:
        version = re.sub(r"[^A-Za-z0-9._-]", "", pinned)[:32] or "pinned"
    else:
        h = hashlib.sha256()
        h.update(f"quran-image/assets/v1|layout:{LAYOUT_VERSION}".encode())
        _stamp(h, "db", db_path)
        _stamp(h, "metrics", metrics_path)
        for name in sorted(_safe_listdir(fonts_dir)):
            if name.lower().endswith(".ttf"):
                _stamp(h, name, os.path.join(fonts_dir, name))
        version = h.hexdigest()[:16]

    return AssetBundle(
        db_path=db_path,
        fonts_dir=fonts_dir,
        metrics_path=metrics_path,
        version=version,
    )
