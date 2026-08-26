"""On-demand QR code rendering for the Share panel.

The viewer's Share button opens a centred panel showing a QR code of the
current URL, generated here at ``GET /api/qr?url=...`` so the client never
needs a QR library. Codes are rendered with H-level error correction and the
brand "K" mark pasted over the centre (small enough not to break scanning),
and a small in-process cache keeps repeat renders cheap.
"""
from __future__ import annotations

import asyncio
import io
import re
from pathlib import Path

import numpy as np
import segno
from fastapi import APIRouter, Query, Request
from PIL import Image, ImageDraw
from starlette.responses import Response

from .errors import BadRequest

router = APIRouter(tags=["qr"])

_LOGO_PATH = Path(__file__).resolve().parent / "static" / "brand-k.png"
_LOGO_FRACTION = 0.22  # logo edge as a fraction of the QR image edge
_MODULE_SCALE = 8  # rendered pixels per QR module
_QUIET_ZONE = 4  # modules of white border around the symbol
_MAX_URL_LEN = 2048
_CACHE_LIMIT = 64

_cache: dict[str, bytes] = {}
_url_re = re.compile(r"^https?://", re.IGNORECASE)


def _render(url: str) -> bytes:
    """Render a PNG QR code for ``url`` with the favicon logo centred over it.

    Args:
        url: The absolute URL to encode.

    Returns:
        PNG bytes: H-level error correction, a 4-module quiet zone, and the
        brand "K" mark on a white rounded plate pasted over the centre at
        ``_LOGO_FRACTION`` of the width.
    """
    qr = segno.make(url, error="h")
    modules = np.asarray(qr.matrix, dtype=np.uint8)
    h, w = modules.shape
    side = w + 2 * _QUIET_ZONE
    canvas = np.full((side, side), 255, dtype=np.uint8)
    canvas[_QUIET_ZONE : _QUIET_ZONE + h, _QUIET_ZONE : _QUIET_ZONE + w] = (
        1 - modules
    ) * 255
    img = Image.fromarray(canvas, "L").convert("RGB")
    img = img.resize((side * _MODULE_SCALE, side * _MODULE_SCALE), Image.NEAREST)

    # White rounded plate behind the logo, exactly 11x11 modules so its edges
    # land on module boundaries (odd count, so it centres perfectly on any QR
    # version - segno grids are always odd).
    plate = 11 * _MODULE_SCALE
    px0 = (img.width - plate) // 2
    ImageDraw.Draw(img).rectangle(
        (px0, px0, px0 + plate, px0 + plate),
        fill=(255, 255, 255),
    )

    logo = Image.open(_LOGO_PATH).convert("RGBA")
    lw = int(side * _MODULE_SCALE * _LOGO_FRACTION)
    lh = max(1, round(lw * logo.height / logo.width))
    logo = logo.resize((lw, lh), Image.LANCZOS)
    img.paste(logo, ((img.width - lw) // 2, (img.height - lh) // 2), logo)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@router.get("/api/qr")
async def qr_endpoint(
    request: Request, url: str = Query(min_length=1, max_length=_MAX_URL_LEN)
) -> Response:
    """Return a PNG QR code for ``url``, with the site logo centred on it.

    Args:
        request: FastAPI request (kept for the router pattern).
        url: The absolute http(s) URL to encode.

    Returns:
        A PNG image with ``no-cache`` headers; raises :class:`BadRequest` for
        non-http(s) URLs or content too large for a QR code.
    """
    if not _url_re.match(url):
        raise BadRequest("url must be an absolute http(s) URL")
    data = _cache.get(url)
    if data is None:
        try:
            data = await asyncio.to_thread(_render, url)
        except ValueError as e:
            raise BadRequest("url too long to encode as a QR code") from e
        if len(_cache) >= _CACHE_LIMIT:
            _cache.clear()
        _cache[url] = data
    return Response(
        content=data,
        media_type="image/png",
        headers={"Cache-Control": "no-cache"},
    )
