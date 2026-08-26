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
from .rights.policy import BLURRED, FULL

SITE_TITLE = "Hyper.K Archive"
DESCRIPTION = (
    "Hyper.K Archive is a speed-optimised archive viewer that displays hundreds "
    "of high-detail scans without slowdown, using dynamic quality tiling."
)


def esc(text) -> str:
    """Escape text for use inside an HTML attribute."""
    return html_mod.escape(str(text), quote=True)


def _og_image_url(request: Request, book: str, page: str, mtime: int) -> str:
    """Absolute URL for a page's real preview image (content-addressed).

    Args:
        request: FastAPI request (for the base URL).
        book: Book directory name.
        page: Page filename.
        mtime: Page file mtime (content version).

    Returns:
        The absolute ``/og/...`` preview URL.
    """
    base = str(request.base_url).rstrip("/")
    return f"{base}/og/{quote(book)}/{quote(page)}/{mtime}.jpg"


def _page_meta(request: Request, book_id: str, page, blurred: bool = False) -> dict:
    """OG metadata for a single page (no preview image when region-locked)."""
    return {
        "title": f"{book_id} • Page {page.order}",
        "image": None if blurred else _og_image_url(request, book_id, page.page_id, page.mtime),
    }


def _book_meta(request: Request, book_id: str, pages, blurred: bool = False) -> dict:
    """OG metadata for a book: preview its first page (none when region-locked)."""
    page = pages[0]
    return {
        "title": book_id,
        "image": None if blurred else _og_image_url(request, book_id, page.page_id, page.mtime),
    }


def _canonical(request: Request) -> str:
    """Absolute URL of the current path (the one true URL for this location)."""
    base = str(request.base_url).rstrip("/")
    path = request.url.path.rstrip("/")
    return base + (path or "/")


def _viewer_of(request: Request):
    """The requester's identity (crawlers are anonymous; sessions count)."""
    return request.app.state.auth.viewer_from_request(request)


def _access_of(request: Request, book_id: str, page_id: str) -> dict:
    """Resolve access for one page against the requester's region."""
    zone = request.app.state.region.zone_of_request(request)
    return request.app.state.policy.resolve(_viewer_of(request), book_id, page_id, zone)


def _noindex_meta() -> dict:
    """Meta for a location that must not be indexed (private books/pages)."""
    return {"title": SITE_TITLE, "image": None, "robots": "noindex, nofollow"}


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
    """The root view: a link to every public book, first cover as OG image.

    Private books are invisible to crawlers (no session), so only public books
    are listed; logged-in viewers get the full list from the API instead.
    """
    state = request.app.state
    viewer = _viewer_of(request)
    try:
        _, books = state.catalog.books()
    except Exception:  # noqa: BLE001 — an unavailable archive yields an empty list
        books = []
    public_books = [
        b for b in books
        if state.rights.book_visibility(b.id) == "public"
        or viewer.kind == "owner"
        or (viewer.kind == "account" and b.id in viewer.grants)
    ]
    meta = {"title": SITE_TITLE, "image": None}
    if public_books:
        cover_book = public_books[0]
        cover = cover_book.cover
        access = _access_of(request, cover_book.id, cover.page_id)
        if access["status"] != BLURRED:
            meta["image"] = _og_image_url(
                request, cover_book.id, cover.page_id, cover.mtime
            )
    ids = state.locations.get_ids([(b.id, None) for b in public_books])
    items = "".join(
        f'    <li><a href="/{esc(ident)}">{esc(book.name)}</a></li>\n'
        for ident, book in zip(ids, public_books)
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
    """A book view: breadcrumb plus a link to every page (the crawl hub).

    Private books (no grant) render an empty fragment with ``noindex`` — they
    do not exist to anyone not logged in.
    """
    state = request.app.state
    viewer = _viewer_of(request)
    granted = viewer.kind == "owner" or (
        viewer.kind == "account" and book_id in viewer.grants
    )
    if state.rights.book_visibility(book_id) != "public" and not granted:
        return "", _noindex_meta()
    _, pages = state.catalog.pages(book_id)  # raises errors.NotFound if gone
    if not granted:
        access = _access_of(request, book_id, pages[0].page_id)
        blurred = access["status"] == BLURRED
    else:
        blurred = False
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
    return content, _book_meta(request, book_id, pages, blurred)


def _render_page(request: Request, book_id: str, page_id: str) -> tuple[str, dict]:
    """A page view: breadcrumb, OCR text, and prev/next/back-to-book links.

    Gated by access: ``full`` pages render their OCR text; ``blurred`` pages
    keep the structure and pager but no OCR text (the text is a copy of the
    work) and a blurred preview image; ``nonexistent`` (private, no grant)
    renders an empty fragment with ``noindex``.
    """
    state = request.app.state
    _, pages = state.catalog.pages(book_id)  # raises errors.NotFound if gone
    index = next((i for i, p in enumerate(pages) if p.page_id == page_id), None)
    if index is None:
        raise NotFound(f"page not found: {page_id}")
    page = pages[index]

    access = _access_of(request, book_id, page_id)
    status = access["status"]
    if status == "nonexistent":
        return "", _noindex_meta()

    book_url = state.locations.get_id(book_id, None)
    prev_url = (
        state.locations.get_id(book_id, pages[index - 1].page_id)
        if index > 0 else None
    )
    next_url = (
        state.locations.get_id(book_id, pages[index + 1].page_id)
        if index + 1 < len(pages) else None
    )

    paragraphs = ""
    if status == FULL:
        ocr = state.ocr.get_page_ocr_cached(book_id, page_id)
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
    # Blurred pages stay indexable (crawlers see the blurred preview); only
    # nonexistent/private locations got the noindex meta above.
    return content, _page_meta(request, book_id, page, blurred=status == BLURRED)
