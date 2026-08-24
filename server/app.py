"""FastAPI application assembly.

Wires the pieces together: builds the app, installs error handlers, registers the
books and tiles routers, exposes shared services on ``app.state``, mounts the
static viewer, and runs startup/shutdown lifecycle hooks.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from .books import router as books_router
from .books.locations import LocationRegistry
from .books.scanner import Catalog
from .config import Settings
from .errors import register_error_handlers
from .ocr import router as ocr_router
from .ocr.service import OCRService
from .tiles import router as tiles_router
from .tiles.manager import TileService


class NoCacheStaticFiles(StaticFiles):
    """StaticFiles that sends ``no-cache`` so the viewer HTML/JS/CSS is always
    revalidated during development (tiles are served separately and stay immutable)."""

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


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

    register_error_handlers(app)
    app.include_router(books_router.router)
    app.include_router(tiles_router.router)
    app.include_router(ocr_router.router)

    app.mount("/", NoCacheStaticFiles(directory=str(_static_dir()), html=True), name="static")
    return app


def _static_dir() -> Path:
    """Resolve the packaged ``static/`` directory next to this module."""
    return Path(__file__).resolve().parent / "static"
