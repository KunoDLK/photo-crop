"""Authentication: owner and account logins, sessions, rate limiting.

The owner is authenticated against the env credentials
(``ARCHIVE_USERNAME``/``ARCHIVE_PASSWORD``) with constant-time comparison;
account logins verify the pbkdf2 password hash stored in the rights database.
Successful logins mint a stateless HMAC-signed session cookie (30-day expiry);
the signing secret is generated once and persisted next to the rights DB so
sessions survive restarts. Failed logins are rate-limited per IP. The resulting
:class:`Viewer` (owner / account / anonymous) is the identity every policy
decision is resolved from.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import Request

from ..config import Settings
from ..errors import TooManyRequests
from ..rights.store import RightsStore

COOKIE_NAME = "bv_session"
SESSION_TTL = 30 * 86400  # seconds; ~30 days
_PBKDF2_ITERATIONS = 200_000
_RATE_WINDOW = 300  # seconds a failed attempt counts against an IP


@dataclass(frozen=True)
class Viewer:
    """The requester's identity: owner, a granted account, or anonymous.

    Attributes:
        kind: ``owner``, ``account`` or ``anonymous``.
        username: Login name for owner/account viewers.
        user_id: Rights-DB user row id for account viewers.
        grants: Book ids the account is explicitly granted (accounts only).
    """

    kind: str
    username: str | None = None
    user_id: int | None = None
    grants: frozenset[str] = frozenset()

    @property
    def authenticated(self) -> bool:
        """True for owner and account viewers."""
        return self.kind != "anonymous"


def _hash_password(password: str) -> str:
    """Hash a password with pbkdf2_hmac (sha256).

    Returns:
        A self-describing string ``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>``
        so the parameters travel with the hash and can be upgraded later.
    """
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt, _PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    """Check ``password`` against a ``_hash_password`` string in constant time.

    Returns:
        False for malformed or unknown-format hashes (fail closed).
    """
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
    except ValueError:
        return False
    return hmac.compare_digest(digest, bytes.fromhex(hash_hex))


class AuthService:
    """Session minting/verification, owner + account login, per-IP rate limits."""

    def __init__(self, settings: Settings, store: RightsStore) -> None:
        self._settings = settings
        self._store = store
        self._secret = self._load_secret(settings)
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    @staticmethod
    def _load_secret(settings: Settings) -> bytes:
        """Return the session-signing secret, generating and persisting one.

        An explicit ``session_secret`` env value wins; otherwise a random
        secret is written to ``cache_dir/secret`` (mode 0600) on first boot so
        sessions survive restarts without configuration. If the write fails the
        secret is ephemeral (all sessions die on restart).
        """
        if settings.session_secret:
            return settings.session_secret.encode()
        path: Path = settings.cache_dir / "secret"
        try:
            return path.read_bytes()
        except OSError:
            pass
        secret = secrets.token_bytes(32)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(secret)
            path.chmod(0o600)
        except OSError:
            pass
        return secret

    # ------------------------------------------------------------- sessions

    def _sign(self, body: str) -> str:
        """HMAC-SHA256 of the base64url body, base64url-encoded."""
        digest = hmac.new(self._secret, body.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def create_session(self, viewer: Viewer) -> str:
        """Mint an HMAC-signed session token for an authenticated viewer.

        Args:
            viewer: An authenticated (owner or account) Viewer.

        Returns:
            A ``<payload>.<signature>`` token to store in the session cookie.
        """
        payload = {
            "sub": viewer.username,
            "kind": viewer.kind,
            "user_id": viewer.user_id,
            "exp": int(time.time()) + SESSION_TTL,
        }
        body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        return f"{body}.{self._sign(body)}"

    def verify_session(self, token: str) -> Viewer | None:
        """Validate a session token and return its Viewer, or None when invalid.

        Rejects forged/altered tokens (HMAC), expired sessions, owner tokens
        whose username no longer matches the env credentials, and account
        tokens for deleted users. Grants are refreshed from the DB each call,
        so revocations take effect without waiting for expiry.
        """
        if not token or "." not in token:
            return None
        body, signature = token.split(".", 1)
        if not hmac.compare_digest(signature, self._sign(body)):
            return None
        try:
            payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        except (ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
            return None
        kind = payload.get("kind")
        if kind == "owner":
            if payload.get("sub") != self._settings.archive_username:
                return None
            return Viewer(kind="owner", username=payload.get("sub"))
        if kind == "account":
            username = payload.get("sub")
            user_id = payload.get("user_id")
            if not isinstance(username, str) or not isinstance(user_id, int):
                return None
            if self._store.user_by_username(username) is None:
                return None  # account deleted since the session was minted
            return Viewer(
                kind="account",
                username=username,
                user_id=user_id,
                grants=frozenset(self._store.user_grants(user_id)),
            )
        return None

    # ------------------------------------------------------------- login

    def login(self, ip: str | None, username: str, password: str) -> Viewer | None:
        """Authenticate an owner or account; None on bad credentials.

        Args:
            ip: Peer address, used for the failure rate limit.
            username: Login name.
            password: Plaintext password.

        Returns:
            The authenticated Viewer, or None for bad credentials.

        Raises:
            errors.TooManyRequests: When the IP is past the failure rate limit.
        """
        ip = ip or "unknown"
        with self._lock:
            if not self._rate_allowed(ip):
                raise TooManyRequests(
                    "too many login attempts, try again later"
                )
            if self._owner_matches(username, password):
                self._failures.pop(ip, None)
                return Viewer(kind="owner", username=username)
            user = self._store.user_by_username(username)
            if user is not None and _verify_password(password, user["password_hash"]):
                self._failures.pop(ip, None)
                return Viewer(
                    kind="account",
                    username=username,
                    user_id=user["id"],
                    grants=frozenset(self._store.user_grants(user["id"])),
                )
            self._failures.setdefault(ip, []).append(time.time())
            return None

    def _owner_matches(self, username: str, password: str) -> bool:
        """Constant-time owner credential check (disabled when unset)."""
        owner_user = self._settings.archive_username
        owner_pass = self._settings.archive_password
        if not owner_user or not owner_pass:
            return False
        return hmac.compare_digest(username, owner_user) and hmac.compare_digest(
            password, owner_pass
        )

    def _rate_allowed(self, ip: str) -> bool:
        """True if ``ip`` has fewer than the limit of failures in the window."""
        cutoff = time.time() - _RATE_WINDOW
        attempts = [t for t in self._failures.get(ip, []) if t > cutoff]
        self._failures[ip] = attempts
        return len(attempts) < self._settings.login_rate_limit

    # ------------------------------------------------------------- requests

    def csrf_token(self, request: Request) -> str:
        """A per-session CSRF token for the request's session cookie.

        Derived with the signing secret from the session token itself, so it is
        stateless (no server-side store) and bound to the exact session: a form
        token can never be replayed across sessions or forged without the
        secret. Rendered into every admin form as a hidden field.
        """
        token = request.cookies.get(COOKIE_NAME) or ""
        digest = hmac.new(self._secret, f"csrf:{token}".encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def verify_csrf(self, request: Request, form_token: str | None) -> bool:
        """True when ``form_token`` matches the request's session in constant time."""
        if not form_token:
            return False
        return hmac.compare_digest(form_token, self.csrf_token(request))

    def viewer_from_request(self, request: Request) -> Viewer:
        """Resolve the Viewer from the request's session cookie.

        Anonymous when the cookie is absent or invalid. Fails closed.
        """
        token = request.cookies.get(COOKIE_NAME)
        if token:
            viewer = self.verify_session(token)
            if viewer is not None:
                return viewer
        return Viewer(kind="anonymous")


def current_viewer(request: Request) -> Viewer:
    """FastAPI dependency: the :class:`Viewer` for the current request.

    Reads the session cookie; anonymous when absent or invalid. Add
    ``viewer: Viewer = Depends(current_viewer)`` to any protected route.
    """
    return request.app.state.auth.viewer_from_request(request)
