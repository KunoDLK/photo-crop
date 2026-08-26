"""Blur rendering for restricted pages.

A blurred tile has the same geometry as the real tile (the crop rect, so the
client's pan/zoom tiling still lines up) with the detail destroyed. Instead of
blurring each tile's crop independently — which left visible seams where
adjacent tiles met — the whole page is blurred ONCE at a capped resolution
(``blur_levels_from_coarsest`` levels in from the coarsest pyramid level) and
every tile at any zoom level is a crop of that single plane, so tiles are
pixel-consistent by construction. Only colour masses survive — no text or
structure is recoverable. No darkening happens here: the client overlays its
own dark banner so its "Unavailable in your region" text stays readable on any
image.
"""
from __future__ import annotations

import cv2
import numpy as np


def build_blur_plane(plane: np.ndarray, sigma: float) -> np.ndarray:
    """Return ``plane`` (BGR uint8) with all detail destroyed.

    The Gaussian is applied over the whole image, so a feature spanning
    several tiles blurs identically everywhere; the caller crops per-tile
    regions out of the result.

    Args:
        plane: The whole page at the capped blur level.
        sigma: Gaussian sigma in plane pixels (level-scaled by the caller so
            the source-space blur matches the configured strength).

    Returns:
        A blurred copy of the same shape (the input is never mutated).
    """
    if sigma <= 0:
        return plane.copy()
    return cv2.GaussianBlur(plane, (0, 0), sigmaX=sigma)


def crop_and_upscale(
    plane: np.ndarray, x: float, y: float, w: float, h: float,
    out_w: int, out_h: int,
) -> np.ndarray:
    """Crop a (possibly fractional) rect from the blurred plane and upscale.

    Args:
        plane: The blurred whole-page plane (BGR uint8).
        x: Crop left in plane pixels (float; sub-pixel allowed).
        y: Crop top in plane pixels.
        w: Crop width in plane pixels.
        h: Crop height in plane pixels.
        out_w: Output width (tile content width).
        out_h: Output height (tile content height).

    Returns:
        The crop resized to ``(out_w, out_h)`` with cubic interpolation, or
        the plain crop when the sizes already match. Crops are clamped to the
        plane's bounds (a coarse tile covering the whole page simply takes the
        whole plane).
    """
    hgt, wid = plane.shape[:2]
    x0 = max(0, min(wid - 1, int(np.floor(x))))
    y0 = max(0, min(hgt - 1, int(np.floor(y))))
    x1 = max(x0 + 1, min(wid, int(np.ceil(x + w))))
    y1 = max(y0 + 1, min(hgt, int(np.ceil(y + h))))
    region = plane[y0:y1, x0:x1]
    if (x1 - x0) == out_w and (y1 - y0) == out_h:
        return region
    return cv2.resize(region, (out_w, out_h), interpolation=cv2.INTER_CUBIC)
