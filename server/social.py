"""Social-media (Open Graph) previews and the SPA fallback page.

Share links are plain path segments at the site root (``/93050a0``) so crawlers
(iMessage, Discord, Reddit), which never run JavaScript or see URL hashes, get an
HTML page with Open Graph tags pointing at a rendered preview image. The same
viewer page is served for every path; :func:`~pages.render_fragment` supplies
the per-path body content (book list / page grid / page OCR) and the OG meta,
which this module injects into the shell. The description is fixed: it describes
what the site is.

Preview images are stitched from the finest cached tile level via
:class:`~tiles.manager.TileService`, so already-viewed pages render from the
encoded disk cache and pages never share a second decode path with the viewer.
The preview URL is content-addressed by the page mtime and cached immutably.
"""
from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import numpy as np
from fastapi import APIRouter, Request
from starlette.responses import HTMLResponse, Response

from .books import dimensions, scanner
from .errors import NotFound
from .pages import DESCRIPTION, SITE_TITLE, esc, render_fragment
from .rights.policy import FULL
from .tiles import decoder, encoder, geometry, resampler

router = APIRouter(tags=["social"])

PREVIEW_W = 1200
PREVIEW_H = 630

#: Preview cache keyed by (book, page, version): content-addressed (immutable
#: URLs) and cached apart from the tiles themselves.
_preview_cache: dict[tuple[str, str, int], bytes] = {}

_index_html: str | None = None

#: Sitemap cache: (monotonic timestamp, rendered bytes). Rebuilt when the
#: catalog TTL elapses so re-scans (new books/pages) eventually appear.
_SITEMAP_TTL = 600.0
_sitemap_cache: tuple[float, bytes] | None = None


def _viewer_html() -> str:
    """Return the raw viewer page HTML (cached after the first read)."""
    global _index_html
    if _index_html is None:
        path = Path(__file__).resolve().parent / "static" / "index.html"
        _index_html = path.read_text(encoding="utf-8")
    return _index_html


def _inject_og(html: str, meta: dict) -> str:
    """Insert Open Graph/meta tags into the viewer page's ``<head>``."""
    title = meta.get("title") or SITE_TITLE
    image = meta.get("image")
    canonical = meta.get("canonical")
    tags = [
        f'<meta name="description" content="{esc(DESCRIPTION)}">',
        f'<meta property="og:site_name" content="{esc(SITE_TITLE)}">',
        f'<meta property="og:title" content="{esc(title)}">',
        f'<meta property="og:description" content="{esc(DESCRIPTION)}">',
        '<meta property="og:type" content="website">',
    ]
    if canonical:
        tags.append(f'<link rel="canonical" href="{esc(canonical)}">')
    if image:
        tags.append(f'<meta property="og:image" content="{esc(image)}">')
        tags.append('<meta name="twitter:card" content="summary_large_image">')
        tags.append(f'<meta name="twitter:title" content="{esc(title)}">')
        tags.append(f'<meta name="twitter:image" content="{esc(image)}">')
    block = "\n".join("    " + t for t in tags)
    return html.replace("</head>", block + "\n</head>", 1)


def _inject_title(html: str, meta: dict) -> str:
    """Replace the static page title with the per-path one (mirrors the client)."""
    title = esc(meta.get("title") or SITE_TITLE)
    return html.replace("<title>Hyper.K Archive</title>", f"<title>{title}</title>", 1)


def _inject_content(html: str, content: str) -> str:
    """Insert the crawler-facing body fragment into the shell's ``#seo-content``."""
    if not content:
        return html
    return html.replace(
        '<div id="seo-content"></div>',
        f'<div id="seo-content">{content}</div>',
        1,
    )


def spa_response(request: Request) -> HTMLResponse:
    """Serve the viewer page, injecting content and OG tags for the request path.

    The root renders a link to every book; a bare root segment (``/93050a0``) is
    a location id and renders that book's page grid or a page's OCR text with
    prev/next links. Every other path gets the plain shell (the client re-reads
    it at startup).
    """
    try:
        content, meta = render_fragment(request)
    except Exception:  # noqa: BLE001 — a gone location yields the plain shell
        content, meta = "", {"title": SITE_TITLE, "image": None}
    html = _inject_og(_viewer_html(), meta)
    html = _inject_title(html, meta)
    html = _inject_content(html, content)
    headers = {"Cache-Control": "no-cache"}
    if meta.get("robots"):
        # Private locations must not be indexed at all.
        headers["X-Robots-Tag"] = meta["robots"]
    return HTMLResponse(html, headers=headers)


def _stitch_preview(
    tiles: list[bytes], cols: int, rows: int, tile_size: int, lw: int, lh: int,
    nw: int, nh: int, quality: int,
) -> bytes:
    """Assemble encoded tiles into the 1200×630 preview JPEG.

    Args:
        tiles: Encoded JPEG tiles in row-major order covering the level image.
        cols: Tile columns.
        rows: Tile rows.
        tile_size: Tile edge length (edge tiles are padded to this size).
        lw: True level-image width (unpadded).
        lh: True level-image height (unpadded).
        nw: Target content width after downscaling.
        nh: Target content height after downscaling.
        quality: JPEG quality for the final encode.

    Returns:
        A progressive JPEG, 1200×630, content centered on white.
    """
    canvas = np.empty((rows * tile_size, cols * tile_size, 3), dtype=np.uint8)
    for i, data in enumerate(tiles):
        ty, tx = divmod(i, cols)
        tile = decoder.decode_bytes(data)
        canvas[
            ty * tile_size : (ty + 1) * tile_size, tx * tile_size : (tx + 1) * tile_size
        ] = tile
    img = canvas[:lh, :lw]
    if (nw, nh) != (lw, lh):
        img = resampler.resize_area(img, nw, nh)
    out = np.full((PREVIEW_H, PREVIEW_W, 3), 255, dtype=np.uint8)
    x = (PREVIEW_W - nw) // 2
    y = (PREVIEW_H - nh) // 2
    out[y : y + nh, x : x + nw] = img
    return encoder.encode_progressive_jpeg(out, quality)


async def _render_preview(
    service, settings, book_id: str, page_id: str, version: int, path: Path,
) -> bytes:
    """Render a preview from the finest tile level that still covers the target.

    Picks the smallest pyramid level whose image is at least the target content
    size, so the stitch uses few tiles while staying above preview resolution;
    every tile goes through :meth:`tiles.manager.TileService.get_tile`, reusing
    the encoded disk cache, the decoded-page cache, and the mipmaps.
    """
    width, height = dimensions.image_dims(path)
    scale = min(PREVIEW_W / width, PREVIEW_H / height, 1.0)
    nw = max(1, round(width * scale))
    nh = max(1, round(height * scale))
    tile_size = settings.tile_size
    top = geometry.max_level(width, height, tile_size)
    ratio = min(width / nw, height / nh)
    level = max(0, min(top, math.floor(math.log2(ratio))))
    cols, rows = geometry.grid_extent(width, height, tile_size, level)
    lw, lh = geometry.level_size(width, height, level)

    getter = service.get_tile
    jobs = [
        getter(book_id, page_id, version, level, tx, ty)
        for ty in range(rows)
        for tx in range(cols)
    ]
    results = await asyncio.gather(*jobs)
    tiles = [data for data, _ in results]
    return await asyncio.to_thread(
        _stitch_preview, tiles, cols, rows, tile_size, lw, lh, nw, nh,
        settings.jpeg_quality,
    )


def _preview_access(request: Request, book_id: str, page_id: str) -> dict:
    """Resolve the page's access for the requesting crawler/session."""
    viewer = request.app.state.auth.viewer_from_request(request)
    zone = request.app.state.region.zone_of_request(request)
    return request.app.state.policy.resolve(viewer, book_id, page_id, zone)


@router.get("/og/{book_id}/{page_id}/{version}.jpg")
async def preview_image_endpoint(
    book_id: str, page_id: str, version: int, request: Request
) -> Response:
    """Return the real social preview JPEG for a page (full access only).

    Args:
        book_id: Book directory name.
        page_id: Page filename.
        version: Page file mtime (content version) — makes the URL immutable.
        request: FastAPI request (to reach ``app.state`` services).

    Returns:
        A 1200×630 progressive JPEG with long-lived cache headers.

    Raises:
        errors.NotFound: Unless the requester's access is ``full`` — the real
            image never leaks to blurred/nonexistent requests (those pages get
            no preview image at all).
    """
    settings = request.app.state.settings
    path = scanner.page_path(settings.archive_root, book_id, page_id)
    access = _preview_access(request, book_id, page_id)
    if access["status"] != FULL:
        raise NotFound(f"page not found: {page_id}")
    key = (book_id, page_id, version)
    data = _preview_cache.get(key)
    if data is None:
        data = await _render_preview(
            request.app.state.tiles, settings, book_id, page_id, version, path
        )
        if len(_preview_cache) >= 32:
            _preview_cache.clear()
        _preview_cache[key] = data
    # The real preview is share-cacheable only when the page is not
    # region-locked (open to everyone); otherwise browser-cache only.
    cache_control = (
        "public, max-age=31536000, immutable"
        if not access["region_locked"]
        else "private, max-age=31536000, immutable"
    )
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": cache_control},
    )


def _iso_date(mtime_ns: int) -> str:
    """Format a page mtime (ns since epoch) as a sitemap ``YYYY-MM-DD`` date."""
    return datetime.fromtimestamp(mtime_ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%d")


@router.get("/sitemap.xml")
async def sitemap_endpoint(request: Request) -> Response:
    """Return an XML sitemap of every book and page for search engines.

    URLs use the persisted short-id share links (``/{id}``), which are stable
    across restarts, so indexed pages keep working. Rendered once per catalog
    TTL and served with matching cache headers.

    Args:
        request: FastAPI request (to reach ``app.state`` services).

    Returns:
        A ``application/xml`` urlset listing the root, each book, and each page.
    """
    now = time.monotonic()
    global _sitemap_cache
    if _sitemap_cache is not None and now - _sitemap_cache[0] < _SITEMAP_TTL:
        return Response(
            content=_sitemap_cache[1],
            media_type="application/xml",
            headers={"Cache-Control": f"public, max-age={int(_SITEMAP_TTL)}"},
        )

    catalog = request.app.state.catalog
    locations = request.app.state.locations
    rights = request.app.state.rights
    base = str(request.base_url).rstrip("/")

    entries = [f"  <url><loc>{base}/</loc><priority>1.0</priority></url>"]
    try:
        _, books = catalog.books()
    except Exception:  # noqa: BLE001 — an unavailable archive yields an empty sitemap
        books = []

    pairs: list[tuple[str, str | None]] = []
    meta: list[tuple[str, str | None]] = []  # (mtime_ns, priority) per pair
    for book in books:
        # Private books are excluded from the sitemap entirely.
        if rights.book_visibility(book.id) != "public":
            continue
        pairs.append((book.id, None))
        meta.append((book.cover.mtime, "0.9"))
        try:
            _, pages = catalog.pages(book.id)
        except Exception:  # noqa: BLE001 — skip books that fail to re-scan
            pages = []
        for page in pages:
            pairs.append((book.id, page.page_id))
            meta.append((page.mtime, "0.6"))

    for ident, (mtime_ns, priority) in zip(locations.get_ids(pairs), meta):
        if mtime_ns:
            entries.append(
                f'  <url><loc>{base}/{quote(ident)}</loc>'
                f"<lastmod>{_iso_date(mtime_ns)}</lastmod><priority>{priority}</priority></url>"
            )
        else:
            entries.append(f"  <url><loc>{base}/{quote(ident)}</loc><priority>{priority}</priority></url>")

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(entries)
        + "\n</urlset>\n"
    ).encode("utf-8")
    _sitemap_cache = (now, xml)
    return Response(
        content=xml,
        media_type="application/xml",
        headers={"Cache-Control": f"public, max-age={int(_SITEMAP_TTL)}"},
    )
