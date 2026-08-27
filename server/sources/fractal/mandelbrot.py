"""Pure Mandelbrot rendering.

No I/O, no settings, no FastAPI: given fractal parameters and a tile
coordinate, compute that tile's escape-time coloring as a BGR ndarray. Tiles
are pure functions of their complex rect, so any level, any ``(tx, ty)`` can
be rendered independently and cached forever (the "bottomless pyramid").
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class FractalParams:
    """The region and iteration budget of one Mandelbrot image.

    The iteration budget grows with depth so deep tiles keep their detail,
    but the cap bounds the worst case: a fully interior (converged) tile runs
    every pixel to the cap, so ``max_iter_cap`` times 65536 bounds one tile's
    render cost.

    ``contrast_window`` and ``contrast_power`` shape the grayscale mapping:
    the final ``contrast_window`` iterations before ``max_iter`` — where the
    fine boundary detail lives at every zoom depth — are stretched across the
    full black-to-white range (raised to ``contrast_power``), so deep zoom
    keeps strong contrast. Fast-escape background pixels fall below the
    window and render black.
    """

    re_min: float = -2.5
    re_max: float = 1.0
    im_min: float = -1.25
    im_max: float = 1.25
    max_iter_base: int = 300
    max_iter_per_level: int = 20
    max_iter_cap: int = 1500
    contrast_window: int = 250
    contrast_power: float = 0.6


def _build_palette(size: int = 256) -> np.ndarray:
    """A linear grayscale ramp (one row per normalized value).

    Index 0 is black; index ``size - 1`` is pure white. Contrast comes from
    the windowed escape-time mapping in :func:`render_tile`, not from this
    curve.
    """
    t = np.arange(size, dtype=np.float64) / max(size - 1, 1)
    gray = (t * 255.0).astype(np.uint8)
    return np.stack([gray, gray, gray], axis=-1)  # BGR == RGB for gray


PALETTE = _build_palette()


def render_tile(
    params: FractalParams,
    level: int,
    tx: int,
    ty: int,
    tile_size: int,
) -> np.ndarray:
    """Render one Mandelbrot tile as a BGR uint8 ndarray.

    Level 0 is the whole region on one tile; level ``-k`` divides the region
    into a ``2^k x 2^k`` grid, so tile ``(tx, ty)`` covers the sub-rectangle
    starting at the corresponding fraction of the region. The iteration
    budget grows with depth so deep tiles keep their detail.

    Args:
        params: Region and iteration-budget parameters.
        level: Non-positive pyramid level (0 = whole image).
        tx: Tile column index.
        ty: Tile row index.
        tile_size: Square tile edge length in pixels.

    Returns:
        A ``(tile_size, tile_size, 3)`` uint8 BGR image.
    """
    span_x = (params.re_max - params.re_min) / (2.0 ** -level)
    span_y = (params.im_max - params.im_min) / (2.0 ** -level)
    dc_x = span_x / tile_size
    dc_y = span_y / tile_size
    cx0 = params.re_min + tx * span_x
    # Tile row ty sits BELOW row ty-1 on screen, and rows run top-to-bottom
    # (descending complex y), so higher ty must hold LOWER complex y: anchor
    # the tile's range at im_max instead of im_min.
    cy0 = params.im_max - (ty + 1) * span_y
    xs = np.linspace(cx0, cx0 + (tile_size - 1) * dc_x, tile_size)
    ys = np.linspace(cy0 + (tile_size - 1) * dc_y, cy0, tile_size)
    # Pixel (row, col) samples (x=xs[col], y=ys[row]): x grows left-to-right,
    # y descends top-to-bottom, so the set keeps its standard orientation.
    c = xs[None, :] + 1j * ys[:, None]

    max_iter = min(
        params.max_iter_base + params.max_iter_per_level * -level,
        params.max_iter_cap,
    )

    z = np.zeros_like(c)
    m = np.full(c.shape, max_iter, dtype=np.float64)
    done = np.zeros(c.shape, dtype=bool)
    for i in range(max_iter):
        z[~done] = z[~done] ** 2 + c[~done]
        div = ~done & (z.real ** 2 + z.imag ** 2 > 4.0)
        if div.any():
            nu = i + 1 - np.log2(np.log2(np.abs(z[div])))
            m[div] = np.clip(nu, 0.0, max_iter)
            done |= div
        if done.all():
            break

    # Windowed contrast: stretch the final `contrast_window` iterations up to
    # max_iter (where the fine boundary detail lives at every zoom depth)
    # across the full black->white range. Tile-independent, so neighbouring
    # tiles keep identical contrast (no seams). Fast-escape background falls
    # below the window and renders black.
    lo = max(0.0, max_iter - params.contrast_window)
    t = (m - lo) / max(max_iter - lo, 1e-9)
    t = np.clip(t, 0.0, 1.0) ** params.contrast_power
    index = (t * (PALETTE.shape[0] - 1)).astype(np.uint8)
    return PALETTE[index]