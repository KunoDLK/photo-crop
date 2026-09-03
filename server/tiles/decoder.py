"""Source image decoding.

Decodes a page file into an in-memory BGR ndarray using OpenCV's ``imdecode``
(libjpeg-turbo under the hood for JPEG). cv2 applies EXIF orientation by
default, so ``decode_bytes`` explicitly decodes **raw** pixels and ``decode``
applies the orientation tag itself — exactly once, deterministically, matching
the listing dimensions.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from ..errors import BadRequest, NotFound
from .orientation import orientation_of_bytes, transpose


def decode(path: Path) -> np.ndarray:
    """Decode an image file into a BGR uint8 ndarray.

    Args:
        path: Filesystem path to the image.

    Returns:
        The decoded image as a ``(H, W, 3)`` uint8 ndarray (BGR order).

    Raises:
        errors.NotFound: If the file does not exist.
        errors.BadRequest: If the file cannot be decoded.
    """
    if not path.is_file():
        raise NotFound(f"page not found: {path.name}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise NotFound(f"page not readable: {path.name}") from exc
    img = decode_bytes(data)
    # Rotate/flip per EXIF orientation so pixels match the listing dims.
    return transpose(img, orientation_of_bytes(data))


def decode_bytes(data: bytes) -> np.ndarray:
    """Decode raw image bytes (e.g. a cached tile) into a BGR ndarray.

    Decodes with EXIF orientation ignored so the returned array holds the
    stored pixels as-is; orientation is applied by :func:`decode` for source
    pages, and our own encoded tiles carry no orientation tag.

    Args:
        data: Encoded image bytes (JPEG/PNG/...).

    Returns:
        The decoded image as a ``(H, W, 3)`` uint8 ndarray (BGR order).

    Raises:
        errors.BadRequest: If the bytes cannot be decoded.
    """
    buf = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
    if img is None:
        raise BadRequest("cannot decode image bytes")
    return img
