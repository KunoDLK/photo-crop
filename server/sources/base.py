"""Pluggable image sources: the hook between the tile server and providers.

The books and tiles routers only ever talk to :class:`SourceRegistry`; the
fractal generator (and any future image server) implements :class:`ImageSource`
and is registered in ``app.py``. Sources know nothing about HTTP, sessions,
rights, or caches — they answer "which books/pages do I provide" and "render
this tile". Shared tile machinery (disk LRU, concurrency dedupe, JPEG
encoding) lives in :mod:`sources.service`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np

from ..models import BookSummary, ImageInfo, PageInfo


@dataclass(frozen=True)
class TileRequest:
    """Everything a source needs to render one tile, on a worker thread."""

    book: str
    page: str
    version: int
    level: int
    tx: int
    ty: int
    tile_size: int


class ImageSource(ABC):
    """A pluggable book/page/tile provider hooked into the viewer.

    Subclasses declare the book ids they own (``owns``) and render tiles on
    demand (``render_tile``). Listings default to empty/absent; only sources
    that want to appear in the root listing override ``list_books``. A
    source's ``pages()`` returning a result for a book id shadows the archive
    catalog (the registry is consulted first), so source book ids must not
    collide with real archive directories.

    Implementations must be deterministic: the same ``TileRequest`` always
    produces the same bytes, so tiles cache immutably.
    """

    #: Stable URL/cache namespace slug; never change it between restarts.
    key: str = "source"

    #: Cache-Control for this source's tiles. Procedural bytes are identical
    #: for every requester, so the default is public; a session-bound source
    #: overrides this with ``private``.
    cache_control: str = "public, max-age=31536000, immutable"

    #: Whether rendered tiles may be stored in the shared disk LRU. Set to
    #: False for sources that must render fresh on every request (e.g. an
    #: interactive preview); the tile service then skips both lookup and store,
    #: while ``cache_control`` still governs browser/edge caching.
    cacheable: bool = True

    def owns(self, book_id: str) -> bool:
        """True when this source serves ``book_id`` (cheap check, no scanning).

        Args:
            book_id: The book id from the URL.

        Returns:
            Whether the source claims the id. Defaults to False.
        """
        return False

    def list_books(self) -> list[BookSummary]:
        """Books to append to the root listing, each with cover metadata.

        Returns:
            An empty list by default; override to appear in the root view.
        """
        return []

    def pages(self, book_id: str) -> tuple[str, list[PageInfo]] | None:
        """Return ``(signature, pages)`` for an owned book, else ``None``.

        Args:
            book_id: The book id from the URL.

        Returns:
            A ``(change signature, page list)`` pair when the source owns the
            book, ``None`` when it does not (the archive path takes over).
        """
        return None

    def image_info(self, book_id: str, page_id: str) -> ImageInfo | None:
        """Return detailed metadata for one owned page, else ``None``.

        Args:
            book_id: The book id from the URL.
            page_id: The page id from the URL.

        Returns:
            The page's ``ImageInfo`` when owned, ``None`` otherwise.
        """
        return None

    @abstractmethod
    def render_tile(self, req: TileRequest) -> np.ndarray:
        """Render one ``tile_size`` square tile as a BGR uint8 ndarray.

        Runs on a worker thread (never on the event loop). Pure function of
        the request.

        Args:
            req: The tile coordinate and size.

        Returns:
            A ``(tile_size, tile_size, 3)`` uint8 BGR image.

        Raises:
            errors.NotFound: For unknown book/page ids.
            errors.BadRequest: For out-of-range levels or tile coordinates.
        """
        raise NotImplementedError

    @property
    def signature(self) -> str:
        """Change signature for this source's listings (ids + versions)."""
        return self.key

    def tile_zoom(self, level: int) -> int | None:
        """Cache eviction depth for a rendered tile at ``level``.

        Sources whose level 0 is the whole image (fractal-style, levels run
        ``0, -1, -2, ...``) leave this as ``None`` and the tile service stores
        ``-level`` (0 = whole image, evicted last). A source that reuses the
        archive-style pyramid (``0`` = 1:1 up to a positive ``max_level``)
        overrides this with ``max_level - level`` so eviction still prefers
        deep zoom tiles over overview tiles.

        Args:
            level: The pyramid level of the requested tile.

        Returns:
            The tile's depth from the whole-image level, or ``None`` for the
            provider default.
        """
        return None


class SourceRegistry:
    """Ordered collection of :class:`ImageSource` providers.

    The only object the routers touch. Consulted before the archive catalog:
    sources win over real archive directories for the ids they own, and the
    first registered source wins a disputed id.
    """

    def __init__(self, sources: list[ImageSource]) -> None:
        self._sources = list(sources)

    def source_for_book(self, book_id: str) -> ImageSource | None:
        """Return the source that owns ``book_id``, or ``None``.

        Args:
            book_id: The book id from the URL.

        Returns:
            The owning source, or ``None`` when no source claims the id.
        """
        for source in self._sources:
            if source.owns(book_id):
                return source
        return None

    def list_books(self) -> list[BookSummary]:
        """Every provider book for the root listing, deduped by id.

        Returns:
            Books from all sources in registration order; a book id claimed
            by more than one source appears once (first wins).
        """
        seen: set[str] = set()
        books: list[BookSummary] = []
        for source in self._sources:
            for book in source.list_books():
                if book.id not in seen:
                    seen.add(book.id)
                    books.append(book)
        return books

    def pages(self, book_id: str) -> tuple[str, list[PageInfo]] | None:
        """Pages for a book owned by a source, else ``None``.

        Args:
            book_id: The book id from the URL.

        Returns:
            A ``(signature, pages)`` pair from the owning source, or ``None``
            when no source owns the book.
        """
        source = self.source_for_book(book_id)
        return source.pages(book_id) if source is not None else None

    def image_info(self, book_id: str, page_id: str) -> ImageInfo | None:
        """Detailed metadata for a page owned by a source, else ``None``.

        Args:
            book_id: The book id from the URL.
            page_id: The page id from the URL.

        Returns:
            The owning source's ``ImageInfo``, or ``None``.
        """
        source = self.source_for_book(book_id)
        return source.image_info(book_id, page_id) if source is not None else None

    @property
    def signature(self) -> str:
        """Combined change signature of every registered source."""
        return ",".join(source.signature for source in self._sources)
