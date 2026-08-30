"""Time-limited share links: mint and inspect server-stored ``?key=`` secrets.

The Share button offers a time-limited link for private works, so a recipient
outside the rights/region system can see the shared work until the key is
revoked or expires. Keys are random 32-byte secrets held authoritatively in the
share store (:mod:`shares.store`) — minted here, verified through the store on
every content request, and manageable from the admin pages (list, extend,
revoke). ``GET /api/share/info`` lets clients validate a key and read its
metadata.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from ..auth.service import Viewer, current_viewer
from ..errors import BadRequest, NotFound, Unauthorized
from .store import SHARE_DURATIONS

router = APIRouter(tags=["shares"])


class ShareBody(BaseModel):
    """Request body for ``POST /api/share``."""

    book: str
    page: str | None = None
    duration: int = 86400


@router.post("/api/share")
def create_share_endpoint(
    request: Request,
    body: ShareBody,
    viewer: Viewer = Depends(current_viewer),
) -> dict:
    """Mint a time-limited share key for a ``(book, page)`` location.

    Owner-only (or an account granted the book). The key grants the book's
    pages in full — in every region — until it is revoked or expires: the
    whole book when ``page`` is omitted, else exactly that page.

    Args:
        request: FastAPI request (to reach ``app.state`` services).
        body: ``{book, page?, duration}``, duration one of :data:`SHARE_DURATIONS`.
        viewer: The current session's identity (share keys never count).

    Returns:
        ``{key, book, page, expires_at, created_at}``; ``key`` appends to a
        share URL as ``?key=``.

    Raises:
        errors.BadRequest: For an unsupported duration.
        errors.Unauthorized: For viewers without owner/account-grant access.
        errors.NotFound: For unknown books or pages.
    """
    if body.duration not in SHARE_DURATIONS:
        raise BadRequest("unsupported duration")
    granted = viewer.kind == "owner" or (
        viewer.kind == "account" and body.book in viewer.grants
    )
    if not granted:
        raise Unauthorized("not allowed to create share links")
    # Validate the location exists (the catalog scan is rights-agnostic, so
    # private books are reachable here by design).
    catalog = request.app.state.catalog
    _, pages = catalog.pages(body.book)  # raises errors.NotFound if gone
    if body.page is not None and not any(p.page_id == body.page for p in pages):
        raise NotFound(f"page not found: {body.page}")
    store = request.app.state.shares
    created_by = viewer.username or viewer.kind
    key = store.create(body.book, body.page, body.duration, created_by)
    now = int(time.time())
    return {
        "key": key,
        "book": body.book,
        "page": body.page,
        "expires_at": datetime.fromtimestamp(now + body.duration, tz=timezone.utc).isoformat(),
        "created_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
    }


@router.get("/api/share/info")
def share_info_endpoint(
    request: Request,
    key: str = Query(min_length=1),
) -> dict:
    """Validate a share key and return its metadata.

    Possession of the key is the credential, so no session is required. This
    is also the one endpoint that records the key's last use (for the admin
    manager); per-request viewer resolution never writes.

    Args:
        request: FastAPI request (to reach ``app.state.shares``).
        key: The raw share key from a URL query or cookie.

    Returns:
        ``{"valid": false}`` for unknown, revoked, or expired keys, else
        ``{"valid": true, "book", "page", "expires_at", "revoked"}``.
    """
    store = request.app.state.shares
    row = store.lookup(key)
    now = int(time.time())
    if row is None or row["revoked_at"] is not None:
        return {"valid": False}
    if row["expires_at"] is not None and row["expires_at"] <= now:
        return {"valid": False}
    store.touch(key)
    return {
        "valid": True,
        "book": row["book"],
        "page": row["page"],
        "expires_at": row["expires_at"],
        "revoked": False,
    }
