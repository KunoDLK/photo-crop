"""HTTP routes for provider tiles.

``GET /pv/{book}/{page}/{version}/{level}/{tx}/{ty}.jpg`` serves tiles for any
registered :class:`~sources.base.ImageSource`. One generic route covers every
source (procedural generators now, future image-server adapters later): the
registry resolves the book id to its owning source, the shared service
renders and caches. Cache headers come from the source itself — procedural
bytes are deterministic and public; a session-bound source would override
``cache_control`` with ``private``.

``GET /api/mosaic/{book}/proxy`` exposes a mosaic page's proxy-generation
state so a client can hold off tile requests while the coarse band warms and
show the progress; the same state rides on every tile response as
``X-Mosaic-*`` headers.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..errors import NotFound
from ..models import ProxyStatus
from .base import ImageSource

router = APIRouter(tags=["provider-tiles"])


def _proxy_headers(source: ImageSource | None, page: str) -> dict[str, str]:
    """Proxy-generation headers for a tile response, when the source has them.

    Sources that do not expose ``proxy_state`` (fractal, future adapters) and
    proxy-disabled mosaics contribute nothing, so those responses are byte-
    and header-identical to today's. The state read is short-lived cached
    inside the source, so this adds no per-tile store query.

    Args:
        source: The source that produced the tile.
        page: The tile's page id (a mosaic book may serve several pages).

    Returns:
        ``X-Mosaic-Ready``/``X-Mosaic-Progress`` for mosaic sources, else ``{}``.
    """
    probe = getattr(source, "proxy_state", None)
    if probe is None:
        return {}
    state = probe(page)
    if not state.enabled:
        return {}
    return {
        "X-Mosaic-Ready": "1" if state.ready else "0",
        "X-Mosaic-Progress": str(state.percent),
    }


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
    headers = {
        "Cache-Control": source.cache_control,
        "X-Tile-Cache": "hit" if from_cache else "miss",
    }
    headers.update(_proxy_headers(source, page))
    return Response(
        content=data,
        media_type="image/jpeg",
        headers=headers,
    )


@router.get("/api/mosaic/{book}/proxy", response_model=ProxyStatus)
def mosaic_proxy_status_endpoint(
    book: str, request: Request, page: str | None = None,
) -> ProxyStatus:
    """Report a mosaic page's proxy-generation state and start warming if needed.

    A client that finds the page not ``ready`` holds off tile requests and
    shows ``percent`` as image progress; because a page can only be rendered
    once its chains exist, this endpoint also kicks off the background sweep
    (:meth:`~sources.mosaic.MosaicSource.ensure_warming`) so polling alone
    drives generation forward.

    Args:
        book: Book id (resolves the owning source).
        request: FastAPI request (to reach ``app.state`` services).
        page: Page id to report; defaults to the book's cover page.

    Returns:
        The page's :class:`~models.ProxyStatus`.

    Raises:
        errors.NotFound: If the book is unknown, its source has no proxies, or
            the page id is not served by the book.
    """
    source = request.app.state.sources.source_for_book(book)
    probe = getattr(source, "proxy_state", None)
    if probe is None:
        raise NotFound(f"book has no proxy status: {book}")
    page_id = page or getattr(source, "default_page_id", None)
    if page_id is None:
        raise NotFound(f"book has no proxy status: {book}")
    status = probe(page_id, fresh=True)
    if not status.ready:
        source.ensure_warming(page_id)
        # Re-probe through the (now warm) status cache so `generating` reflects
        # the sweep that may just have started, without a second store count.
        status = probe(page_id)
    return status
