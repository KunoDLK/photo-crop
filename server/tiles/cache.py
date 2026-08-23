"""Disk-backed, byte-limited LRU cache of encoded tile JPEGs.

Implements the "cache the last used X GB of tiles" requirement. Backed by
``diskcache`` with a ``size_limit`` so eviction is automatic, durable across
restarts, and thread/process safe. Keys are ``book/page/level/tx/ty``.
"""
from __future__ import annotations

from pathlib import Path

from diskcache import Cache


class TileCache:
    """LRU cache of encoded JPEG bytes keyed by tile coordinate."""

    def __init__(self, cache_dir: Path, size_limit_bytes: int) -> None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache = Cache(str(cache_dir), size_limit=size_limit_bytes)

    @staticmethod
    def key(book: str, page: str, level: int, tx: int, ty: int) -> str:
        """Build a stable cache key for a tile.

        Args:
            book: Book directory name.
            page: Page id (filename).
            level: Pyramid level.
            tx: Tile column.
            ty: Tile row.

        Returns:
            A string key safe for the cache backend.
        """
        return f"{book}/{page}/{level}/{tx}/{ty}"

    def get(self, key: str) -> bytes | None:
        """Return cached tile bytes, or ``None`` on a miss.

        Args:
            key: Cache key produced by :meth:`key`.
        """
        return self._cache.get(key, default=None)

    def put(self, key: str, data: bytes) -> None:
        """Store encoded tile bytes.

        Args:
            key: Cache key produced by :meth:`key`.
            data: Encoded JPEG bytes.
        """
        self._cache.set(key, data)

    def contains(self, key: str) -> bool:
        """Return True if the tile is cached.

        Args:
            key: Cache key produced by :meth:`key`.
        """
        return key in self._cache
