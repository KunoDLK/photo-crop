"""HTTP route for tile bytes.

Serves ``GET /tiles/{book}/{page}/{version}/{level}/{tx}/{ty}.jpg``. The version
is the page file's mtime, making tiles content-addressed so re-saved pages get
fresh URLs. Tiles are deterministic per version, so responses carry immutable
cache headers.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

router = APIRouter(tags=["tiles"])

_IMMUTABLE = "public, max-age=31536000, immutable"


@router.get("/tiles/{book}/{page}/{version}/{level}/{tx}/{ty}.jpg")
async def tile_endpoint(
    book: str, page: str, version: int, level: int, tx: int, ty: int, request: Request
) -> Response:
    """Return a single progressive-JPEG tile.

    Args:
        book: Book directory name.
        page: Page filename (without extension; ``.jpg`` suffix is the route).
        version: Page file mtime (content version) — namespaces the cache.
        level: Pyramid level.
        tx: Tile column.
        ty: Tile row.
        request: FastAPI request (to reach ``app.state`` services).

    Returns:
        A ``image/jpeg`` response with immutable cache headers.
    """
    service = request.app.state.tiles
    data, from_cache = await service.get_tile(book, page, version, level, tx, ty)
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={
            "Cache-Control": _IMMUTABLE,
            "X-Tile-Cache": "hit" if from_cache else "miss",
        },
    )
