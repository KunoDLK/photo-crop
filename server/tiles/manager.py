"""Tile request orchestration.

The single seam the router uses. Given a tile coordinate it returns encoded JPEG
bytes, walking: encoded cache → decoded-page cache → mipmap level → crop →
resample → progressive encode → encoded cache. Concurrency is deduped per tile so
identical in-flight requests share one render, and per page so a page is decoded
only once even when many of its tiles arrive together.
"""
from __future__ import annotations

import asyncio
import threading

import numpy as np

from ..books.scanner import page_path
from ..config import Settings
from ..errors import BadRequest
from . import cache as encoded_cache
from . import decoder, encoder, geometry
from . import page_cache as decoded_cache
from .locks import KeyedLock
from .mipmap import PageMipmap


class TileService:
    """Generates (and caches) encoded tiles for the tile HTTP route."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.tiles = encoded_cache.TileCache(settings.cache_dir, settings.cache_bytes)
        self.pages = decoded_cache.PageCache(
            settings.page_cache_bytes, idle_seconds=settings.page_idle_seconds
        )
        self.locks = KeyedLock()
        self._page_locks: dict[str, threading.Lock] = {}
        self._page_locks_guard = threading.Lock()

    def page_key(self, book: str, page: str, version: int) -> str:
        """Cache key for a decoded page (versioned by the file mtime)."""
        return f"{book}/{page}/{version}"

    async def get_tile(self, book: str, page: str, version: int, level: int, tx: int, ty: int) -> bytes:
        """Return encoded JPEG bytes for a tile, caching along the way.

        Args:
            book: Book directory name.
            page: Page filename.
            version: Page file mtime (content version).
            level: Pyramid level.
            tx: Tile column.
            ty: Tile row.

        Returns:
            Progressive JPEG bytes for a ``tile_size`` square (edge tiles padded).

        Raises:
            errors.NotFound: If the page does not exist.
            errors.BadRequest: If the coordinates are out of range.
        """
        key = self.tiles.key(book, page, version, level, tx, ty)
        cached = self.tiles.get(key)
        if cached is not None:
            return cached

        async with self.locks.acquire(key):
            cached = self.tiles.get(key)
            if cached is not None:
                return cached
            data = await asyncio.to_thread(self._render_tile, book, page, version, level, tx, ty)
            self.tiles.put(key, data)
            return data

    def _render_tile(self, book: str, page: str, version: int, level: int, tx: int, ty: int) -> bytes:
        """Decode/build the mipmap level, crop, resample, and encode one tile.

        Runs on a worker thread (called via ``asyncio.to_thread``); performs no
        encoded-tile caching itself — the caller stores the result.
        """
        path = page_path(self.settings.archive_root, book, page)
        mip = self._get_mipmap(book, page, version, path)

        level = int(level)
        if level < 0 or level > mip.max_level:
            raise BadRequest(f"level {level} out of range (0..{mip.max_level})")

        cols, rows = geometry.grid_extent(mip.width, mip.height, self.settings.tile_size, level)
        if tx < 0 or ty < 0 or tx >= cols or ty >= rows:
            raise BadRequest(f"tile ({tx},{ty}) out of range for level {level}")

        crop = geometry.crop_rect(mip.width, mip.height, self.settings.tile_size, level, tx, ty)
        if crop.w <= 0 or crop.h <= 0:
            raise BadRequest(f"tile ({tx},{ty}) out of range for level {level}")

        lv = mip.level(level)
        canvas = np.zeros((self.settings.tile_size, self.settings.tile_size, 3), dtype=np.uint8)
        canvas[0 : crop.h, 0 : crop.w] = lv[crop.y : crop.y + crop.h, crop.x : crop.x + crop.w]
        return encoder.encode_progressive_jpeg(
            canvas, self.settings.jpeg_quality, self.settings.jpeg_progressive
        )

    def _get_mipmap(self, book: str, page: str, version: int, path) -> PageMipmap:
        """Return the decoded page mipmap, decoding it once under a per-page lock."""
        pkey = self.page_key(book, page, version)
        mip = self.pages.get(pkey)
        if mip is not None:
            return mip

        with self._page_locks_guard:
            lock = self._page_locks.setdefault(pkey, threading.Lock())
        with lock:
            mip = self.pages.get(pkey)
            if mip is not None:
                return mip
            source = decoder.decode(path)
            ml = geometry.max_level(source.shape[1], source.shape[0], self.settings.tile_size)
            mip = PageMipmap(pkey, source, ml, self.settings.tile_size)
            self.pages.put(pkey, mip)
            return mip
