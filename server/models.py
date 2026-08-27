"""Pydantic response schemas shared across the API.

These models are the contract between the server and the client viewer: listings
report each image's dimensions and ``max_level`` so the client can compute tile
geometry and the coarsest (root) tile without ever decoding an image.
"""
from __future__ import annotations

from pydantic import BaseModel


class AccessInfo(BaseModel):
    """Resolved access for one page: ``full``, ``blurred`` or ``nonexistent``.

    ``until`` is present on blurred pages that have a known public-domain date
    (e.g. ``"1 Jan 2026"``) so the client can render the overlay text.
    ``region_locked`` is True unless the page resolves ``full`` for an
    anonymous viewer in every zone — the only case where real tiles may be
    cached publicly (Cloudflare edge); locked content is served ``private``.
    """

    status: str
    zone: str
    until: str | None = None
    region_locked: bool = True


class CoverInfo(BaseModel):
    """Cover metadata for a book (the first page of its first group).

    ``access`` is the cover page's resolved access, so the root view knows
    which tile variant (``/rt/`` real vs ``/bx/`` blurred) to request.
    """

    page_id: str
    width: int
    height: int
    max_level: int
    mtime: int
    access: AccessInfo | None = None
    source: str = "archive"


class BookSummary(BaseModel):
    """A single book as shown in the root view."""

    id: str
    name: str
    cover: CoverInfo
    visibility: str = "private"


class PageInfo(BaseModel):
    """A single page inside a book."""

    page_id: str
    name: str
    group: int
    order: str
    width: int
    height: int
    max_level: int
    mtime: int
    access: AccessInfo | None = None
    source: str = "archive"


class BooksResponse(BaseModel):
    """Response body for ``GET /api/books``."""

    books: list[BookSummary]
    signature: str


class PagesResponse(BaseModel):
    """Response body for ``GET /api/books/{book}/pages``."""

    book: str
    pages: list[PageInfo]
    signature: str


class ImageInfo(BaseModel):
    """Detailed metadata for a single image, including a content hash."""

    page_id: str
    width: int
    height: int
    max_level: int
    file_size: int
    hash: str
    access: AccessInfo | None = None
    source: str = "archive"


class OCRWord(BaseModel):
    """A single recognized word with its bounding box in source-pixel coords."""

    x: int
    y: int
    w: int
    h: int
    text: str
    conf: float


class OCRLine(BaseModel):
    """A reconstructed line (words grouped by Tesseract line id)."""

    x: int
    y: int
    w: int
    h: int
    text: str


class OCRPage(BaseModel):
    """OCR result for one page (word + line boxes in source-pixel coords)."""

    page_id: str
    width: int
    height: int
    version: int
    lines: list[OCRLine]
    words: list[OCRWord]


class SearchHit(BaseModel):
    """A page whose OCR text matched the query, with the matching words."""

    page_id: str
    hits: list[OCRWord]


class SearchResponse(BaseModel):
    """Response body for ``GET /api/search``."""

    book: str
    query: str
    regex: bool
    matches: list[SearchHit]
    pending: int = 0


class MeResponse(BaseModel):
    """Response body for ``GET /api/me`` and a successful login."""

    authenticated: bool
    username: str | None = None
    is_owner: bool = False
    grants: list[str] = []
