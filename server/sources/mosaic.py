"""A virtual page composed server-side from many separate source images.

The "mosaic" image source presents a grid of independent images (e.g. a 3x3
block of Sentinel-2 tiles) as one seamless zoomable page. No giant stitched
file and no precomputed zoom proxies ever exist: every requested tile is
rendered on demand by decoding the full-resolution source images its rectangle
overlaps, cropping each overlap, and downsampling onto the 256px output. The
shared SQLite tile cache is what makes repeat views cheap — a tile is rendered
once per zoom level and served from disk afterwards.

The manifest (JSON) describes the virtual canvas and the placement of every
cell::

    {
      "version": 1788564183968674999,
      "canvas": {"width": 32940, "height": 32940},
      "cells": [
        {"id": "30UWB", "x": 0, "y": 21960,
         "width": 10980, "height": 10980,
         "source": "../satellite/30UWB_2025-10-27_q60.jpg"},
        ...
      ]
    }

Cell ``source`` paths are resolved relative to the manifest file. Renders are
pure functions of the tile request (the manifest and source images are
immutable once written), so tiles cache immutably like every other source.
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from ..errors import BadRequest, NotFound
from ..models import AccessInfo, BookSummary, CoverInfo, ImageInfo, PageInfo
from ..tiles import geometry
from ..tiles.page_cache import PageCache
from .base import ImageSource, TileRequest


@dataclass(frozen=True)
class Cell:
    """One source image placed on the virtual canvas."""

    id: str
    x: int
    y: int
    width: int
    height: int
    source: Path


@dataclass(frozen=True)
class MosaicManifest:
    """The virtual canvas: overall size plus every cell's placement."""

    version: int
    canvas_w: int
    canvas_h: int
    cells: tuple[Cell, ...]

    @property
    def max_level(self) -> int:
        """Coarsest pyramid level (the whole canvas on one tile)."""
        return geometry.max_level(self.canvas_w, self.canvas_h, 256)

    @staticmethod
    def load(path: Path) -> "MosaicManifest":
        """Parse and validate a manifest file.

        Args:
            path: Path to ``manifest.json``.

        Returns:
            The validated manifest.

        Raises:
            errors.BadRequest: If the manifest is malformed or cells do not
                fit the canvas.
        """
        try:
            raw = json.loads(path.read_text())
            version = int(raw["version"])
            cw, ch = raw["canvas"]["width"], raw["canvas"]["height"]
            cells = tuple(
                Cell(
                    id=str(c["id"]),
                    x=int(c["x"]),
                    y=int(c["y"]),
                    width=int(c["width"]),
                    height=int(c["height"]),
                    source=(path.parent / c["source"]).resolve(),
                )
                for c in raw["cells"]
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BadRequest(f"invalid mosaic manifest: {exc}") from exc
        manifest = MosaicManifest(version, cw, ch, cells)
        if manifest.canvas_w <= 0 or manifest.canvas_h <= 0 or not manifest.cells:
            raise BadRequest("mosaic manifest needs a canvas and at least one cell")
        for cell in manifest.cells:
            if (
                cell.x < 0 or cell.y < 0
                or cell.x + cell.width > cw or cell.y + cell.height > ch
            ):
                raise BadRequest(f"mosaic cell {cell.id} lies outside the canvas")
            if not cell.source.is_file():
                raise BadRequest(f"mosaic cell {cell.id} source missing: {cell.source}")
        return manifest

    def overlapping(self, x0: int, y0: int, x1: int, y1: int) -> list[Cell]:
        """Cells whose canvas box intersects the given integer canvas rect."""
        return [
            cell for cell in self.cells
            if cell.x < x1 and cell.x + cell.width > x0
            and cell.y < y1 and cell.y + cell.height > y0
        ]


class MosaicSource(ImageSource):
    """A pluggable image source serving one virtual page from many images.

    Attributes:
        manifest: The validated canvas/cell manifest.
        tile_size: Output tile edge length (must match the client's ``TILE``).
    """

    key = "mosaic"
    cacheable = True
    cache_control = "public, max-age=31536000, immutable"

    def __init__(
        self,
        manifest: MosaicManifest,
        tile_size: int = 256,
        page_cache: PageCache | None = None,
    ) -> None:
        self.manifest = manifest
        self.tile_size = tile_size
        self._page_cache = page_cache
        self._page_id = "mosaic"
        self._decode_locks: dict[str, threading.Lock] = {}
        self._decode_locks_guard = threading.Lock()

    def owns(self, book_id: str) -> bool:
        return book_id == "satellite-mosaic"

    def _access(self) -> AccessInfo:
        """Provider pages are unrestricted demo content: full, never region-locked."""
        return AccessInfo(status="full", zone="", region_locked=False)

    @property
    def signature(self) -> str:
        """Change signature: the manifest version plus its content hash."""
        digest = hashlib.sha256()
        digest.update(str(self.manifest.version).encode())
        for cell in self.manifest.cells:
            digest.update(f"{cell.id}{cell.x}{cell.y}".encode())
        return digest.hexdigest()

    def list_books(self) -> list[BookSummary]:
        access = self._access()
        m = self.manifest
        return [
            BookSummary(
                id="satellite-mosaic",
                name="Satellite mosaic",
                cover=CoverInfo(
                    page_id=self._page_id,
                    width=m.canvas_w,
                    height=m.canvas_h,
                    max_level=m.max_level,
                    mtime=m.version,
                    access=access,
                    source=self.key,
                ),
                visibility="public",
            )
        ]

    def pages(self, book_id: str) -> tuple[str, list[PageInfo]] | None:
        if not self.owns(book_id):
            return None
        m = self.manifest
        return self.signature, [
            PageInfo(
                page_id=self._page_id,
                name="Mosaic",
                group=1,
                order="1",
                width=m.canvas_w,
                height=m.canvas_h,
                max_level=m.max_level,
                mtime=m.version,
                access=self._access(),
                source=self.key,
            )
        ]

    def image_info(self, book_id: str, page_id: str) -> ImageInfo | None:
        if not self.owns(book_id) or page_id != self._page_id:
            return None
        m = self.manifest
        return ImageInfo(
            page_id=page_id,
            width=m.canvas_w,
            height=m.canvas_h,
            max_level=m.max_level,
            file_size=0,
            hash=f"provider:{self.key}:{self.signature}",
            access=self._access(),
            source=self.key,
        )

    def tile_zoom(self, level: int) -> int:
        """Cache eviction depth: canvas levels are archive-style (0 = 1:1)."""
        return self.manifest.max_level - level

    def render_tile(self, req: TileRequest) -> np.ndarray:
        """Compose one tile from every source image it overlaps.

        Args:
            req: Tile coordinate and size (from the shared tile service).

        Returns:
            A ``(tile_size, tile_size, 3)`` uint8 BGR image.

        Raises:
            errors.NotFound: For unknown book/page ids or missing sources.
            errors.BadRequest: For out-of-range levels or coordinates.
        """
        if not self.owns(req.book) or req.page != self._page_id:
            raise NotFound(f"provider page not found: {req.book}/{req.page}")
        m = self.manifest
        if not (0 <= req.level <= m.max_level):
            raise BadRequest(f"level {req.level} out of range (0..{m.max_level})")
        cols, rows = geometry.grid_extent(
            m.canvas_w, m.canvas_h, req.tile_size, req.level
        )
        if not (0 <= req.tx < cols and 0 <= req.ty < rows):
            raise BadRequest(f"tile ({req.tx},{req.ty}) out of range for level {req.level}")

        canvas = np.zeros((req.tile_size, req.tile_size, 3), dtype=np.uint8)
        scale = 1 << req.level  # canvas pixels per level pixel
        # The tile's rectangle in the level image, clamped to the canvas.
        rect = geometry.crop_rect(
            m.canvas_w, m.canvas_h, req.tile_size, req.level, req.tx, req.ty
        )
        # Same rectangle in 1:1 canvas pixels (level pixels times the scale).
        c_x0, c_y0 = rect.x * scale, rect.y * scale
        c_x1, c_y1 = (rect.x + rect.w) * scale, (rect.y + rect.h) * scale

        for cell in m.overlapping(c_x0, c_y0, c_x1, c_y1):
            self._draw_cell(canvas, req, cell, rect, scale)
        return canvas

    def _draw_cell(
        self,
        canvas: np.ndarray,
        req: TileRequest,
        cell: Cell,
        rect: geometry.Rect,
        scale: int,
    ) -> None:
        """Decode one cell and resample its overlap onto the output tile.

        Level pixels are exactly ``2**level`` canvas pixels wide, so every
        level pixel a cell touches maps back to one output column; consecutive
        cells abut with no gaps and no overlap. Decoded cell bitmaps live in
        the shared decoded-image LRU (same cache and budget as archive page
        mipmaps), so panning and zooming reuse resident sources.

        Args:
            canvas: The output tile being painted (BGR uint8).
            req: The tile request.
            cell: The source image to draw.
            rect: The tile's rectangle in level-image space.
            scale: ``2 ** req.level``.
        """
        scale_m1 = scale - 1
        # Output columns/rows this cell covers (clamped to the tile).
        j0 = max(rect.x, cell.x >> req.level)
        j1 = min(rect.x + rect.w, (cell.x + cell.width + scale_m1) >> req.level)
        k0 = max(rect.y, cell.y >> req.level)
        k1 = min(rect.y + rect.h, (cell.y + cell.height + scale_m1) >> req.level)
        if j0 >= j1 or k0 >= k1:
            return

        # The cell's full-resolution pixels behind those output pixels.
        fx0 = min(cell.width, max(0, j0 * scale - cell.x))
        fx1 = min(cell.width, max(0, j1 * scale - cell.x))
        fy0 = min(cell.height, max(0, k0 * scale - cell.y))
        fy1 = min(cell.height, max(0, k1 * scale - cell.y))
        if fx0 >= fx1 or fy0 >= fy1:
            return

        image = self._cell_image(cell)
        region = image[fy0:fy1, fx0:fx1]
        out_w, out_h = j1 - j0, k1 - k0
        if region.shape[1] != out_w or region.shape[0] != out_h:
            region = cv2.resize(region, (out_w, out_h), interpolation=cv2.INTER_AREA)
        canvas[k0 - rect.y : k1 - rect.y, j0 - rect.x : j1 - rect.x] = region

    def _cell_key(self, cell: Cell) -> str:
        """Shared-cache key for one cell's decoded bitmap (version-scoped)."""
        return f"mosaic:{self.manifest.version}:{cell.id}"

    def _cell_image(self, cell: Cell) -> np.ndarray:
        """The cell's decoded full-resolution BGR image, from the shared LRU.

        When no shared cache is attached (standalone use), the image is
        decoded on every call. Otherwise the decoded bitmap is cached in the
        shared decoded-image LRU under a per-cell lock, so concurrent tiles of
        the same cell decode it once.

        Args:
            cell: The source image.

        Returns:
            The BGR uint8 image.

        Raises:
            errors.NotFound: If the source file is missing or undecodable.
        """
        cache = self._page_cache
        key = self._cell_key(cell)
        if cache is not None:
            image = cache.get(key)
            if image is not None:
                return image
        with self._decode_locks_guard:
            lock = self._decode_locks.setdefault(cell.id, threading.Lock())
        with lock:
            if cache is not None:
                image = cache.get(key)
                if image is not None:
                    return image
            image = cv2.imread(str(cell.source), cv2.IMREAD_COLOR)
            if image is None:
                raise NotFound(f"mosaic cell source undecodable: {cell.source}")
            if cache is not None:
                cache.put(key, image, size_bytes=image.nbytes)
            return image
