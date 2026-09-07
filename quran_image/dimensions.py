"""Turn a device's raw screen metrics into a *canonical render spec*.

The Mushaf page is always the golden-ratio portrait rectangle
(``height == int(width * PHI)``) - that is the layout the calligraphy was set
in and the one point 7 of the brief says we must not touch.  The only thing a
device gets to influence is the **pixel width** the page is rasterised at.

A phone reports a logical width in CSS/dp plus a device-pixel-ratio; the
physical pixel width it wants is ``logical_width * dpr``.  If we cached one
image per distinct physical width the cache would hold thousands of
near-identical variants, so requests **snap up to the next rung of a fixed
ladder** (`WIDTH_LADDER`).  Snapping *up* means the device only ever
downscales the image it receives - text stays crisp, never blurry - while the
number of distinct cache entries per page stays at ~17.

    negotiate(screen_width_px=412, dpr=2.625)  ->  width 1170, height 1893
    negotiate(w=1000)                          ->  width 1000, height 1618
"""
from __future__ import annotations

import math
from dataclasses import dataclass

PHI = (math.sqrt(5) + 1) / 2

# Canonical render widths (physical px, dpr already applied).  Covers the
# common Android / iOS physical widths plus tablet and hi-dpi headroom.
WIDTH_LADDER: tuple[int, ...] = (
    360, 480, 540, 640, 720, 810, 900, 1000, 1080, 1170,
    1242, 1260, 1290, 1350, 1440, 1620, 1792, 2048,
)
# 1260 is the width the app historically shipped - keeping it as a rung means a
# device on that width gets a byte-for-byte copy of the legacy asset.
MIN_WIDTH = WIDTH_LADDER[0]
MAX_WIDTH = WIDTH_LADDER[-1]
DEFAULT_WIDTH = 1080
FORMATS = ("png", "webp")

# The Madani Mushaf is 604 pages.
PAGE_MIN = 1
PAGE_MAX = 604


def parse_pages(spec: str) -> list[int]:
    """Parse ``"N"`` | ``"A..B"`` | comma-separated combinations into a sorted,
    de-duplicated list clamped to ``[PAGE_MIN, PAGE_MAX]`` (used by ``--warm``)."""
    out: list[int] = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if ".." in part:
            a, b = part.split("..")
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return [p for p in sorted(dict.fromkeys(out)) if PAGE_MIN <= p <= PAGE_MAX]


@dataclass(frozen=True)
class RenderSpec:
    """A fully-resolved, cache-stable description of one page image."""

    width: int          # canonical render width - a WIDTH_LADDER rung
    height: int         # int(width * PHI) - the Mushaf is always phi-ratio
    fmt: str            # "png" | "webp"
    requested_width: int  # what the device actually asked for (for logging/headers)

    @property
    def key(self) -> str:
        """The per-page portion of a cache key: ``<width>/<fmt>``."""
        return f"{self.width}/{self.fmt}"


def snap_width(px: float) -> int:
    """Round a desired physical width up to the next ladder rung, clamped."""
    px = int(math.ceil(px))
    if px <= MIN_WIDTH:
        return MIN_WIDTH
    if px >= MAX_WIDTH:
        return MAX_WIDTH
    for rung in WIDTH_LADDER:
        if rung >= px:
            return rung
    return MAX_WIDTH  # unreachable


def negotiate(
    *,
    w: float | None = None,
    screen_width_px: float | None = None,
    screen_height_px: float | None = None,  # accepted, not needed (phi-ratio)
    dpr: float | None = None,
    fmt: str = "png",
    max_width: float | None = None,
) -> RenderSpec:
    """Resolve device metrics to a :class:`RenderSpec`.

    Priority for the target width:
      1. explicit ``w`` (already-physical px), else
      2. ``screen_width_px * dpr``, else
      3. :data:`DEFAULT_WIDTH`.
    ``max_width`` (e.g. a client-imposed data cap) clamps before snapping.
    """
    fmt = (fmt or "png").lower()
    if fmt not in FORMATS:
        raise ValueError(f"unsupported format {fmt!r}; use one of {FORMATS}")

    if w is not None:
        requested = float(w)
    elif screen_width_px is not None:
        requested = float(screen_width_px) * float(dpr if dpr and dpr > 0 else 1.0)
    else:
        requested = float(DEFAULT_WIDTH)

    if not math.isfinite(requested) or requested <= 0:
        raise ValueError(f"nonsensical target width: {requested!r}")
    if max_width and max_width > 0:
        requested = min(requested, float(max_width))

    width = snap_width(requested)
    return RenderSpec(
        width=width,
        height=int(width * PHI),
        fmt=fmt,
        requested_width=int(round(requested)),
    )
