"""HTTP routes for OCR data and text search.

Thin FastAPI layer: resolves services from ``app.state`` and returns the pydantic
response models. Routes are async and hand the blocking OCR work to the thread
pool via ``asyncio.to_thread``, so the event loop (and tile serving) is never
blocked. Search returns immediately, reporting matches found so far plus how many
pages are still being OCR'd in the background.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from ..errors import BadRequest
from ..models import OCRPage, SearchResponse

router = APIRouter(tags=["ocr"])


@router.get("/api/books/{book_id}/pages/{page_id}/ocr", response_model=OCRPage)
async def page_ocr_endpoint(book_id: str, page_id: str, request: Request) -> OCRPage:
    """Return the OCR result for one page (word + line boxes in source px).

    Args:
        book_id: The book directory name.
        page_id: The page filename.
        request: FastAPI request (to reach ``app.state.ocr``).

    Returns:
        The page's OCR data; OCR runs on the background worker (blocking here
        only until that single page is ready).
    """
    service = request.app.state.ocr
    return await asyncio.to_thread(service.get_page_ocr, book_id, page_id)


@router.get("/api/search", response_model=SearchResponse)
async def search_endpoint(
    request: Request, book: str, q: str, regex: bool = False
) -> SearchResponse:
    """Search a book's OCR text for a literal string or regex.

    Args:
        book: The book directory name.
        q: Search text or regex pattern.
        regex: Treat ``q`` as a regular expression.
        request: FastAPI request (to reach ``app.state`` services).

    Returns:
        Matching pages found so far, plus ``pending`` (pages still OCR-ing).

    Raises:
        errors.BadRequest: If the query is empty.
    """
    query = q.strip()
    if not query:
        raise BadRequest("search query is empty")
    catalog = request.app.state.catalog
    _, pages = catalog.pages(book)
    service = request.app.state.ocr
    matches, pending = await asyncio.to_thread(service.search, book, pages, query, regex)
    return SearchResponse(book=book, query=q, regex=regex, matches=matches, pending=pending)
