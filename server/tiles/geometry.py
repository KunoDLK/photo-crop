"""Pure tile geometry math.

No imaging or I/O here: everything needed to map a tile request ``(level, tx, ty)``
onto a rectangle in the level-image grid, to compute pyramid levels, and to
describe the level-image dimensions. The manager crops tiles from the level image
produced by :class:`~tiles.mipmap.PageMipmap`, so coordinates here are in
level-image space (source pixels divided by ``2 ** level``).
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Rect:
    """Axis-aligned integer rectangle in level-image space."""

    x: int
    y: int
    w: int
    h: int


def max_level(width: int, height: int, tile_size: int) -> int:
    """Return the coarsest pyramid level (whole image fits one tile).

    Level 0 is 1:1 pixels; level ``max_level`` covers the entire image with a
    single tile.

    Args:
        width: Image width in pixels.
        height: Image height in pixels.
        tile_size: Tile edge length in pixels.

    Returns:
        ``max(0, ceil(log2(max(width, height) / tile_size)))``.
    """
    if width <= 0 or height <= 0:
        return 0
    return max(0, math.ceil(math.log2(max(width, height) / tile_size)))


def downscale(level: int) -> int:
    """Return the scale factor ``2 ** level`` for a pyramid level.

    Args:
        level: Non-negative pyramid level.

    Returns:
        Integer divisor mapping source pixels onto the level's grid.
    """
    return 2 ** level


def level_size(width: int, height: int, level: int) -> tuple[int, int]:
    """Return the ``(w, h)`` of the level image (source divided by ``2**level``).

    Args:
        width: Image width in pixels.
        height: Image height in pixels.
        level: Pyramid level.

    Returns:
        ``(ceil(width / 2**level), ceil(height / 2**level))``.
    """
    p = downscale(level)
    return (math.ceil(width / p), math.ceil(height / p))


def grid_extent(width: int, height: int, tile_size: int, level: int) -> tuple[int, int]:
    """Return the ``(columns, rows)`` of tiles at a level.

    Args:
        width: Image width in pixels.
        height: Image height in pixels.
        tile_size: Tile edge length in pixels.
        level: Pyramid level.

    Returns:
        ``(ceil(lw / tile_size), ceil(lh / tile_size))`` in level-image space.
    """
    lw, lh = level_size(width, height, level)
    return (math.ceil(lw / tile_size), math.ceil(lh / tile_size))


def crop_rect(width: int, height: int, tile_size: int, level: int, tx: int, ty: int) -> Rect:
    """Compute the level-image rectangle covered by tile ``(level, tx, ty)``.

    Args:
        width: Image width in pixels.
        height: Image height in pixels.
        tile_size: Tile edge length in pixels.
        level: Pyramid level.
        tx: Tile column index.
        ty: Tile row index.

    Returns:
        The clamped (possibly partial) rectangle in level-image space, or a
        zero-area rect if the tile is entirely out of bounds.
    """
    lw, lh = level_size(width, height, level)
    x = tx * tile_size
    y = ty * tile_size
    if x >= lw or y >= lh:
        return Rect(0, 0, 0, 0)
    return Rect(x, y, min(tile_size, lw - x), min(tile_size, lh - y))
