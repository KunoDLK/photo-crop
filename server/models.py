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
