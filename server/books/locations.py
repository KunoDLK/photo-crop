"""Persistent short-ID registry for shareable locations.

A "location" is a (book, page) pair. Each location is assigned a short base62 ID
so the viewer's share URL can stay compact (like ``/93050a0``) instead of encoding
the full book/page names. IDs are derived from the location key; on a collision
the value is incremented until an unused ID is found. The mapping is persisted so
IDs stay stable across restarts.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _b62(n: int) -> str:
    """Encode a non-negative integer as a base62 string."""
    if n == 0:
        return ALPHABET[0]
    out = ""
    while n:
        n, r = divmod(n, 62)
        out = ALPHABET[r] + out
    return out


class LocationRegistry:
    """Thread-safe id <-> (book, page) mapping backed by a JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._by_id: dict[str, str] = {}   # id -> "book\0page"
        self._by_key: dict[str, str] = {}  # "book\0page" -> id
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        try:
            if self._path.is_file():
                data = json.loads(self._path.read_text())
                for ident, key in (data.get("ids") or {}).items():
                    self._by_id[ident] = key
                    self._by_key[key] = ident
        except Exception:
            self._by_id = {}
            self._by_key = {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps({"ids": self._by_id}))
        except Exception:
            pass

    @staticmethod
    def _key(book: str, page: str | None) -> str:
        return (book or "") + "\u0000" + (page or "")

    def resolve(self, ident: str) -> dict | None:
        """Map an id back to ``{book, page}`` (page may be None), or None."""
        key = self._by_id.get(ident)
        if key is None:
            return None
        book, page = key.split("\u0000", 1)
        return {"book": book, "page": page or None}

    def get_id(self, book: str, page: str | None) -> str:
        """Return (creating if needed) the short id for a location.

        The id starts as a hash-derived base62 value; if that value is already
        taken by a different location it is incremented until free.
        """
        key = self._key(book, page)
        with self._lock:
            if key in self._by_key:
                return self._by_key[key]
            val = int.from_bytes(hashlib.sha256(key.encode()).digest()[:5], "big")
            while True:
                ident = _b62(val)
                if ident not in self._by_id or self._by_id[ident] == key:
                    break
                val += 1
            self._by_id[ident] = key
            self._by_key[key] = ident
            self._save()
            return ident
