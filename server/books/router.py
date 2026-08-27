"""HTTP routes for book and page listings.

Thin FastAPI layer: resolves services from ``app.state``, delegates to
:mod:`scanner`, and applies the access policy — private books are invisible to
anonymous (and ungranted) viewers, public books list every page with its
resolved ``access`` so the client renders blur labels with no extra calls.
"""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, Request

from ..auth.service import Viewer, current_viewer
from ..errors import NotFound
from ..models import AccessInfo, BooksResponse, ImageInfo, PagesResponse
from . import scanner

router = APIRouter(tags=["books"])


def _granted(viewer: Viewer, book_id: str) -> bool:
    """True when the viewer has an explicit grant for a book (owner included)."""
    return viewer.kind == "owner" or (
        viewer.kind == "account" and book_id in viewer.grants
    )


@router.get("/api/books", response_model=BooksResponse)
def list_books_endpoint(
    request: Request,
    force: bool = False,
    viewer: Viewer = Depends(current_viewer),
) -> BooksResponse:
    """Return the root listing of visible books with cover metadata.

    Private books are hidden from anonymous and ungranted viewers; granted
    viewers see them with their ``visibility`` field so the client can badge
    them. The change ``signature`` covers the scanned archive, not the
    per-viewer filter.

    Args:
        request: FastAPI request (used to reach ``app.state`` services).
        force: Re-scan the archive instead of using the (10-minute) cache.
        viewer: The current session's identity.

    Returns:
        All visible books sorted by name, each with a cover and visibility.
    """
    catalog = request.app.state.catalog
    rights = request.app.state.rights
    policy = request.app.state.policy
    region = request.app.state.region
    signature, books = catalog.books(force)
    zone = region.zone_of_request(request)
    visible = []
    for book in books:
        visibility = rights.book_visibility(book.id)
        if visibility == "private" and not _granted(viewer, book.id):
            continue
        access = AccessInfo(**policy.resolve(viewer, book.id, book.cover.page_id, zone))
        cover = book.cover.model_copy(update={"access": access})
        visible.append(book.model_copy(update={"visibility": visibility, "cover": cover}))
    # Provider books (the image-source hook): sources own their access story,
    # so they are appended as-is; the archive rights policy never sees them.
    visible.extend(request.app.state.sources.list_books())
    combined = hashlib.sha256(
        "\n".join(sorted((signature, request.app.state.sources.signature))).encode()
    ).hexdigest()
    return BooksResponse(books=visible, signature=combined)


@router.get("/api/books/{book_id}/pages", response_model=PagesResponse)
def list_pages_endpoint(
    book_id: str,
    request: Request,
    force: bool = False,
    viewer: Viewer = Depends(current_viewer),
) -> PagesResponse:
    """Return the page listing for a single book, annotated with per-page access.

    Args:
        book_id: The book directory name.
        request: FastAPI request (used to reach ``app.state`` services).
        force: Re-scan the book instead of using the (10-minute) cache.
        viewer: The current session's identity.

    Returns:
        The book's pages sorted by ``(group, order)``, each carrying its
        resolved ``access``, plus a change signature.

    Raises:
        errors.NotFound: For private books the viewer has no grant for
            (indistinguishable from a missing book).
    """
    # Provider books (the image-source hook): sources own their access story,
    # so they are resolved before any rights/visibility checks apply.
    sources = request.app.state.sources
    result = sources.pages(book_id)
    if result is not None:
        sig, pages = result
        return PagesResponse(book=book_id, pages=pages, signature=sig)
    if not _granted(viewer, book_id) and not _is_public(request, book_id):
        raise NotFound(f"book not found: {book_id}")
    catalog = request.app.state.catalog
    policy = request.app.state.policy
    region = request.app.state.region
    signature, pages = catalog.pages(book_id, force)
    zone = region.zone_of_request(request)
    access_map = policy.resolve_pages(
        viewer, book_id, [p.page_id for p in pages], zone
    )
    annotated = [
        page.model_copy(update={"access": AccessInfo(**access_map[page.page_id])})
        for page in pages
    ]
    return PagesResponse(book=book_id, pages=annotated, signature=signature)


@router.get("/api/books/{book_id}/pages/{page_id}/info", response_model=ImageInfo)
def image_info_endpoint(
    book_id: str,
    page_id: str,
    request: Request,
    viewer: Viewer = Depends(current_viewer),
) -> ImageInfo:
    """Return detailed metadata (dims, file size, content hash) for one image.

    Args:
        book_id: The book directory name.
        page_id: The page filename.
        request: FastAPI request (used to reach ``app.state`` services).
        viewer: The current session's identity.

    Returns:
        The image's dimensions, ``max_level``, file size, content hash and the
        resolved ``access`` for the current viewer.

    Raises:
        errors.NotFound: For private books without a grant, and for pages whose
            resolved status is ``nonexistent`` (same as a missing page).
    """
    # Provider books (the image-source hook): resolved before rights checks.
    info = request.app.state.sources.image_info(book_id, page_id)
    if info is not None:
        return info
    if not _granted(viewer, book_id) and not _is_public(request, book_id):
        raise NotFound(f"book not found: {book_id}")
    settings = request.app.state.settings
    policy = request.app.state.policy
    region = request.app.state.region
    zone = region.zone_of_request(request)
    access = policy.resolve(viewer, book_id, page_id, zone)
    if access["status"] == "nonexistent":
        raise NotFound(f"page not found: {page_id}")
    info = scanner.image_info(settings.archive_root, book_id, page_id, settings.tile_size)
    info["access"] = access
    return ImageInfo(**info)


def _is_public(request: Request, book_id: str) -> bool:
    """True when the book's rights row marks it public."""
    return request.app.state.rights.book_visibility(book_id) == "public"


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
