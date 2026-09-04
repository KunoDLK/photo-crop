"""RAM cache of decoded page images.

Large decoded images (a 74MP page is ~220MB, more once downscaled levels are
built; a mosaic cell source is similar) are kept resident so repeated tiles are
cropped from memory instead of re-decoded. This cache bounds how many live at
once and, crucially, drops them after a short idle period so RAM falls back to
near-zero when nothing is being generated. It is shared by every image
provider: archive pages store their decoded mipmaps and the mosaic source
stores its decoded cell bitmaps in the same budget, one LRU, one idle sweeper.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any


class PageCache:
    """Thread-safe LRU of decoded images, bounded by bytes and idle time.

    Accepts any value with a ``memory_bytes()`` method (archive page mipmaps)
    or an explicit byte size (raw arrays, e.g. mosaic cell bitmaps).

    Attributes:
        idle_seconds: Drop an image this many seconds after its last access.
    """

    def __init__(self, budget_bytes: int, idle_seconds: float = 10.0, sweep_interval: float = 1.0) -> None:
        self.budget_bytes = budget_bytes
        self.idle_seconds = idle_seconds
        self._items: OrderedDict[str, Any] = OrderedDict()
        self._sizes: dict[str, int] = {}
        self._last_used: dict[str, float] = {}
        self._bytes = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sweeper = threading.Thread(target=self._sweep_loop, args=(sweep_interval,), daemon=True)
        self._sweeper.start()

    def get(self, key: str) -> Any | None:
        """Return a cached image, refreshing its idle timer and LRU position.

        Args:
            key: Cache key (e.g. ``book/page/version`` or a source cell id).

        Returns:
            The cached object, or ``None`` if absent.
        """
        with self._lock:
            item = self._items.get(key)
            if item is not None:
                self._items.move_to_end(key)
                self._last_used[key] = time.monotonic()
            return item

    def put(self, key: str, value: Any, size_bytes: int | None = None) -> None:
        """Insert an image, evicting over-budget images to fit the byte cap.

        Args:
            key: Cache key.
            value: The decoded image to cache.
            size_bytes: Its memory footprint; defaults to
                ``value.memory_bytes()`` when omitted.
        """
        with self._lock:
            existing = self._items.get(key)
            if existing is not None:
                self._remove(key)
            size = size_bytes if size_bytes is not None else _memory_bytes(value)
            self._items[key] = value
            self._sizes[key] = size
            self._last_used[key] = time.monotonic()
            self._bytes += size
            self._evict_locked()

    def evict(self, key: str) -> None:
        """Drop a specific image (e.g. its source changed on disk)."""
        with self._lock:
            self._remove(key)

    def clear(self) -> None:
        """Drop all cached images."""
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
        """Number of images currently held."""
        with self._lock:
            return len(self._items)

    def _remove(self, key: str) -> None:
        """Remove an image and its accounting (caller must hold the lock)."""
        item = self._items.pop(key, None)
        if item is None:
            return
        self._bytes -= self._sizes.pop(key, 0)
        self._last_used.pop(key, None)

    def _evict_locked(self) -> None:
        """Evict least-recently-used images until within budget (caller holds lock)."""
        while self._bytes > self.budget_bytes and len(self._items) > 1:
            self._remove(next(iter(self._items)))

    def _sweep_locked(self) -> None:
        """Drop images whose idle timer has expired (caller holds lock)."""
        if self.idle_seconds <= 0:
            return
        now = time.monotonic()
        stale = [key for key, ts in self._last_used.items() if now - ts > self.idle_seconds]
        for key in stale:
            self._remove(key)

    def _sweep_loop(self, interval: float) -> None:
        """Background thread: periodically evict idle images."""
        while not self._stop.wait(interval):
            with self._lock:
                self._sweep_locked()


def _memory_bytes(value: Any) -> int:
    """Byte footprint of a value via its ``memory_bytes()`` (e.g. a mipmap)."""
    size = value.memory_bytes()
    if not isinstance(size, int):
        raise TypeError(f"{type(value).__name__} has no usable memory size")
    return size
