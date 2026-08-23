"""Progressive JPEG encoding.

Encodes a tile (BGR ndarray) into JPEG bytes with progressive (SOF2) encoding so
clients see a coarse preview immediately and refine as the image streams in.
CPU-bound regardless of resampling backend (libjpeg-turbo via OpenCV).
"""
from __future__ import annotations

import cv2
import numpy as np

from ..errors import AppError


def encode_progressive_jpeg(bgr: np.ndarray, quality: int, progressive: bool = True) -> bytes:
    """Encode a BGR image as a (progressive) JPEG.

    Args:
        bgr: Image data (H, W, 3) uint8, BGR order.
        quality: JPEG quality 0-100.
        progressive: Emit progressive (SOF2) encoding.

    Returns:
        Encoded JPEG bytes.

    Raises:
        errors.AppError: If encoding fails.
    """
    params: list[int] = [cv2.IMWRITE_JPEG_QUALITY, int(quality)]
    if progressive:
        params += [cv2.IMWRITE_JPEG_PROGRESSIVE, 1]
    ok, buf = cv2.imencode(".jpg", bgr, params)
    if not ok:
        raise AppError("JPEG encoding failed")
    return buf.tobytes()
