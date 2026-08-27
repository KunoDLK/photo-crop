"""HTTP routes for provider tiles.

``GET /pv/{book}/{page}/{version}/{level}/{tx}/{ty}.jpg`` serves tiles for any
registered :class:`~sources.base.ImageSource`. One generic route covers every
source (procedural generators now, future image-server adapters later): the
registry resolves the book id to its owning source, the shared service
renders and caches. Cache headers come from the source itself — procedural
bytes are deterministic and public; a session-bound source would override
``cache_control`` with ``private``.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

router = APIRouter(tags=["provider-tiles"])


@router.get("/pv/{book}/{page}/{version}/{level}/{tx}/{ty}.jpg")
async def provider_tile_endpoint(
    book: str,
    page: str,
    version: int,
    level: int,
    tx: int,
    ty: int,
    request: Request,
) -> Response:
    """Return a progressive-JPEG tile from the owning image source.

    Args:
        book: Book id (resolves the owning source).
        page: Page id.
        version: Content version — namespaces the cache.
        level: Pyramid level (negative levels are valid for procedural
            sources: the bottomless half).
        tx: Tile column.
        ty: Tile row.
        request: FastAPI request (to reach ``app.state`` services).

    Returns:
        The provider tile with the source's cache headers and ``X-Tile-Cache``.

    Raises:
        errors.NotFound: If no registered source owns the book.
        errors.BadRequest: If the coordinates are out of range for the source.
    """
    data, from_cache = await request.app.state.source_tiles.get_tile(
        book, page, version, level, tx, ty
    )
    source = request.app.state.sources.source_for_book(book)
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={
            "Cache-Control": source.cache_control,
            "X-Tile-Cache": "hit" if from_cache else "miss",
        },
    )
