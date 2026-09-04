"""Shared tile machinery for provider image sources.

Mirrors ``tiles.manager.TileService``, but the "decode the page" step is a
source's :meth:`~sources.base.ImageSource.render_tile` call: the same encoded
disk LRU, per-tile concurrency dedupe, worker-thread execution, and
progressive-JPEG encoding. Sources never touch caches or HTTP — they only
render. The version namespaces the cache exactly like a page mtime, so
changing a source's content means bumping its version, never wiping the LRU.
"""
from __future__ import annotations

import asyncio

from ..config import Settings
from ..errors import NotFound
from ..tiles import sqlite_cache as encoded_cache
from ..tiles import encoder
from ..tiles.locks import KeyedLock
from .base import SourceRegistry, TileRequest


class SourceTileService:
    """Caches and serves encoded tiles produced by registered image sources."""

    def __init__(self, settings: Settings, registry: SourceRegistry) -> None:
        self.settings = settings
        self.registry = registry
        self.tiles = encoded_cache.TileCache(settings.cache_dir, settings.cache_bytes)
        self.locks = KeyedLock()

    @staticmethod
    def key(
        source_key: str, book: str, page: str, version: int,
        level: int, tx: int, ty: int,
    ) -> str:
        """Build a cache key for a provider tile.

        The source slug keeps different providers strictly separated; the
        version namespaces content changes.

        Args:
            source_key: The source's ``key`` slug.
            book: Book id.
            page: Page id.
            version: Content version.
            level: Pyramid level (may be negative for procedural sources).
            tx: Tile column.
            ty: Tile row.

        Returns:
            A ``p/<source>/...`` string key for the disk LRU.
        """
        return f"p/{source_key}/{book}/{page}/{version}/{level}/{tx}/{ty}"

    async def get_tile(
        self, book: str, page: str, version: int, level: int, tx: int, ty: int,
    ) -> tuple[bytes, bool]:
        """Return encoded JPEG bytes for a provider tile, caching along the way.

        Args:
            book: Book id (resolves the owning source).
            page: Page id.
            version: Content version.
            level: Pyramid level.
            tx: Tile column.
            ty: Tile row.

        Returns:
            A ``(bytes, from_cache)`` tuple: progressive JPEG bytes and
            whether they came from the disk cache (no render was needed).

        Raises:
            errors.NotFound: If no registered source owns the book.
        """
        source = self.registry.source_for_book(book)
        if source is None:
            raise NotFound(f"book not found: {book}")
        if not source.cacheable:
            # Non-cacheable source (e.g. fractal preview): render fresh every
            # request, touching neither the disk LRU nor the dedupe lock.
            req = TileRequest(book, page, version, level, tx, ty, self.settings.tile_size)
            bgr = await asyncio.to_thread(source.render_tile, req)
            data = encoder.encode_progressive_jpeg(
                bgr, self.settings.jpeg_quality, self.settings.jpeg_progressive
            )
            return data, False
        key = self.key(source.key, book, page, version, level, tx, ty)
        cached = self.tiles.get(key)
        if cached is not None:
            return cached, True

        async with self.locks.acquire(key):
            cached = self.tiles.get(key)
            if cached is not None:
                return cached, True
            req = TileRequest(book, page, version, level, tx, ty, self.settings.tile_size)
            bgr = await asyncio.to_thread(source.render_tile, req)
            data = encoder.encode_progressive_jpeg(
                bgr, self.settings.jpeg_quality, self.settings.jpeg_progressive
            )
            # Provider levels are zoom-native: 0 = whole image on one tile,
            # so zoom is simply ``-level`` (evicted deepest-first).
            self.tiles.put(key, data, zoom=-level)
            return data, False
