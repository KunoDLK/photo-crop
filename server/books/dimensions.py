"""Header-only image dimension reads.

Listings must be fast even across thousands of pages, so dimensions are read from
image headers without decoding pixel data. Pillow's ``Image.open`` + ``size`` does
exactly this for the formats we support.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

from ..errors import BadRequest, NotFound


def image_dims(path: Path) -> tuple[int, int]:
    """Return ``(width, height)`` of an image by reading only its header.

    Args:
        path: Filesystem path to an image file.

    Returns:
        Pixel ``(width, height)``.

    Raises:
        errors.NotFound: If the file does not exist.
        errors.BadRequest: If the file is not a decodable image.
    """
    if not path.is_file():
        raise NotFound(f"image not found: {path.name}")
    try:
        with Image.open(path) as im:
            return im.size
    except Exception as exc:  # noqa: BLE001 — normalize any Pillow error
        raise BadRequest(f"cannot read image header for {path.name}: {exc}") from exc
