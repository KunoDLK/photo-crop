"""HTTP routes for OCR data and text search.

Thin FastAPI layer: resolves services from ``app.state`` and returns the pydantic
response models. Routes are async and hand the blocking OCR work to the thread
pool via ``asyncio.to_thread``, so the event loop (and tile serving) is never
blocked. Search returns immediately, reporting matches found so far plus how many
pages are still being OCR'd in the background.

Access: OCR text is a full copy of the work, so it is gated identically to the
image — served only for pages resolved ``full`` for the requester. Search
results are filtered to pages the requester can see fully.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request

from ..auth.service import Viewer, current_viewer
from ..errors import BadRequest, NotFound
from ..models import OCRPage, SearchResponse
from ..rights.policy import FULL

router = APIRouter(tags=["ocr"])


def _page_access(request: Request, viewer: Viewer, book_id: str, page_id: str) -> dict:
    """Resolve access for one page against the current request's region."""
    zone = request.app.state.region.zone_of_request(request)
    return request.app.state.policy.resolve(viewer, book_id, page_id, zone)


def _granted(request: Request, viewer: Viewer, book_id: str) -> bool:
    """True when the viewer may see the book at all (private requires a grant)."""
    rights = request.app.state.rights
    if rights.book_visibility(book_id) == "public":
        return True
    return viewer.kind == "owner" or (
        viewer.kind == "account" and book_id in viewer.grants
    )


@router.get("/api/books/{book_id}/pages/{page_id}/ocr", response_model=OCRPage)
async def page_ocr_endpoint(
    book_id: str, page_id: str, request: Request,
    viewer: Viewer = Depends(current_viewer),
) -> OCRPage:
    """Return the OCR result for one page (word + line boxes in source px).

    Args:
        book_id: The book directory name.
        page_id: The page filename.
        request: FastAPI request (to reach ``app.state`` services).
        viewer: The current session's identity.

    Returns:
        The page's OCR data; OCR runs on the background worker (blocking here
        only until that single page is ready).

    Raises:
        errors.NotFound: For pages the requester cannot see fully — the text of
            a page is itself a copy of the work, so ``blurred`` and
            ``nonexistent`` pages 404 like missing pages.
    """
    access = _page_access(request, viewer, book_id, page_id)
    if access["status"] != FULL:
        raise NotFound(f"page not found: {page_id}")
    service = request.app.state.ocr
    return await asyncio.to_thread(service.get_page_ocr, book_id, page_id)


@router.get("/api/search", response_model=SearchResponse)
async def search_endpoint(
    request: Request, book: str, q: str, regex: bool = False,
    viewer: Viewer = Depends(current_viewer),
) -> SearchResponse:
    """Search a book's OCR text for a literal string or regex.

    Args:
        book: The book directory name.
        q: Search text or regex pattern.
        regex: Treat ``q`` as a regular expression.
        request: FastAPI request (to reach ``app.state`` services).
        viewer: The current session's identity.

    Returns:
        Matching pages the requester can see fully, plus ``pending`` (pages
        still OCR-ing, whether or not they are visible to the requester).

    Raises:
        errors.BadRequest: If the query is empty.
        errors.NotFound: For private books the viewer has no grant for.
    """
    query = q.strip()
    if not query:
        raise BadRequest("search query is empty")
    if not _granted(request, viewer, book):
        raise NotFound(f"book not found: {book}")
    catalog = request.app.state.catalog
    _, pages = catalog.pages(book)
    service = request.app.state.ocr
    matches, pending = await asyncio.to_thread(service.search, book, pages, query, regex)
    zone = request.app.state.region.zone_of_request(request)
    access_map = request.app.state.policy.resolve_pages(
        viewer, book, [m.page_id for m in matches], zone
    )
    filtered = [m for m in matches if access_map[m.page_id]["status"] == FULL]
    return SearchResponse(book=book, query=q, regex=regex, matches=filtered, pending=pending)
