"""EXIF orientation handling shared by dimension reads and page decoding.

JPEG files may carry an EXIF orientation tag (1-8) saying how the stored
pixels must be rotated before display. The listing dimensions and the decoded
pixel array must agree on that rotation, otherwise a portrait photo stored
landscape (orientation 6/8) gets laid out and tiled as landscape.
"""
from __future__ import annotations

import io
from typing import Callable

import numpy as np
from PIL import Image

#: Orientation tag → transform turning stored pixels into display pixels.
#: Mirrors PIL's ``ImageOps.exif_transpose`` mapping. ``np.rot90`` rotates
#: counter-clockwise, so orientation 6 (90° clockwise) is ``rot90(a, 3)``.
_TRANSFORMS: dict[int, Callable[[np.ndarray], np.ndarray]] = {
    2: lambda a: a[:, ::-1],  # flip left-right
    3: lambda a: np.rot90(a, 2),  # 180°
    4: lambda a: a[::-1, :],  # flip top-bottom
    5: lambda a: np.swapaxes(a, 0, 1),  # transpose
    6: lambda a: np.rot90(a, 3),  # 90° clockwise
    7: lambda a: np.swapaxes(a, 0, 1)[::-1, ::-1],  # transverse
    8: lambda a: np.rot90(a, 1),  # 90° counter-clockwise
}


def _orientation_of_image(im: Image.Image) -> int:
    """Read the EXIF orientation tag from an open image, defaulting to 1."""
    try:
        return im.getexif().get(274, 1) or 1
    except Exception:  # noqa: BLE001 — broken headers fall back to "normal"
        return 1


def orientation_of_image(im: Image.Image) -> int:
    """EXIF orientation tag of an already-open Pillow image (1 when absent).

    Args:
        im: An open Pillow image (header already read).

    Returns:
        The orientation tag in ``1..8``.
    """
    return _orientation_of_image(im)


def orientation_of_bytes(data: bytes) -> int:
    """EXIF orientation tag of encoded image bytes (header-only read).

    Args:
        data: Encoded image bytes (JPEG/PNG/...).

    Returns:
        The orientation tag in ``1..8``.
    """
    try:
        with Image.open(io.BytesIO(data)) as im:
            return _orientation_of_image(im)
    except Exception:  # noqa: BLE001 — broken headers fall back to "normal"
        return 1


def transpose(img: np.ndarray, orientation: int) -> np.ndarray:
    """Rotate/flip a decoded image so it displays per its EXIF orientation.

    Args:
        img: The stored pixel array, e.g. from ``cv2.imdecode``.
        orientation: EXIF orientation tag (``1..8``).

    Returns:
        The display-oriented array (unchanged when orientation is 1).
    """
    transform = _TRANSFORMS.get(orientation)
    return img if transform is None else transform(img)


def oriented_dims(width: int, height: int, orientation: int) -> tuple[int, int]:
    """Display dimensions after applying EXIF orientation.

    Args:
        width: Stored pixel width.
        height: Stored pixel height.
        orientation: EXIF orientation tag (``1..8``).

    Returns:
        The ``(width, height)`` a viewer sees; axes swap for 5-8.
    """
    if orientation in (5, 6, 7, 8):
        return height, width
    return width, height
