"""RAM cache of decoded page mipmaps.

Decoded pages are large (a 74MP page is ~220MB, more once downscaled levels are
built); this cache bounds how many live in memory and, crucially, drops pages
after a short idle period so RAM falls back to near-zero when nothing is being
generated. The encoded-tile disk cache (see :mod:`sqlite_cache`) handles persistence.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict

from .mipmap import PageMipmap


class PageCache:
    """Thread-safe LRU of :class:`PageMipmap` objects, bounded by bytes and idle time.

    Attributes:
        idle_seconds: Drop a page this many seconds after its last access.
    """

    def __init__(self, budget_bytes: int, idle_seconds: float = 10.0, sweep_interval: float = 1.0) -> None:
        self.budget_bytes = budget_bytes
        self.idle_seconds = idle_seconds
        self._items: OrderedDict[str, PageMipmap] = OrderedDict()
        self._sizes: dict[str, int] = {}
        self._last_used: dict[str, float] = {}
        self._bytes = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sweeper = threading.Thread(target=self._sweep_loop, args=(sweep_interval,), daemon=True)
        self._sweeper.start()

    def get(self, key: str) -> PageMipmap | None:
        """Return a cached page, refreshing its idle timer and LRU position.

        Args:
            key: Page cache key (e.g. ``book/page``).

        Returns:
            The :class:`PageMipmap`, or ``None`` if absent.
        """
        with self._lock:
            item = self._items.get(key)
            if item is not None:
                self._items.move_to_end(key)
                self._last_used[key] = time.monotonic()
            return item

    def put(self, key: str, mipmap: PageMipmap) -> None:
        """Insert a page, evicting over-budget pages to fit the byte cap.

        Args:
            key: Page cache key.
            mipmap: The decoded page to cache.
        """
        with self._lock:
            existing = self._items.get(key)
            if existing is not None:
                self._remove(key)
            self._items[key] = mipmap
            size = mipmap.memory_bytes()
            self._sizes[key] = size
            self._last_used[key] = time.monotonic()
            self._bytes += size
            self._evict_locked()

    def evict(self, key: str) -> None:
        """Drop a specific page (e.g. its source changed on disk)."""
        with self._lock:
            self._remove(key)

    def clear(self) -> None:
        """Drop all cached pages."""
        with self._lock:
            self._items.clear()
            self._sizes.clear()
            self._last_used.clear()
            self._bytes = 0

    def stop(self) -> None:
        """Stop the background sweep thread (used on shutdown)."""
        self._stop.set()

    @property
    def resident_count(self) -> int:
        """Number of pages currently held."""
        with self._lock:
            return len(self._items)

    def _remove(self, key: str) -> None:
        """Remove a page and its accounting (caller must hold the lock)."""
        item = self._items.pop(key, None)
        if item is None:
            return
        self._bytes -= self._sizes.pop(key, 0)
        self._last_used.pop(key, None)

    def _evict_locked(self) -> None:
        """Evict least-recently-used pages until within budget (caller holds lock)."""
        while self._bytes > self.budget_bytes and len(self._items) > 1:
            self._remove(next(iter(self._items)))

    def _sweep_locked(self) -> None:
        """Drop pages whose idle timer has expired (caller holds lock)."""
        if self.idle_seconds <= 0:
            return
        now = time.monotonic()
        stale = [key for key, ts in self._last_used.items() if now - ts > self.idle_seconds]
        for key in stale:
            self._remove(key)

    def _sweep_loop(self, interval: float) -> None:
        """Background thread: periodically evict idle pages."""
        while not self._stop.wait(interval):
            with self._lock:
                self._sweep_locked()
