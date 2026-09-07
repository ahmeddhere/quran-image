"""On-demand Qur'an page-image API (FastAPI).

    GET  /v1/manifest                    what the server can do + asset version
    GET  /v1/pages/{page}?w=&fmt=&v=      render/serve one page image
    POST /v1/pages/{page}                 same, taking raw screen metrics as JSON
    GET  /v1/pages/{page}/layout?w=&v=    the page's per-word pixel boxes (JSON)
    GET  /healthz  /v1/stats

The device flow (see ``SERVER.md``):

1. read the real screen: ``physical_w = logical_w * devicePixelRatio``
2. ``GET /v1/pages/{page}?w={physical_w}&fmt=webp&v={asset_version}``
3. server negotiates ``w`` to a canonical rung, renders *iff* not cached,
   returns the image immediately with a strong ``ETag``
4. device stores the bytes keyed by the returned ``Content-Location``
   (or ``ETag``); subsequent loads are local, and a re-fetch sends
   ``If-None-Match`` for a cheap ``304``.

Run::

    uvicorn quran_image.server:app --workers 4
    python -m quran_image.server --port 8080 --warm      # dev convenience
"""
from __future__ import annotations

import argparse
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .assets import load_bundle
from .dimensions import (
    DEFAULT_WIDTH,
    FORMATS,
    MAX_WIDTH,
    MIN_WIDTH,
    PAGE_MAX,
    PAGE_MIN,
    PHI,
    WIDTH_LADDER,
    RenderSpec,
    negotiate,
    parse_pages,
)
from .service import RenderBusy, RenderService, RenderUnavailable

_IMMUTABLE = "public, max-age=31536000, immutable"
_REVALIDATE = "public, max-age=86400, stale-while-revalidate=604800"


def _cfg(name: str, default: str) -> str:
    return os.environ.get(name, default)


# --------------------------------------------------------------------------- #
# app factory
# --------------------------------------------------------------------------- #
def create_app(
    *,
    cache_dir: str | None = None,
    fonts_dir: str | None = None,
    db_path: str | None = None,
    workers: int = 0,
    disk_max_bytes: int | None = None,
    max_concurrent_renders: int = 0,
    bg_workers: int = 0,
    bg_max_queued: int = 0,
    render_fn=None,
    layout_fn=None,
) -> FastAPI:
    bundle = load_bundle(db_path, fonts_dir, None)
    service = RenderService(
        bundle,
        cache_dir=cache_dir or _cfg("QURAN_CACHE_DIR", os.path.join(os.getcwd(), "cache")),
        workers=workers or int(_cfg("QURAN_WORKERS", "0")),
        max_concurrent_renders=max_concurrent_renders,
        bg_workers=bg_workers or int(_cfg("QURAN_BG_WORKERS", "0")),
        bg_max_queued=bg_max_queued or int(_cfg("QURAN_BG_MAX_QUEUED", "64")),
        disk_max_bytes=disk_max_bytes
        or int(_cfg("QURAN_DISK_CACHE_BYTES", str(2 * 1024**3))),
        render_fn=render_fn,
        layout_fn=layout_fn,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        service.start()
        yield
        service.close()

    app = FastAPI(
        title="Qur'an page-image service",
        version=__version__,
        summary="Renders Madani Mushaf pages on demand at a device's screen resolution.",
        lifespan=lifespan,
    )
    app.state.service = service
    app.state.bundle = bundle
    _mount_routes(app)
    return app


# --------------------------------------------------------------------------- #
# request / response models
# --------------------------------------------------------------------------- #
class ScreenMetrics(BaseModel):
    width_px: float | None = Field(
        None, description="logical/CSS width of the page viewport (dp)"
    )
    height_px: float | None = Field(None, description="logical/CSS height (dp)")
    dpr: float | None = Field(None, description="devicePixelRatio / display density")
    physical_width_px: float | None = Field(
        None, description="pre-computed physical width; wins over width_px*dpr"
    )


class PageRequest(BaseModel):
    screen: ScreenMetrics | None = None
    width: int | None = Field(None, description="explicit physical render width (px)")
    format: str = Field("png", pattern="^(png|webp)$")
    max_width: int | None = Field(None, description="hard client cap on render width")


def _spec_from_query(w, sw, sh, dpr, fmt, max_w) -> RenderSpec:
    return negotiate(
        w=w,
        screen_width_px=sw,
        screen_height_px=sh,
        dpr=dpr,
        fmt=fmt,
        max_width=max_w,
    )


def _spec_from_body(req: PageRequest) -> RenderSpec:
    w = req.width
    sw = sh = dpr = None
    if req.screen is not None:
        if req.screen.physical_width_px:
            w = w or req.screen.physical_width_px
        sw, sh, dpr = req.screen.width_px, req.screen.height_px, req.screen.dpr
    return negotiate(
        w=w, screen_width_px=sw, screen_height_px=sh, dpr=dpr,
        fmt=req.format, max_width=req.max_width,
    )


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
def _mount_routes(app: FastAPI) -> None:
    service: RenderService = app.state.service

    def _canonical_url(page: int, spec: RenderSpec) -> str:
        return (
            f"/v1/pages/{page}?w={spec.width}&fmt={spec.fmt}"
            f"&v={service.bundle.version}"
        )

    def _canonical_layout_url(page: int, spec: RenderSpec) -> str:
        return f"/v1/pages/{page}/layout?w={spec.width}&v={service.bundle.version}"

    def _serve(request: Request, page: int, spec: RenderSpec, v: str | None) -> Response:
        if not (PAGE_MIN <= page <= PAGE_MAX):
            return JSONResponse(
                {"error": f"page out of range 1..{PAGE_MAX}"}, status_code=404
            )
        try:
            (data, content_type, etag), state, served = service.get_or_fallback(
                page, spec
            )
        except RenderUnavailable as e:
            return JSONResponse({"error": str(e)}, status_code=503)
        except RenderBusy as e:
            return JSONResponse(
                {"error": str(e)}, status_code=503, headers={"Retry-After": "2"}
            )

        fresh = v is not None and v == service.bundle.version
        fallback = state == "FALLBACK"
        headers = {
            "ETag": etag,
            # a fallback is transient - never let it be cached, so the device
            # comes back for the exact width (a fast-path hit once the
            # background render lands).
            "Cache-Control": "no-store"
            if fallback
            else (_IMMUTABLE if fresh else _REVALIDATE),
            "Vary": "Accept",
            "Content-Location": _canonical_url(page, served),
            "X-Cache": state,
            "X-Asset-Version": service.bundle.version,
            "X-Render-Width": str(served.width),
            "X-Requested-Width": str(spec.requested_width),
            "X-Image-Height": str(served.height),
        }
        if fallback:
            headers["X-Fallback"] = "1"
            headers["X-Target-Width"] = str(spec.width)

        inm = request.headers.get("if-none-match", "")
        if etag in inm or inm.strip() == "*":
            return Response(status_code=304, headers=headers)

        if request.method == "HEAD":
            headers["Content-Length"] = str(len(data))
            return Response(status_code=200, headers=headers, media_type=content_type)
        return Response(content=data, media_type=content_type, headers=headers)

    def _serve_layout(
        request: Request, page: int, spec: RenderSpec, v: str | None
    ) -> Response:
        if not (PAGE_MIN <= page <= PAGE_MAX):
            return JSONResponse(
                {"error": f"page out of range 1..{PAGE_MAX}"}, status_code=404
            )
        try:
            (data, content_type, etag), state, served = service.get_layout_or_fallback(
                page, spec
            )
        except RenderUnavailable as e:
            return JSONResponse({"error": str(e)}, status_code=503)
        except RenderBusy as e:
            return JSONResponse(
                {"error": str(e)}, status_code=503, headers={"Retry-After": "2"}
            )

        fresh = v is not None and v == service.bundle.version
        fallback = state == "FALLBACK"
        headers = {
            "ETag": etag,
            "Cache-Control": "no-store"
            if fallback
            else (_IMMUTABLE if fresh else _REVALIDATE),
            "Content-Location": _canonical_layout_url(page, served),
            "X-Cache": state,
            "X-Asset-Version": service.bundle.version,
            "X-Render-Width": str(served.width),
            "X-Requested-Width": str(spec.requested_width),
            "X-Image-Height": str(served.height),
        }
        if fallback:
            headers["X-Fallback"] = "1"
            headers["X-Target-Width"] = str(spec.width)

        inm = request.headers.get("if-none-match", "")
        if etag in inm or inm.strip() == "*":
            return Response(status_code=304, headers=headers)

        if request.method == "HEAD":
            headers["Content-Length"] = str(len(data))
            return Response(status_code=200, headers=headers, media_type=content_type)
        return Response(content=data, media_type=content_type, headers=headers)

    # -- discovery ----------------------------------------------------------- #
    @app.get("/healthz")
    def healthz() -> dict:
        return {
            "status": "ok" if service.renderable else "degraded",
            "renderable": service.renderable,
            "asset_version": service.bundle.version,
            "missing_assets": service.bundle.missing(),
        }

    @app.get("/v1/manifest")
    def manifest() -> dict:
        return {
            "asset_version": service.bundle.version,
            "pages": {"min": PAGE_MIN, "max": PAGE_MAX, "count": PAGE_MAX - PAGE_MIN + 1},
            "formats": list(FORMATS),
            "aspect_ratio": PHI,
            "height_formula": "height = floor(width * aspect_ratio)",
            "width": {
                "min": MIN_WIDTH,
                "max": MAX_WIDTH,
                "default": DEFAULT_WIDTH,
                "ladder": list(WIDTH_LADDER),
                "note": "requested width snaps up to the next ladder rung",
            },
            "cache": {
                "server_side": True,
                "canonical_query_params": ["w", "fmt", "v"],
                "immutable_when": "query param v == asset_version",
                "revalidate_with": "If-None-Match against the returned ETag",
            },
            "endpoints": {
                "image_get": "/v1/pages/{page}?w={physical_px}&fmt={png|webp}&v={asset_version}",
                "image_post": "/v1/pages/{page}  (JSON body: screen metrics)",
                "layout_get": "/v1/pages/{page}/layout?w={physical_px}&v={asset_version}",
            },
        }

    @app.get("/v1/stats")
    def stats() -> dict:
        return service.stats()

    # -- images ------------------------------------------------------------ #
    @app.api_route("/v1/pages/{page}", methods=["GET", "HEAD"])
    def get_page(
        request: Request,
        page: int = Path(description="Mushaf page number 1..604"),
        w: float | None = Query(None, description="explicit physical render width (px)"),
        sw: float | None = Query(None, description="logical screen/viewport width (dp)"),
        sh: float | None = Query(None, description="logical screen height (dp)"),
        dpr: float | None = Query(None, description="devicePixelRatio"),
        fmt: str = Query("png", pattern="^(png|webp)$"),
        max_w: float | None = Query(None, description="hard cap on render width"),
        v: str | None = Query(None, description="asset version for immutable caching"),
    ) -> Response:
        try:
            spec = _spec_from_query(w, sw, sh, dpr, fmt, max_w)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return _serve(request, page, spec, v)

    @app.post("/v1/pages/{page}")
    def post_page(
        request: Request,
        body: PageRequest,
        page: int = Path(description="Mushaf page number 1..604"),
        v: str | None = Query(None),
    ) -> Response:
        try:
            spec = _spec_from_body(body)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return _serve(request, page, spec, v)

    # -- layout (per-word pixel boxes) ------------------------------------- #
    @app.api_route("/v1/pages/{page}/layout", methods=["GET", "HEAD"])
    def get_page_layout(
        request: Request,
        page: int = Path(description="Mushaf page number 1..604"),
        w: float | None = Query(None, description="explicit physical render width (px)"),
        sw: float | None = Query(None, description="logical screen/viewport width (dp)"),
        sh: float | None = Query(None, description="logical screen height (dp)"),
        dpr: float | None = Query(None, description="devicePixelRatio"),
        max_w: float | None = Query(None, description="hard cap on render width"),
        v: str | None = Query(None, description="asset version for immutable caching"),
    ) -> Response:
        """The word geometry of one page, in the pixel space of the page image
        rendered at the same negotiated width - fetch it alongside the image."""
        try:
            spec = _spec_from_query(w, sw, sh, dpr, "png", max_w)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return _serve_layout(request, page, spec, v)


# default ASGI app (uvicorn quran_image.server:app)
app = create_app()


# --------------------------------------------------------------------------- #
# dev entry point
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="run the Qur'an page-image API")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--fonts", default=None)
    ap.add_argument("--db", default=None)
    ap.add_argument("--workers", type=int, default=0, help="render pool size")
    ap.add_argument("--disk-cache-gb", type=float, default=2.0)
    ap.add_argument(
        "--warm",
        nargs="?",
        const="1..20",
        default=None,
        help="pre-render pages (e.g. --warm 1..604) at the common widths",
    )
    ap.add_argument("--warm-widths", default="720,1080,1242")
    args = ap.parse_args(argv)

    import uvicorn

    application = create_app(
        cache_dir=args.cache_dir,
        fonts_dir=args.fonts,
        db_path=args.db,
        workers=args.workers,
        disk_max_bytes=int(args.disk_cache_gb * 1024**3),
    )

    if args.warm:
        _warm(application, args.warm, args.warm_widths)

    uvicorn.run(application, host=args.host, port=args.port)
    return 0


def _warm(application: FastAPI, pages_spec: str, widths_spec: str) -> None:
    svc: RenderService = application.state.service
    svc.start()
    if not svc.renderable:
        print("warm skipped: assets not available")
        return
    pages = parse_pages(pages_spec)
    specs = [
        negotiate(w=float(x)) for x in widths_spec.split(",") if x.strip()
    ]
    print(f"warming {len(pages)} page(s) x {len(specs)} width(s) ...")
    n = svc.warm(pages, specs)
    print(f"warm done: {n} entries cached -> {svc.disk.stats()}")


if __name__ == "__main__":
    raise SystemExit(main())
