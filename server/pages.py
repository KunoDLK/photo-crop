"""Server-rendered HTML fragments for crawlers and no-JS visitors.

Every path serves the same viewer shell (see :func:`social.spa_response`); this
module builds the per-path body content: the root book list, a book's page grid,
and a single page's OCR text with prev/next/back links. The fragment is plain
HTML with real links, so search engines can crawl and index every location
without running JavaScript, while the client re-renders the same locations
interactively. Both audiences get identical markup (no cloaking).

Also owns the Open Graph metadata helpers used by :mod:`social`, since they
resolve the same per-path views.
"""
from __future__ import annotations

import html as html_mod
from urllib.parse import quote

from starlette.requests import Request

from .errors import NotFound

SITE_TITLE = "Hyper.K Archive"
DESCRIPTION = (
    "Hyper.K Archive is a speed-optimised archive viewer that displays hundreds "
    "of high-detail scans without slowdown, using dynamic quality tiling."
)


def esc(text) -> str:
    """Escape text for use inside an HTML attribute."""
    return html_mod.escape(str(text), quote=True)


def _og_image_url(request: Request, book: str, page: str, mtime: int) -> str:
    """Absolute URL for the preview image of one page (content-addressed)."""
    base = str(request.base_url).rstrip("/")
    return f"{base}/og/{quote(book)}/{quote(page)}/{mtime}.jpg"


def _page_meta(request: Request, book_id: str, page) -> dict:
    """OG metadata for a single page."""
    return {
        "title": f"{book_id} • Page {page.order}",
        "image": _og_image_url(request, book_id, page.page_id, page.mtime),
    }


def _book_meta(request: Request, book_id: str, pages) -> dict:
    """OG metadata for a book: preview its first page."""
    page = pages[0]
    return {
        "title": book_id,
        "image": _og_image_url(request, book_id, page.page_id, page.mtime),
    }


def _canonical(request: Request) -> str:
    """Absolute URL of the current path (the one true URL for this location)."""
    base = str(request.base_url).rstrip("/")
    path = request.url.path.rstrip("/")
    return base + (path or "/")


def render_fragment(request: Request) -> tuple[str, dict]:
    """Return ``(body-fragment HTML, OG meta)`` for the request path.

    The root renders a link to every book; a bare root segment (``/93050a0``)
    resolves through the location registry to a book or page and renders that
    view; every other path gets an empty fragment with the default meta.

    Args:
        request: FastAPI request (to reach ``app.state`` services).

    Returns:
        ``(content, meta)`` where ``content`` is the fragment inserted into the
        viewer shell's ``#seo-content`` div (empty for unknown paths) and
        ``meta`` carries the canonical URL in addition to the OG fields.

    Raises:
        errors.NotFound: The path is a location id whose book or page is gone.
    """
    path = request.url.path.rstrip("/")
    if path in ("", "/index.html"):
        content, meta = _render_root(request)
    elif not path or "/" in path.lstrip("/"):
        content, meta = "", {"title": SITE_TITLE, "image": None}
    else:
        loc = request.app.state.locations.resolve(path.lstrip("/"))
        if loc is None:
            content, meta = "", {"title": SITE_TITLE, "image": None}
        elif loc["page"] is None:
            content, meta = _render_book(request, loc["book"])
        else:
            content, meta = _render_page(request, loc["book"], loc["page"])
    meta["canonical"] = _canonical(request)
    return content, meta


def _render_root(request: Request) -> tuple[str, dict]:
    """The root view: a link to every book, and the first book's cover as OG image."""
    state = request.app.state
    try:
        _, books = state.catalog.books()
    except Exception:  # noqa: BLE001 — an unavailable archive yields an empty list
        books = []
    meta = {"title": SITE_TITLE, "image": None}
    if books:
        cover = books[0].cover
        meta["image"] = _og_image_url(request, books[0].id, cover.page_id, cover.mtime)
    ids = state.locations.get_ids([(b.id, None) for b in books])
    items = "".join(
        f'    <li><a href="/{esc(ident)}">{esc(book.name)}</a></li>\n'
        for ident, book in zip(ids, books)
    )
    content = (
        '<main class="seo-list">\n'
        f"  <h1>{esc(SITE_TITLE)}</h1>\n"
        "  <ul>\n"
        f"{items}\n"
        "  </ul>\n"
        "</main>"
    )
    return content, meta


def _render_book(request: Request, book_id: str) -> tuple[str, dict]:
    """A book view: breadcrumb plus a link to every page (the crawl hub)."""
    state = request.app.state
    _, pages = state.catalog.pages(book_id)  # raises errors.NotFound if gone
    ids = state.locations.get_ids([(book_id, p.page_id) for p in pages])
    items = "".join(
        f'    <li><a href="/{esc(ident)}">{esc(f"{p.group}.{p.order}  {p.name}")}</a></li>\n'
        for ident, p in zip(ids, pages)
    )
    content = (
        '<main class="seo-book">\n'
        f'  <nav class="seo-breadcrumb"><a href="/">{esc(SITE_TITLE)}</a> › {esc(book_id)}</nav>\n'
        f"  <h1>{esc(book_id)}</h1>\n"
        '  <ul class="seo-grid">\n'
        f"{items}\n"
        "  </ul>\n"
        "</main>"
    )
    return content, _book_meta(request, book_id, pages)


def _render_page(request: Request, book_id: str, page_id: str) -> tuple[str, dict]:
    """A page view: breadcrumb, OCR text, and prev/next/back-to-book links."""
    state = request.app.state
    _, pages = state.catalog.pages(book_id)  # raises errors.NotFound if gone
    index = next((i for i, p in enumerate(pages) if p.page_id == page_id), None)
    if index is None:
        raise NotFound(f"page not found: {page_id}")
    page = pages[index]

    book_url = state.locations.get_id(book_id, None)
    page_url = state.locations.get_id(book_id, page_id)
    prev_url = (
        state.locations.get_id(book_id, pages[index - 1].page_id)
        if index > 0 else None
    )
    next_url = (
        state.locations.get_id(book_id, pages[index + 1].page_id)
        if index + 1 < len(pages) else None
    )

    ocr = state.ocr.get_page_ocr_cached(book_id, page_id)
    paragraphs = ""
    if ocr is not None and ocr.lines:
        paragraphs = "".join(f"    <p>{esc(line.text)}</p>\n" for line in ocr.lines)

    pager = [f'<a href="/{esc(book_url)}">All pages</a>']
    if prev_url:
        pager.insert(0, f'<a href="/{esc(prev_url)}">← Previous</a>')
    if next_url:
        pager.append(f'<a href="/{esc(next_url)}">Next →</a>')
    pager_html = "".join(f"    {item}\n" for item in pager)

    content = (
        '<main class="seo-page">\n'
        f'  <nav class="seo-breadcrumb"><a href="/">{esc(SITE_TITLE)}</a> › '
        f'<a href="/{esc(book_url)}">{esc(book_id)}</a> › Page {esc(page.order)}</nav>\n'
        f"  <h1>{esc(book_id)} • Page {esc(page.order)}</h1>\n"
        f"{paragraphs}"
        '  <nav class="seo-pager">\n'
        f"{pager_html}"
        "  </nav>\n"
        "</main>"
    )
    return content, _page_meta(request, book_id, page)
