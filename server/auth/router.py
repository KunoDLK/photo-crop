"""HTTP routes for login, logout, and session introspection.

Login exchanges credentials for an HMAC-signed session cookie (httpOnly,
Secure, SameSite=Lax); ``/api/me`` reports the current viewer; logout clears
the cookie. Credential verification (pbkdf2) is short-blocking and runs in the
thread pool so the event loop stays free.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from pydantic import BaseModel
from starlette.responses import JSONResponse

from ..errors import Unauthorized
from ..models import MeResponse
from .service import SESSION_TTL, COOKIE_NAME, AuthService, Viewer, current_viewer

router = APIRouter(tags=["auth"])


class LoginBody(BaseModel):
    """Credentials submitted to ``POST /api/login``."""

    username: str
    password: str


def _profile(viewer: Viewer) -> dict:
    """The viewer profile shared by login success and ``/api/me``."""
    return {
        "authenticated": viewer.authenticated,
        "username": viewer.username,
        "is_owner": viewer.kind == "owner",
        "grants": sorted(viewer.grants),
    }


@router.post("/api/login")
async def login_endpoint(request: Request, body: LoginBody) -> JSONResponse:
    """Exchange credentials for a session cookie.

    Args:
        request: FastAPI request (to reach ``app.state.auth`` and peer IP).
        body: ``{username, password}``.

    Returns:
        200 with the viewer profile and a session ``Set-Cookie``; 401 for bad
        credentials; 429 when the IP is past the failure rate limit.
    """
    auth: AuthService = request.app.state.auth
    ip = request.client.host if request.client else None
    viewer = await asyncio.to_thread(auth.login, ip, body.username, body.password)
    if viewer is None:
        raise Unauthorized("invalid username or password")
    response = JSONResponse(_profile(viewer))
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


@router.post("/api/logout")
async def logout_endpoint(request: Request) -> JSONResponse:
    """Clear the session cookie."""
    settings = request.app.state.settings
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(
        key=COOKIE_NAME,
        path="/",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )
    return response


@router.get("/api/me", response_model=MeResponse)
def me_endpoint(request: Request) -> MeResponse:
    """Report the current viewer's identity and book grants.

    Args:
        request: FastAPI request (session cookie resolved to a Viewer).

    Returns:
        ``{authenticated, username, is_owner, grants}`` for the current viewer.
    """
    viewer = current_viewer(request)
    return MeResponse(
        authenticated=viewer.authenticated,
        username=viewer.username,
        is_owner=viewer.kind == "owner",
        grants=sorted(viewer.grants),
    )
