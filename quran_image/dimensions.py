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
number of distinct cache entries per page stays at ~18.

    negotiate(screen_width_px=360, dpr=3)      ->  width 1080, height 1747
    negotiate(w=1000)                          ->  width 1010, height 1634
"""
from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass

PHI = (math.sqrt(5) + 1) / 2

# Canonical render widths (physical px, dpr already applied): an approximately
# geometric ladder over 360..2048 px whose consecutive rungs grow by ~11 %, so
# the worst-case oversize a device has to downscale is ~5.5 % at any width and
# the width error is spread evenly instead of clustering (the old hand-picked
# ladder had 1242/1260/1290 within 1.4 % of each other and a 33 % gap at 360).
# 1260 is force-inserted: it is the width the app historically shipped, so a
# device on that rung still gets a byte-for-byte copy of the legacy asset.
# 1080 is force-inserted too: 1080 px is by far the most common physical panel
# width on Android (1080x2400 at dpr 3, i.e. 360 dp), so pinning it lets those
# devices render 1:1 instead of downscaling a 1120 px image.


def _build_width_ladder(
    lo: int, hi: int, *, ratio: float, pinned: tuple[int, ...]
) -> tuple[int, ...]:
    """Ascending ladder from ``lo`` to ``hi`` stepping up by ~``ratio`` (rungs
    snapped to a multiple of 10).  Each width in ``pinned`` is kept verbatim and
    any generated rung within half a step of it is dropped, so a pinned legacy
    width never spawns a cluster of near-identical rungs."""
    rungs = [lo]
    while rungs[-1] < hi:
        rungs.append(min(hi, int(round(rungs[-1] * ratio / 10)) * 10))
    slack = 1.0 + (ratio - 1.0) / 2.0
    for p in pinned:
        rungs = [w for w in rungs if not (p / slack) < w < (p * slack)]
        rungs.append(p)
    return tuple(sorted(dict.fromkeys(w for w in rungs if lo <= w <= hi)))


# -> 360, 400, 440, 490, 540, 600, 670, 740, 820, 910,
#    1010, 1080, 1260, 1380, 1530, 1700, 1890, 2048
WIDTH_LADDER: tuple[int, ...] = _build_width_ladder(
    360, 2048, ratio=1.11, pinned=(1080, 1260)
)
MIN_WIDTH = WIDTH_LADDER[0]
MAX_WIDTH = WIDTH_LADDER[-1]
DEFAULT_WIDTH = 1080
# PNG is the only supported container: the page is an 8-grey + transparency
# palette image, which PNG stores about as tightly as anything lossless and
# every client decodes without a codec check.
FORMATS = ("png",)

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
    fmt: str            # always "png" - the only supported format
    requested_width: int  # what the device actually asked for (for logging/headers)

    @property
    def key(self) -> str:
        """Compact ``<width>/<fmt>`` tag for this spec (logging / headers)."""
        return f"{self.width}/{self.fmt}"


# How far *above* a rung a request may sit and still snap down to it.  Snapping
# up is the rule (the device downscales, so text stays crisp), but a request
# that overshoots a rung by a hair - 412 dp x 2.625 = 1082 px against the 1080
# rung - would otherwise jump a whole step to 1260: a 16 % oversize image, ~35 %
# more pixels, to avoid a 0.2 % upscale nobody can see.  2 % is well under the
# threshold where upscaling is visible and far under one ladder step (~11 %), so
# it only ever catches these off-by-a-rounding-error cases.
SNAP_DOWN_TOLERANCE = 0.02


def snap_width(px: float) -> int:
    """Snap a desired physical width to a ladder rung, clamped.

    Rounds *up* to the next rung, except when ``px`` sits within
    :data:`SNAP_DOWN_TOLERANCE` of the rung below - then it snaps down to that
    rung and the device upscales by an imperceptible amount.
    """
    px = int(math.ceil(px))
    if px <= MIN_WIDTH:
        return MIN_WIDTH
    if px >= MAX_WIDTH:
        return MAX_WIDTH
    i = bisect_left(WIDTH_LADDER, px)
    if WIDTH_LADDER[i] != px and px <= WIDTH_LADDER[i - 1] * (1 + SNAP_DOWN_TOLERANCE):
        return WIDTH_LADDER[i - 1]
    return WIDTH_LADDER[i]


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
