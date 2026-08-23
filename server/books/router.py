"""HTTP routes for book and page listings.

Thin FastAPI layer: resolves the :class:`~config.Settings` from ``app.state``,
delegates to :mod:`scanner`, and returns the pydantic response models.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..errors import NotFound
from ..models import BooksResponse, ImageInfo, PagesResponse
from . import scanner

router = APIRouter(tags=["books"])


@router.get("/api/books", response_model=BooksResponse)
def list_books_endpoint(request: Request, force: bool = False) -> BooksResponse:
    """Return the root listing of books with cover metadata.

    Args:
        request: FastAPI request (used to reach ``app.state.catalog``).
        force: Re-scan the archive instead of using the (10-minute) cache.

    Returns:
        All books sorted by name, each with a cover, plus a change signature.
    """
    catalog = request.app.state.catalog
    signature, books = catalog.books(force)
    return BooksResponse(books=books, signature=signature)


@router.get("/api/books/{book_id}/pages", response_model=PagesResponse)
def list_pages_endpoint(book_id: str, request: Request, force: bool = False) -> PagesResponse:
    """Return the page listing for a single book.

    Args:
        book_id: The book directory name.
        request: FastAPI request (used to reach ``app.state.catalog``).
        force: Re-scan the book instead of using the (10-minute) cache.

    Returns:
        The book's pages sorted by ``(group, order)``, plus a change signature.
    """
    catalog = request.app.state.catalog
    signature, pages = catalog.pages(book_id, force)
    return PagesResponse(book=book_id, pages=pages, signature=signature)


@router.get("/api/books/{book_id}/pages/{page_id}/info", response_model=ImageInfo)
def image_info_endpoint(book_id: str, page_id: str, request: Request) -> ImageInfo:
    """Return detailed metadata (dims, file size, content hash) for one image.

    Args:
        book_id: The book directory name.
        page_id: The page filename.
        request: FastAPI request (used to reach ``app.state.settings``).

    Returns:
        The image's dimensions, ``max_level``, file size and content hash.
    """
    settings = request.app.state.settings
    return scanner.image_info(settings.archive_root, book_id, page_id, settings.tile_size)


@router.get("/api/locations")
def get_location_id_endpoint(request: Request, book: str, page: str | None = None) -> dict:
    """Return (creating if needed) the short id for a ``(book, page)`` location.

    Args:
        book: The book directory name.
        page: Optional page filename.
        request: FastAPI request (to reach ``app.state.locations``).

    Returns:
        ``{"id": "<short-id>"}``.
    """
    ident = request.app.state.locations.get_id(book, page)
    return {"id": ident}


@router.get("/api/locations/{ident}")
def resolve_location_endpoint(ident: str, request: Request) -> dict:
    """Resolve a short location id back to its ``{book, page}``.

    Args:
        ident: The short base62 location id.
        request: FastAPI request (to reach ``app.state.locations``).

    Returns:
        ``{"book": ..., "page": ...}`` (``page`` may be null).

    Raises:
        errors.NotFound: If the id is unknown.
    """
    loc = request.app.state.locations.resolve(ident)
    if loc is None:
        raise NotFound(f"unknown location: {ident}")
    return loc
