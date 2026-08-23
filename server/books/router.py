"""HTTP routes for book and page listings.

Thin FastAPI layer: resolves the :class:`~config.Settings` from ``app.state``,
delegates to :mod:`scanner`, and returns the pydantic response models.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..models import BooksResponse, PagesResponse
from . import scanner

router = APIRouter(tags=["books"])


@router.get("/api/books", response_model=BooksResponse)
def list_books_endpoint(request: Request) -> BooksResponse:
    """Return the root listing of books with cover metadata.

    Args:
        request: FastAPI request (used to reach ``app.state.settings``).

    Returns:
        All books sorted by name, each with a cover.
    """
    settings = request.app.state.settings
    return BooksResponse(books=scanner.list_books(settings.archive_root, settings.tile_size))


@router.get("/api/books/{book_id}/pages", response_model=PagesResponse)
def list_pages_endpoint(book_id: str, request: Request) -> PagesResponse:
    """Return the page listing for a single book.

    Args:
        book_id: The book directory name.
        request: FastAPI request (used to reach ``app.state.settings``).

    Returns:
        The book's pages sorted by ``(group, order)``.
    """
    settings = request.app.state.settings
    pages = scanner.list_pages(settings.archive_root, book_id, settings.tile_size)
    return PagesResponse(book=book_id, pages=pages)
