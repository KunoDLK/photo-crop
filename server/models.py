"""Pydantic response schemas shared across the API.

These models are the contract between the server and the client viewer: listings
report each image's dimensions and ``max_level`` so the client can compute tile
geometry and the coarsest (root) tile without ever decoding an image.
"""
from __future__ import annotations

from pydantic import BaseModel


class CoverInfo(BaseModel):
    """Cover metadata for a book (the first page of its first group)."""

    page_id: str
    width: int
    height: int
    max_level: int
    mtime: int


class BookSummary(BaseModel):
    """A single book as shown in the root view."""

    id: str
    name: str
    cover: CoverInfo


class PageInfo(BaseModel):
    """A single page inside a book."""

    page_id: str
    name: str
    group: int
    order: int
    width: int
    height: int
    max_level: int
    mtime: int


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
