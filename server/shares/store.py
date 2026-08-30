"""Persistent share-key store (SQLite).

Share links are random 32-byte secrets handed out as capability tokens; the
server keeps the authoritative record — book, page, expiry, revocation state —
so links can be listed, extended, and invalidated from the admin pages. Keys
are stored hashed (SHA-256) so a leaked database cannot mint new links, and
the per-request verification path is a hash-then-row lookup with no writes.

The table lives in the same SQLite file as the rights database (WAL mode, one
connection + a lock), matching the RightsStore pattern.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from pathlib import Path

#: Durations (seconds) accepted for a minted share key; the client's share
#: panel offers these as its option row. Allowlisted so a stale or tampered
#: client can never mint a decades-long grant.
SHARE_DURATIONS = (3600, 86400, 604800, 2592000)  # 1 hour, 1 day, 7 days, 30 days

#: Cookie Max-Age for keys with no expiry (``expires_at`` NULL).
_NEVER_TTL = 10 * 365 * 86400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS share_keys (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash     TEXT NOT NULL UNIQUE,
    book         TEXT NOT NULL,
    page         TEXT,
    created_by   TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER,
    revoked_at   INTEGER,
    last_used_at INTEGER,
    note         TEXT
);
"""


def _hash(key: str) -> str:
    """SHA-256 hex of a share key (the only form stored at rest)."""
    return hashlib.sha256(key.encode()).hexdigest()


def share_cookie_name(key: str) -> str:
    """The per-key cookie name (``bv_share_<hash>``).

    One cookie per key so several share links can be held in the browser at
    once without overwriting each other; the name is deterministic so
    re-opening the same keyed URL refreshes the same cookie (and its Max-Age).
    """
    return f"bv_share_{_hash(key)[:16]}"


class ShareStore:
    """Thread-safe SQLite store for share keys."""

    def __init__(self, db_path: Path) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------ creation

    def create(
        self, book: str, page: str | None, ttl: int | None, created_by: str,
        note: str | None = None,
    ) -> str:
        """Mint a new share key for ``(book, page)`` and store it.

        Args:
            book: Book directory name to grant.
            page: Page filename to grant, or None for the whole book.
            ttl: Lifetime in seconds (expiry = now + ttl), or None for no
                expiry (``expires_at`` NULL).
            created_by: Username that minted the key (audit).
            note: Optional admin label.

        Returns:
            The raw 32-byte secret (base64url) to hand out; only its hash is
            stored.
        """
        key = secrets.token_urlsafe(32)
        now = int(time.time())
        expires_at = None if ttl is None else now + ttl
        with self._lock:
            self._conn.execute(
                "INSERT INTO share_keys"
                " (key_hash, book, page, created_by, created_at, expires_at, note)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_hash(key), book, page, created_by, now, expires_at, note),
            )
            self._conn.commit()
        return key

    # ---------------------------------------------------------- verification

    def lookup(self, key: str) -> dict | None:
        """The stored row for a key (None when unknown).

        Args:
            key: The raw share key from a URL query or cookie.

        Returns:
            A dict of the row's columns, or None.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM share_keys WHERE key_hash = ?", (_hash(key),)
            ).fetchone()
        return dict(row) if row is not None else None

    def resolve_grants(self, keys: list[str]) -> frozenset[tuple[str, str | None]]:
        """Valid ``(book, page)`` grants among ``keys`` — no writes.

        Filters out unknown, revoked, and expired keys. This is the per-request
        hot path (viewer resolution), so it deliberately does not touch
        ``last_used_at``.

        Args:
            keys: Raw share keys presented by the request (query + cookies).

        Returns:
            One ``(book, page)`` pair per valid key.
        """
        grants: set[tuple[str, str | None]] = set()
        now = int(time.time())
        for key in keys:
            row = self.lookup(key)
            if row is None or row["revoked_at"] is not None:
                continue
            exp = row["expires_at"]
            if exp is not None and exp <= now:
                continue
            grants.add((row["book"], row["page"]))
        return frozenset(grants)

    def ttl_of(self, key: str) -> int | None:
        """Remaining seconds of validity for a key, or None when invalid.

        Used by the SPA response to size the ``bv_share_*`` cookie's Max-Age
        to the key's own lifetime. Keys with no expiry get a long Max-Age.

        Args:
            key: The raw share key.

        Returns:
            Seconds until expiry (> 0), or None for unknown/revoked/expired.
        """
        row = self.lookup(key)
        if row is None or row["revoked_at"] is not None:
            return None
        exp = row["expires_at"]
        if exp is None:
            return _NEVER_TTL
        remaining = exp - int(time.time())
        return remaining if remaining > 0 else None

    def touch(self, key: str) -> None:
        """Record a use of a key (called by the /info endpoint only)."""
        with self._lock:
            self._conn.execute(
                "UPDATE share_keys SET last_used_at = ? WHERE key_hash = ?",
                (int(time.time()), _hash(key)),
            )
            self._conn.commit()

    # -------------------------------------------------------------- admin CRUD

    def list(self) -> list[dict]:
        """Every share key, newest first (for the admin manager)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM share_keys ORDER BY created_at DESC, id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get(self, share_id: int) -> dict | None:
        """The row for an admin id, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM share_keys WHERE id = ?", (share_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def set_expiry(self, share_id: int, expires_at: int | None) -> bool:
        """Set (or clear, with None) a key's expiry; False when unknown id."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE share_keys SET expires_at = ? WHERE id = ?",
                (expires_at, share_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def revoke(self, share_id: int) -> bool:
        """Invalidate a key immediately; False when unknown or already revoked."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE share_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (int(time.time()), share_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def restore(self, share_id: int) -> bool:
        """Un-revoke a key (reactivates it); False when unknown id."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE share_keys SET revoked_at = NULL WHERE id = ?", (share_id,)
            )
            self._conn.commit()
        return cur.rowcount > 0

    def delete(self, share_id: int) -> bool:
        """Hard-delete a key; False when unknown id."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM share_keys WHERE id = ?", (share_id,)
            )
            self._conn.commit()
        return cur.rowcount > 0
