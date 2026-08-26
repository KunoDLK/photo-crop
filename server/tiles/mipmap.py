"""Per-page mipmap pyramid.

Holds a decoded page and lazily builds downscaled levels (each ``2x`` smaller)
using :func:`resampler.resize_area`. Levels are built by repeated halving so no
source pixel is ever skipped, and cached for the lifetime of the object.
"""
from __future__ import annotations

import threading

import numpy as np

from . import resampler


class PageMipmap:
    """Cached, lazily built downscaled pyramid for a single page.

    Attributes:
        page_id: Opaque page identifier (used for logging/keys).
        source: The decoded level-0 image (BGR uint8).
        max_level: Coarsest level (whole image on one tile).
        tile_size: Tile edge length in pixels.
    """

    def __init__(self, page_id: str, source: np.ndarray, max_level: int, tile_size: int) -> None:
        self.page_id = page_id
        self.source = source
        self.max_level = max_level
        self.tile_size = tile_size
        self._levels: dict[int, np.ndarray] = {0: source}
        # Reentrant so the blur plane can be built under the same lock that
        # builds levels (level() re-locks while the plane is being computed).
        self._lock = threading.RLock()
        # Whole-page blur plane (built lazily by the tile manager); lives on
        # the mipmap so it is evicted with the page and shares its lock.
        self.blur_plane: np.ndarray | None = None
        self.blur_plane_level: int = 0

    @property
    def width(self) -> int:
        """Source width in pixels."""
        return self.source.shape[1]

    @property
    def height(self) -> int:
        """Source height in pixels."""
        return self.source.shape[0]

    def level(self, L: int) -> np.ndarray:
        """Return the whole-image ndarray at level ``L``, building it if needed.

        Args:
            L: Target level in ``[0, max_level]``.

        Returns:
            The downscaled image for that level, cached for future calls.
        """
        L = max(0, min(int(L), self.max_level))
        with self._lock:
            if L in self._levels:
                return self._levels[L]
            for lv in range(1, L + 1):
                if lv in self._levels:
                    continue
                self._levels[lv] = self._build_from(self._levels[lv - 1])
            return self._levels[L]

    def _build_from(self, prev: np.ndarray) -> np.ndarray:
        """Downscale ``prev`` by roughly 2x with area resampling.

        Args:
            prev: The image one level finer (larger).

        Returns:
            The halved image.
        """
        nh = (prev.shape[0] + 1) // 2
        nw = (prev.shape[1] + 1) // 2
        return resampler.resize_area(prev, nw, nh)

    def memory_bytes(self) -> int:
        """Approximate RAM held by all cached levels (for the page cache budget)."""
        total = 0
        with self._lock:
            for img in self._levels.values():
                total += img.nbytes
            if self.blur_plane is not None:
                total += self.blur_plane.nbytes
        return total
