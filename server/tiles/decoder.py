"""Source image decoding.

Decodes a page file into an in-memory BGR ndarray using OpenCV's ``imdecode``
(libjpeg-turbo under the hood for JPEG). Kept in its own module so the decode
strategy (format support, orientation) can evolve without touching the pipeline.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from ..errors import BadRequest, NotFound


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
    return decode_bytes(data)


def decode_bytes(data: bytes) -> np.ndarray:
    """Decode raw image bytes (e.g. a cached tile) into a BGR ndarray.

    Args:
        data: Encoded image bytes (JPEG/PNG/...).

    Returns:
        The decoded image as a ``(H, W, 3)`` uint8 ndarray (BGR order).

    Raises:
        errors.BadRequest: If the bytes cannot be decoded.
    """
    buf = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        raise BadRequest("cannot decode image bytes")
    return img
