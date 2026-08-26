"""HTTP routes for tile bytes.

Two routes, one per access variant:

- ``GET /rt/{book}/{page}/{version}/{level}/{tx}/{ty}.jpg`` — the **real** tile,
  refused (404) unless the requester's access resolves to ``full``.
- ``GET /bx/{book}/{page}/{version}/{level}/{tx}/{ty}.jpg`` — the **blurred**
  tile, refused (404) unless the requester's access resolves to ``blurred``.

Cache safety differs per variant and per page:

- Blur bytes are identical for every requester allowed to see them, so ``/bx/``
  is always ``public, immutable`` — safe for shared caches (Cloudflare edge,
  browsers).
- Real bytes under ``/rt/`` are shared-cacheable **only when the page is not
  region-locked** (``region_locked: false`` — full for an anonymous viewer in
  every zone, e.g. the owner's own images marked ``public`` with no governing
  editor rule). Those responses are ``public, immutable``. Anything tied to a
  session or region (private books, pending PD rules, ``block`` defaults) is
  ``private, immutable``: cached only in the requester's own browser, so the
  origin's per-request refusal is always authoritative.

The version is the page file's mtime, so re-saved pages get fresh URLs.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ..auth.service import Viewer, current_viewer
from ..errors import NotFound
from ..rights.policy import BLURRED, FULL

router = APIRouter(tags=["tiles"])

#: Real tiles: entitlement differs per viewer/region, so never share-cache them.
_PRIVATE_IMMUTABLE = "private, max-age=31536000, immutable"
#: Real tiles proven open to everyone (not region-locked) + all blur tiles.
_PUBLIC_IMMUTABLE = "public, max-age=31536000, immutable"


def _tile_response(data: bytes, from_cache: bool, cache_control: str) -> Response:
    """An immutable image/jpeg response for a tile."""
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={
            "Cache-Control": cache_control,
            "X-Tile-Cache": "hit" if from_cache else "miss",
        },
    )


@router.get("/rt/{book}/{page}/{version}/{level}/{tx}/{ty}.jpg")
async def real_tile_endpoint(
    book: str, page: str, version: int, level: int, tx: int, ty: int, request: Request,
    viewer: Viewer = Depends(current_viewer),
) -> Response:
    """Return the real progressive-JPEG tile — ``full`` access only.

    Args:
        book: Book directory name.
        page: Page filename (without extension; ``.jpg`` suffix is the route).
        version: Page file mtime (content version) — namespaces the cache.
        level: Pyramid level.
        tx: Tile column.
        ty: Tile row.
        request: FastAPI request (to reach ``app.state`` services).
        viewer: The current session's identity (defaults to anonymous).

    Returns:
        The real tile with immutable cache headers — ``public`` only when the
        page is not region-locked, otherwise ``private`` (browser cache only).

    Raises:
        errors.NotFound: Unless the requester's access is ``full`` — the real
            image never reaches blurred/nonexistent requests (they use ``/bx/``).
    """
    zone = request.app.state.region.zone_of_request(request)
    access = request.app.state.policy.resolve(viewer, book, page, zone)
    if access["status"] != FULL:
        raise NotFound(f"page not found: {page}")
    data, from_cache = await request.app.state.tiles.get_tile(
        book, page, version, level, tx, ty
    )
    cache_control = _PRIVATE_IMMUTABLE if access["region_locked"] else _PUBLIC_IMMUTABLE
    return _tile_response(data, from_cache, cache_control)


@router.get("/bx/{book}/{page}/{version}/{level}/{tx}/{ty}.jpg")
async def blur_tile_endpoint(
    book: str, page: str, version: int, level: int, tx: int, ty: int, request: Request,
    viewer: Viewer = Depends(current_viewer),
) -> Response:
    """Return the blurred progressive-JPEG tile — ``blurred`` access only.

    Same geometry as the real tile so the client's tiling lines up, with all
    detail destroyed. Blur bytes are region-independent, so this URL caches
    publicly for every requester allowed to see it.

    Args:
        book: Book directory name.
        page: Page filename (without extension; ``.jpg`` suffix is the route).
        version: Page file mtime (content version) — namespaces the cache.
        level: Pyramid level.
        tx: Tile column.
        ty: Tile row.
        request: FastAPI request (to reach ``app.state`` services).
        viewer: The current session's identity (defaults to anonymous).

    Returns:
        The blurred tile with public immutable cache headers.

    Raises:
        errors.NotFound: Unless the requester's access is exactly ``blurred``
            — a blur tile is never served as real content.
    """
    zone = request.app.state.region.zone_of_request(request)
    access = request.app.state.policy.resolve(viewer, book, page, zone)
    if access["status"] != BLURRED:
        raise NotFound(f"page not found: {page}")
    data, from_cache = await request.app.state.tiles.get_blur_tile(
        book, page, version, level, tx, ty
    )
    return _tile_response(data, from_cache, _PUBLIC_IMMUTABLE)
