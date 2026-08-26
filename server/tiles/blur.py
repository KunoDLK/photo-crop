"""Blur rendering for restricted pages.

A blurred tile has the same geometry as the real tile (the crop rect, so the
client's pan/zoom tiling still lines up) with the detail destroyed: the crop is
downscaled to a tiny patch, heavily Gaussian-blurred, and upscaled back to tile
size. Only colour masses survive — no text or structure is recoverable. No
darkening happens here: the client overlays its own dark banner so its
"Unavailable in your region" text stays readable on any image.

The blur strength is halved for every pyramid level up from level 0, keeping
the source-space blur identical across levels: a level-1 tile covers the same
region as four level-0 tiles (each patch pixel is twice the source area), so
adjacent tiles rendered at different levels blur consistently.
"""
from __future__ import annotations

import cv2
import numpy as np

_BLUR_PATCH = 64  # px: the crop is reduced to this before blurring


def apply_blur(tile: np.ndarray, strength: float) -> np.ndarray:
    """Return ``tile`` with all detail destroyed.

    Args:
        tile: A BGR uint8 tile (``tile_size`` × ``tile_size`` × 3).
        strength: Gaussian sigma applied at the small patch size (already
            level-scaled by the caller).

    Returns:
        A blurred BGR uint8 tile of the same shape.
    """
    small = cv2.resize(tile, (_BLUR_PATCH, _BLUR_PATCH), interpolation=cv2.INTER_AREA)
    if strength > 0:
        small = cv2.GaussianBlur(small, (0, 0), sigmaX=strength)
    return cv2.resize(
        small, (tile.shape[1], tile.shape[0]), interpolation=cv2.INTER_CUBIC
    )
