"""FastAPI application assembly.

Wires the pieces together: builds the app, installs error handlers, registers the
books and tiles routers, exposes shared services on ``app.state``, mounts the
static viewer, and runs startup/shutdown lifecycle hooks.
"""
from __future__ import annotations

from pathlib import Path
from stat import S_ISREG

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request
from starlette.responses import Response

from . import social
from .admin import router as admin_router
from .auth import router as auth_router
from .auth.service import AuthService
from .books import router as books_router
from .books.locations import LocationRegistry
from .books.scanner import Catalog
from .config import Settings
from .errors import register_error_handlers
from .ocr import router as ocr_router
from .ocr.service import OCRService
from .qr import router as qr_router
from .rights.geo import RegionDetector
from .rights.policy import Policy
from .rights.store import RightsStore
from .sources import router as sources_router
from .sources.base import SourceRegistry
from .sources.fractal import FractalSource
from .sources.service import SourceTileService
from .tiles import router as tiles_router
from .tiles.manager import TileService


class NoCacheStaticFiles(StaticFiles):
    """StaticFiles with viewer-friendly caching and an SPA fallback.

    Assets (``js/``, ``css/``, favicons) are served as files — ``no-cache`` so
    the viewer revalidates during development, favicons cached long-term so
    browsers keep them — while the root, ``/index.html``, and every other path
    (e.g. any share link like ``/93050a0``) serve the viewer page with content
    and Open Graph tags injected by :mod:`social` so crawlers get real content
    and link previews. Tiles are separate and immutable."""

    IMMUTABLE = {"favicon-16.png", "favicon-32.png"}

    async def get_response(self, path: str, scope) -> Response:
        # The root and a direct /index.html request serve the viewer page with
        # injected content and OG tags (not the bare static file), so the root
        # carries the book list and every location is indexable.
        if path in ("", "index.html"):
            return social.spa_response(Request(scope))
        full_path, stat_result = self.lookup_path(path)
        if full_path and stat_result is not None and S_ISREG(stat_result.st_mode):
            response = await super().get_response(path, scope)
            if path in self.IMMUTABLE:
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                response.headers["Cache-Control"] = "no-cache"
            return response
        # Every other unknown path serves the viewer page (the client re-reads
        # the launch path at startup); content + OG tags are injected for the
        # root and bare share-link segments.
        return social.spa_response(Request(scope))


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the FastAPI application.

    Args:
        settings: Service configuration; defaults to :class:`Settings()`.

    Returns:
        A fully wired FastAPI app ready for uvicorn.
    """
    settings = settings or Settings()

    app = FastAPI(title="Book viewer tile server", version="1.0.0")
    app.state.settings = settings
    app.state.tiles = TileService(settings)
    app.state.ocr = OCRService(settings)
    app.state.locations = LocationRegistry(settings.cache_dir / "locations.json")
    app.state.catalog = Catalog(settings.archive_root, settings.tile_size)
    app.state.rights = RightsStore(settings.rights_db)
    app.state.auth = AuthService(settings, app.state.rights)
    app.state.region = RegionDetector(settings.default_region, settings.dev_region_header)
    app.state.policy = Policy(app.state.rights)
    # The image-source hook: registered sources own their book ids and render
    # tiles on demand (the fractal generator is the reference implementation).
    app.state.sources = SourceRegistry([FractalSource()])
    app.state.source_tiles = SourceTileService(settings, app.state.sources)

    register_error_handlers(app)
    app.include_router(books_router.router)
    app.include_router(tiles_router.router)
    app.include_router(ocr_router.router)
    app.include_router(social.router)
    app.include_router(qr_router)
    app.include_router(auth_router.router)
    app.include_router(admin_router.router)
    app.include_router(sources_router.router)

    app.mount("/", NoCacheStaticFiles(directory=str(_static_dir()), html=True), name="static")
    return app


def _static_dir() -> Path:
    """Resolve the packaged ``static/`` directory next to this module."""
    return Path(__file__).resolve().parent / "static"
