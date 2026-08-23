"""HTTP route for tile bytes.

Serves ``GET /tiles/{book}/{page}/{level}/{tx}/{ty}.jpg``. Tiles are deterministic
so responses carry immutable cache headers, letting browsers (and any CDN in
front) satisfy repeat requests without hitting the server.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

router = APIRouter(tags=["tiles"])

_IMMUTABLE = "public, max-age=31536000, immutable"


@router.get("/tiles/{book}/{page}/{level}/{tx}/{ty}.jpg")
async def tile_endpoint(
    book: str, page: str, level: int, tx: int, ty: int, request: Request
) -> Response:
    """Return a single progressive-JPEG tile.

    Args:
        book: Book directory name.
        page: Page filename (without extension; ``.jpg`` suffix is the route).
        level: Pyramid level.
        tx: Tile column.
        ty: Tile row.
        request: FastAPI request (to reach ``app.state`` services).

    Returns:
        A ``image/jpeg`` response with immutable cache headers.
    """
    service = request.app.state.tiles
    data = await service.get_tile(book, page, level, tx, ty)
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": _IMMUTABLE})
