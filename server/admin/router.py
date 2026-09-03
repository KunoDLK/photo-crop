"""Server-rendered admin CRUD for the rights database.

Every mutation is a standard POST form carrying a per-session CSRF token (see
:meth:`~server.auth.service.AuthService.csrf_token`); GET pages are plain HTML.
The whole section is gated by the app-level owner session — Cloudflare Access
guards the network layer in production. Anonymous visitors get a login form,
not the UI.
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import quote

from fastapi import APIRouter, Request
from starlette.responses import HTMLResponse, RedirectResponse

from ..auth.service import SESSION_TTL, COOKIE_NAME, AuthService
from ..errors import BadRequest, NotFound, TooManyRequests, Unauthorized
from ..pages import _public_base
from . import pages

router = APIRouter(tags=["admin"])


def _owner_viewer(request: Request):
    """The current viewer when it is the owner, else None (render login page)."""
    viewer = request.app.state.auth.viewer_from_request(request)
    return viewer if viewer.kind == "owner" else None


def _require_owner(request: Request):
    """The current viewer if it is the owner, else 401 (used by POSTs)."""
    viewer = request.app.state.auth.viewer_from_request(request)
    if viewer.kind != "owner":
        raise Unauthorized("owner login required")
    return viewer


def _check_csrf(request: Request, form) -> None:
    """Reject a form without a valid per-session CSRF token."""
    if not request.app.state.auth.verify_csrf(request, form.get("_csrf")):
        raise BadRequest("invalid or missing CSRF token")


def _redirect(location: str) -> RedirectResponse:
    """303 after a successful POST (PRG: refresh cannot resubmit)."""
    return RedirectResponse(location, status_code=303)


def _int_or_none(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise BadRequest(f"invalid integer: {value}")


def _editor_names(store) -> dict[int, str]:
    return {e["id"]: e["name"] for e in store.list_editors()}


# --------------------------------------------------------------- login gate

@router.get("/admin")
async def admin_index(request: Request) -> HTMLResponse:
    """Landing page (or the login form when no owner session)."""
    viewer = request.app.state.auth.viewer_from_request(request)
    if viewer.kind != "owner":
        return HTMLResponse(pages.login())
    state = request.app.state
    try:
        _, books = state.catalog.books()
    except Exception:  # noqa: BLE001 — admin stays usable without an archive
        books = []
    counts = {
        "books": len(books),
        "editors": len(state.rights.list_editors()),
        "holders": len(state.rights.list_rights_holders()),
        "users": len(state.rights.list_users()),
    }
    return HTMLResponse(pages.index(state.auth.csrf_token(request), counts))


@router.get("/admin/login")
async def admin_login_form(request: Request) -> HTMLResponse:
    """The owner login form."""
    del request
    return HTMLResponse(pages.login())


@router.post("/admin/login")
async def admin_login(request: Request):
    """Authenticate the owner from a form and set the session cookie.

    Accounts cannot administer: only the owner session passes the gate.
    """
    form = await request.form()
    auth: AuthService = request.app.state.auth
    ip = request.client.host if request.client else None
    try:
        viewer = await asyncio.to_thread(
            auth.login, ip, form.get("username", ""), form.get("password", "")
        )
    except TooManyRequests as e:
        return HTMLResponse(pages.login(e.message), status_code=429)
    if viewer is None or viewer.kind != "owner":
        return HTMLResponse(pages.login("invalid credentials or not the owner"), status_code=401)
    response = _redirect("/admin")
    settings = request.app.state.settings
    response.set_cookie(
        key=COOKIE_NAME,
        value=auth.create_session(viewer),
        max_age=SESSION_TTL,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/admin/logout")
async def admin_logout(request: Request) -> RedirectResponse:
    """Clear the session cookie (CSRF-guarded)."""
    _check_csrf(request, await request.form())
    settings = request.app.state.settings
    response = _redirect("/admin")
    response.delete_cookie(
        key=COOKIE_NAME, path="/", httponly=True,
        secure=settings.session_cookie_secure, samesite="lax",
    )
    return response


# ------------------------------------------------------------------- books

@router.get("/admin/books")
async def books_page(request: Request) -> HTMLResponse:
    """Book list with per-book visibility/editor/holder/year editing."""
    if _owner_viewer(request) is None:
        return HTMLResponse(pages.login())
    state = request.app.state
    try:
        _, books = state.catalog.books()
    except Exception:  # noqa: BLE001
        books = []
    editors = state.rights.list_editors()
    holders = state.rights.list_rights_holders()
    rows = [
        {"book": b, "rights": state.rights.book_row(b.id)}
        for b in books
    ]
    return HTMLResponse(pages.books(state.auth.csrf_token(request), rows, editors, holders))


@router.post("/admin/books/{book_id}")
async def book_update(request: Request, book_id: str) -> RedirectResponse:
    """Save a book's visibility, publication year, editor and rights holder."""
    _require_owner(request)
    form = await request.form()
    _check_csrf(request, form)
    rights = request.app.state.rights
    rights.update_book(
        book_id,
        visibility=form.get("visibility") or None,
        publication_year=_int_or_none(form.get("publication_year")),
        editor_id=_int_or_none(form.get("editor_id")),
        rights_holder_id=_int_or_none(form.get("rights_holder_id")),
    )
    return _redirect("/admin/books")


# ----------------------------------------------------------------- editors

@router.get("/admin/editors")
async def editors_page(request: Request) -> HTMLResponse:
    """Editor CRUD list."""
    if _owner_viewer(request) is None:
        return HTMLResponse(pages.login())
    state = request.app.state
    return HTMLResponse(
        pages.editors_page(state.auth.csrf_token(request), state.rights.list_editors())
    )


@router.post("/admin/editors")
async def editor_create(request: Request) -> RedirectResponse:
    """Create an editor."""
    _require_owner(request)
    form = await request.form()
    _check_csrf(request, form)
    try:
        request.app.state.rights.create_editor(
            form.get("name", ""),
            birth_year=_int_or_none(form.get("birth_year")),
            death_year=_int_or_none(form.get("death_year")),
            notes=form.get("notes") or None,
        )
    except ValueError as e:
        raise BadRequest(str(e))
    return _redirect("/admin/editors")


@router.post("/admin/editors/{editor_id}")
async def editor_update(request: Request, editor_id: int) -> RedirectResponse:
    """Update an editor."""
    _require_owner(request)
    form = await request.form()
    _check_csrf(request, form)
    request.app.state.rights.update_editor(
        editor_id,
        name=form.get("name") or None,
        birth_year=_int_or_none(form.get("birth_year")),
        death_year=_int_or_none(form.get("death_year")),
        notes=form.get("notes") or None,
    )
    return _redirect("/admin/editors")


@router.post("/admin/editors/{editor_id}/delete")
async def editor_delete(request: Request, editor_id: int) -> RedirectResponse:
    """Delete an editor (book/page references become unset)."""
    _require_owner(request)
    _check_csrf(request, await request.form())
    request.app.state.rights.delete_editor(editor_id)
    return _redirect("/admin/editors")


# ---------------------------------------------------------- rights holders

@router.get("/admin/holders")
async def holders_page(request: Request) -> HTMLResponse:
    """Rights-holder CRUD list."""
    if _owner_viewer(request) is None:
        return HTMLResponse(pages.login())
    state = request.app.state
    return HTMLResponse(
        pages.holders_page(state.auth.csrf_token(request), state.rights.list_rights_holders())
    )


@router.post("/admin/holders")
async def holder_create(request: Request) -> RedirectResponse:
    """Create a rights holder."""
    _require_owner(request)
    form = await request.form()
    _check_csrf(request, form)
    try:
        request.app.state.rights.create_rights_holder(
            form.get("name", ""),
            kind=form.get("kind") or "unknown",
            contact=form.get("contact") or None,
            notes=form.get("notes") or None,
        )
    except ValueError as e:
        raise BadRequest(str(e))
    return _redirect("/admin/holders")


@router.post("/admin/holders/{holder_id}")
async def holder_update(request: Request, holder_id: int) -> RedirectResponse:
    """Update a rights holder."""
    _require_owner(request)
    form = await request.form()
    _check_csrf(request, form)
    request.app.state.rights.update_rights_holder(
        holder_id,
        name=form.get("name") or None,
        kind=form.get("kind") or None,
        contact=form.get("contact") or None,
        notes=form.get("notes") or None,
    )
    return _redirect("/admin/holders")


@router.post("/admin/holders/{holder_id}/delete")
async def holder_delete(request: Request, holder_id: int) -> RedirectResponse:
    """Delete a rights holder (book references become unset)."""
    _require_owner(request)
    _check_csrf(request, await request.form())
    request.app.state.rights.delete_rights_holder(holder_id)
    return _redirect("/admin/holders")


# ------------------------------------------------------------- page rights

@router.get("/admin/pages")
async def pages_chooser(request: Request, book: str | None = None) -> HTMLResponse:
    """Book chooser for the page-rights walk.

    The chooser's Open button submits a plain GET form carrying the selected
    book as ``?book=<id>``; redirect that to the path-based per-book screen
    (``/admin/pages/{book_id}``) so the same URL always works, bookmarks and
    pasted links included.
    """
    if _owner_viewer(request) is None:
        return HTMLResponse(pages.login())
    if book:
        return _redirect(f"/admin/pages/{quote(book)}")
    state = request.app.state
    try:
        _, books = state.catalog.books()
    except Exception:  # noqa: BLE001
        books = []
    return HTMLResponse(pages.page_chooser(state.auth.csrf_token(request), books))


@router.get("/admin/pages/{book_id}")
async def pages_screen(request: Request, book_id: str) -> HTMLResponse:
    """One book's page-rights screen: default, bulk, and per-page overrides."""
    if _owner_viewer(request) is None:
        return HTMLResponse(pages.login())
    state = request.app.state
    _, catalog_pages = state.catalog.pages(book_id, force=True)
    editors_by_page = state.rights.page_editors_map(book_id)
    kinds = state.rights.page_kinds_map(book_id)
    defaults = state.rights.page_defaults_map(book_id)
    book_row = state.rights.book_row(book_id)
    editors = state.rights.list_editors()
    return HTMLResponse(
        pages.page_rights(
            state.auth.csrf_token(request), book_id, catalog_pages, editors_by_page,
            book_row, editors, kinds, defaults,
        )
    )


@router.post("/admin/pages/{book_id}")
async def page_overrides_save(request: Request, book_id: str) -> RedirectResponse:
    """Apply per-page editor sets, copyright kinds and default-access flags.

    Empty keeps the current value; ``__clear__`` (single-editor legacy) and the
    empty option in a multi-select remove a rule.
    """
    _require_owner(request)
    form = await request.form()
    _check_csrf(request, form)
    rights = request.app.state.rights
    editors_by_page: dict[str, list] = {}
    for key, value in form.multi_items():
        if key.startswith("editors_"):
            page_id = key[len("editors_"):]
            # "" is the explicit "clear per-page editors" sentinel.
            if value == "":
                editors_by_page[page_id] = []
            else:
                editors_by_page.setdefault(page_id, []).append(_int_or_none(value))
            continue
        if value == "":
            continue
        if key.startswith("editor_"):  # legacy single-editor field
            page_id = key[len("editor_"):]
            editor_id = None if value == "__clear__" else _int_or_none(value)
            rights.set_page_editor(book_id, page_id, editor_id)
        elif key.startswith("kind_"):
            page_id = key[len("kind_"):]
            rights.set_page_copyright_kind(book_id, page_id, value)
        elif key.startswith("default_"):
            page_id = key[len("default_"):]
            rights.set_page_default(book_id, page_id, value)
    for page_id, editor_ids in editors_by_page.items():
        rights.set_page_editors(book_id, page_id, editor_ids)
    return _redirect(f"/admin/pages/{quote(book_id)}")


@router.post("/admin/pages/{book_id}/default")
async def page_default_save(request: Request, book_id: str) -> RedirectResponse:
    """Set the whole-book default editor."""
    _require_owner(request)
    form = await request.form()
    _check_csrf(request, form)
    request.app.state.rights.set_page_editor(
        book_id, None, _int_or_none(form.get("editor_id"))
    )
    return _redirect(f"/admin/pages/{quote(book_id)}")


@router.post("/admin/pages/{book_id}/bulk")
async def page_bulk_save(request: Request, book_id: str) -> RedirectResponse:
    """Set (or clear) the editor of every page, plus optional kind/default."""
    _require_owner(request)
    form = await request.form()
    _check_csrf(request, form)
    editor_id = _int_or_none(form.get("editor_id"))
    default_access = form.get("default_access") or ""
    kind = form.get("kind") or ""
    rights = request.app.state.rights
    _, catalog_pages = request.app.state.catalog.pages(book_id, force=True)
    for page in catalog_pages:
        rights.set_page_editor(book_id, page.page_id, editor_id)
        if default_access:
            rights.set_page_default(book_id, page.page_id, default_access)
        if kind:
            rights.set_page_copyright_kind(book_id, page.page_id, kind)
    return _redirect(f"/admin/pages/{quote(book_id)}")


# ------------------------------------------------------------------- users

@router.get("/admin/users")
async def users_page(request: Request) -> HTMLResponse:
    """Account list with password reset, grants and delete."""
    if _owner_viewer(request) is None:
        return HTMLResponse(pages.login())
    state = request.app.state
    try:
        _, books = state.catalog.books()
    except Exception:  # noqa: BLE001
        books = []
    users = state.rights.list_users()
    grants = {u["id"]: state.rights.user_grants(u["id"]) for u in users}
    return HTMLResponse(
        pages.users(state.auth.csrf_token(request), users, books, grants)
    )


@router.post("/admin/users")
async def user_create(request: Request) -> RedirectResponse:
    """Create an account (pbkdf2-hashed password)."""
    _require_owner(request)
    form = await request.form()
    _check_csrf(request, form)
    from ..auth.service import _hash_password

    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    if not username or not password:
        raise BadRequest("username and password are required")
    try:
        request.app.state.rights.create_user(username, _hash_password(password))
    except ValueError as e:
        raise BadRequest(str(e))
    return _redirect("/admin/users")


@router.post("/admin/users/{user_id}/password")
async def user_password(request: Request, user_id: int) -> RedirectResponse:
    """Reset an account's password."""
    _require_owner(request)
    form = await request.form()
    _check_csrf(request, form)
    from ..auth.service import _hash_password

    password = form.get("password") or ""
    if not password:
        raise BadRequest("password is required")
    request.app.state.rights.set_user_password(user_id, _hash_password(password))
    return _redirect("/admin/users")


@router.post("/admin/users/{user_id}/grants")
async def user_grants(request: Request, user_id: int) -> RedirectResponse:
    """Replace an account's book grants with the checked books."""
    _require_owner(request)
    form = await request.form()
    _check_csrf(request, form)
    rights = request.app.state.rights
    checked = {
        key[len("book_"):] for key in form.keys() if key.startswith("book_")
    }
    rights.set_user_grants(user_id, checked)
    return _redirect("/admin/users")


@router.post("/admin/users/{user_id}/delete")
async def user_delete(request: Request, user_id: int) -> RedirectResponse:
    """Delete an account (grants cascade)."""
    _require_owner(request)
    _check_csrf(request, await request.form())
    request.app.state.rights.delete_user(user_id)
    return _redirect("/admin/users")


# ---------------------------------------------------------------- share links

@router.get("/admin/shares")
async def admin_shares(request: Request) -> HTMLResponse:
    """Share-link manager: list all keys, create new ones."""
    viewer = request.app.state.auth.viewer_from_request(request)
    if viewer.kind != "owner":
        return HTMLResponse(pages.login())
    state = request.app.state
    try:
        _, books = state.catalog.books()
    except Exception:  # noqa: BLE001 — admin stays usable without an archive
        books = []
    return HTMLResponse(
        pages.shares(state.auth.csrf_token(request), state.shares.list(), books)
    )


@router.post("/admin/shares")
async def admin_shares_create(request: Request) -> RedirectResponse:
    """Create a share key for a (book, optional page) location."""
    _require_owner(request)
    form = await request.form()
    _check_csrf(request, form)
    state = request.app.state
    book = (form.get("book") or "").strip()
    page = (form.get("page") or "").strip() or None
    if not book:
        raise BadRequest("book is required")
    _, pages = state.catalog.pages(book)  # raises errors.NotFound if gone
    if page is not None and not any(p.page_id == page for p in pages):
        raise BadRequest(f"page not found: {page}")
    duration = _int_or_none(form.get("duration"))
    if duration is None or duration < 0:
        raise BadRequest("invalid duration")
    if duration == 0:
        duration = None  # no expiry — stored as NULL
    note = (form.get("note") or "").strip() or None
    state.shares.create(book, page, duration, "admin", note)
    return _redirect("/admin/shares")


@router.post("/admin/shares/{share_id}/copy")
async def admin_shares_copy(request: Request, share_id: int) -> dict:
    """Mint a fresh key for the same location and return its share URL.

    Keys are stored hashed, so an existing key's plaintext can never be
    recovered; "copy" therefore re-issues the share as a brand-new key with
    the same duration and hands back the full ``/…?key=…`` URL for the
    clipboard (the new key shows up as its own row in the list).
    """
    _require_owner(request)
    _check_csrf(request, await request.form())
    state = request.app.state
    row = state.shares.get(share_id)
    if row is None:
        raise NotFound(f"share key not found: {share_id}")
    ttl = None  # never-expiring keys copy as never-expiring
    if row["expires_at"] is not None:
        ttl = row["expires_at"] - row["created_at"]
        if ttl <= 0:
            raise BadRequest("share key has no usable duration")
    note = f"copy of #{share_id}"
    if row["note"]:
        note += f" ({row['note']})"
    key = state.shares.create(row["book"], row["page"], ttl, "admin", note)
    loc_id = state.locations.get_id(row["book"], row["page"])
    url = f"{_public_base(request)}/{loc_id}?key={key}"
    return {"url": url}


@router.post("/admin/shares/{share_id}/extend")
async def admin_shares_extend(request: Request, share_id: int) -> RedirectResponse:
    """Set a key's expiry to now + duration (or never, with 0)."""
    _require_owner(request)
    form = await request.form()
    _check_csrf(request, form)
    duration = _int_or_none(form.get("duration"))
    if duration is None or duration < 0:
        raise BadRequest("invalid duration")
    if not request.app.state.shares.set_expiry(share_id, None if duration == 0 else int(time.time()) + duration):
        raise NotFound(f"share key not found: {share_id}")
    return _redirect("/admin/shares")


@router.post("/admin/shares/{share_id}/revoke")
async def admin_shares_revoke(request: Request, share_id: int) -> RedirectResponse:
    """Invalidate a share key immediately."""
    _require_owner(request)
    _check_csrf(request, await request.form())
    if not request.app.state.shares.revoke(share_id):
        raise NotFound(f"share key not found: {share_id}")
    return _redirect("/admin/shares")


@router.post("/admin/shares/{share_id}/restore")
async def admin_shares_restore(request: Request, share_id: int) -> RedirectResponse:
    """Un-revoke a share key (reactivates it)."""
    _require_owner(request)
    _check_csrf(request, await request.form())
    if not request.app.state.shares.restore(share_id):
        raise NotFound(f"share key not found: {share_id}")
    return _redirect("/admin/shares")


@router.post("/admin/shares/{share_id}/delete")
async def admin_shares_delete(request: Request, share_id: int) -> RedirectResponse:
    """Hard-delete a share key."""
    _require_owner(request)
    _check_csrf(request, await request.form())
    if not request.app.state.shares.delete(share_id):
        raise NotFound(f"share key not found: {share_id}")
    return _redirect("/admin/shares")
