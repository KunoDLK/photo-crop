"""Disk-backed, byte-limited LRU cache of encoded tile JPEGs.

Implements the "cache the last used X GB of tiles" requirement. Backed by
``diskcache`` with a ``size_limit`` so eviction is automatic, durable across
restarts, and thread/process safe. Keys are ``t/...`` for real tiles and
``x<gen>/...`` for blurred tiles: the variant lives in the key (the values are
opaque bytes), so a real tile cached from an owner's visit can never be served
to an anonymous viewer, and a blur tile is never served as content.
"""
from __future__ import annotations

from pathlib import Path

from diskcache import Cache

#: Bumped whenever the blur rendering changes: blur-tile keys embed it, so a
#: re-render never serves the old bytes from the disk cache (no manual wipe).
BLUR_GENERATION = 3


class TileCache:
    """LRU cache of encoded JPEG bytes keyed by tile coordinate + variant."""

    def __init__(self, cache_dir: Path, size_limit_bytes: int) -> None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache = Cache(str(cache_dir), size_limit=size_limit_bytes)

    @staticmethod
    def key(
        book: str, page: str, version: int, level: int, tx: int, ty: int,
        blur: bool = False,
    ) -> str:
        """Build a stable cache key for a tile.

        The ``version`` (the page file's mtime) namespaces the cache, so a
        re-saved page produces new keys and stale tiles are never served. The
        ``blur`` flag selects the ``t/`` (real) or ``x<gen>/`` (blurred)
        prefix, keeping the two variants strictly separated in the cache.

        Args:
            book: Book directory name.
            page: Page id (filename).
            version: Page file mtime (content version).
            level: Pyramid level.
            tx: Tile column.
            ty: Tile row.
            blur: True for the blurred variant of the tile.

        Returns:
            A string key safe for the cache backend.
        """
        prefix = f"x{BLUR_GENERATION}" if blur else "t"
        return f"{prefix}/{book}/{page}/{version}/{level}/{tx}/{ty}"

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
