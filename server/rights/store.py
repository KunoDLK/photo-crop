"""Persistent rights database (SQLite).

Stores editors, rights holders, book visibility, per-page allow rules, viewer
accounts and per-book grants in ``cache_dir/rights.db``. The full schema is
created on first boot so the file exists once; phase 1 implements the user and
grant operations the auth layer needs, while book/editor access methods arrive
with the policy work (later phases). All operations are serialized behind a
single lock and connection, which is plenty for the tiny admin/read traffic
this database sees.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS editors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    birth_year INTEGER,
    death_year INTEGER,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS rights_holders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'unknown',
    contact TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS books (
    id TEXT PRIMARY KEY,
    title TEXT,
    visibility TEXT NOT NULL DEFAULT 'private'
        CHECK (visibility IN ('public', 'private')),
    editor_id INTEGER REFERENCES editors(id) ON DELETE SET NULL,
    rights_holder_id INTEGER REFERENCES rights_holders(id) ON DELETE SET NULL,
    publication_year INTEGER,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS page_rights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    page_name TEXT,
    editor_id INTEGER REFERENCES editors(id) ON DELETE SET NULL,
    copyright_kind TEXT NOT NULL DEFAULT 'editor'
        CHECK (copyright_kind IN ('editor', 'holder', 'ad')),
    default_access TEXT NOT NULL DEFAULT 'block'
        CHECK (default_access IN ('block', 'public')),
    notes TEXT,
    UNIQUE (book_id, page_name)
);

CREATE TABLE IF NOT EXISTS page_editors (
    page_rights_id INTEGER NOT NULL REFERENCES page_rights(id) ON DELETE CASCADE,
    editor_id INTEGER NOT NULL REFERENCES editors(id) ON DELETE CASCADE,
    PRIMARY KEY (page_rights_id, editor_id)
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_grants (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    book_id TEXT NOT NULL,
    PRIMARY KEY (user_id, book_id)
);
"""


class RightsStore:
    """Thread-safe wrapper around the rights SQLite database."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._db = self._connect(path)

    def _connect(self, path: Path) -> sqlite3.Connection:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(path), check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(_SCHEMA)
        # Migrations: CREATE IF NOT EXISTS leaves pre-existing databases without
        # the newer columns/tables, so add them explicitly (safe defaults for
        # old rows) and fold legacy single-editor rows into the join table.
        pr_cols = {row[1] for row in db.execute("PRAGMA table_info(page_rights)")}
        if "default_access" not in pr_cols:
            db.execute(
                "ALTER TABLE page_rights ADD COLUMN default_access TEXT NOT NULL DEFAULT 'block'"
            )
        if "copyright_kind" not in pr_cols:
            db.execute(
                "ALTER TABLE page_rights ADD COLUMN copyright_kind TEXT NOT NULL DEFAULT 'editor'"
            )
        db.executescript(
            """CREATE TABLE IF NOT EXISTS page_editors (
                   page_rights_id INTEGER NOT NULL REFERENCES page_rights(id) ON DELETE CASCADE,
                   editor_id INTEGER NOT NULL REFERENCES editors(id) ON DELETE CASCADE,
                   PRIMARY KEY (page_rights_id, editor_id)
               );
               INSERT OR IGNORE INTO page_editors (page_rights_id, editor_id)
                   SELECT id, editor_id FROM page_rights WHERE editor_id IS NOT NULL;"""
        )
        db.commit()
        return db

    # ------------------------------------------------------------- users

    def user_by_username(self, username: str) -> dict | None:
        """Return the user row (with ``id``/``username``/``password_hash``) or None."""
        with self._lock:
            row = self._db.execute(
                "SELECT id, username, password_hash FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        return dict(row) if row else None

    def create_user(self, username: str, password_hash: str) -> int:
        """Create a user and return its id.

        Raises:
            ValueError: If the username is already taken.
        """
        with self._lock:
            try:
                cur = self._db.execute(
                    "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                    (username, password_hash),
                )
                self._db.commit()
            except sqlite3.IntegrityError as e:
                raise ValueError(f"username already exists: {username}") from e
            return int(cur.lastrowid)

    def set_user_password(self, user_id: int, password_hash: str) -> None:
        """Replace a user's password hash."""
        with self._lock:
            self._db.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (password_hash, user_id),
            )
            self._db.commit()

    # ------------------------------------------------------------- grants

    def user_grants(self, user_id: int) -> set[str]:
        """Return the set of book ids granted to a user."""
        with self._lock:
            rows = self._db.execute(
                "SELECT book_id FROM user_grants WHERE user_id = ?", (user_id,)
            ).fetchall()
        return {row["book_id"] for row in rows}

    def grant_book(self, user_id: int, book_id: str) -> None:
        """Grant a user full access to a book (idempotent)."""
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO user_grants (user_id, book_id) VALUES (?, ?)",
                (user_id, book_id),
            )
            self._db.commit()

    def revoke_book(self, user_id: int, book_id: str) -> None:
        """Revoke a user's grant to a book (idempotent)."""
        with self._lock:
            self._db.execute(
                "DELETE FROM user_grants WHERE user_id = ? AND book_id = ?",
                (user_id, book_id),
            )
            self._db.commit()

    def set_user_grants(self, user_id: int, book_ids: set[str]) -> None:
        """Replace a user's grants with exactly ``book_ids``."""
        with self._lock:
            self._db.execute(
                "DELETE FROM user_grants WHERE user_id = ?", (user_id,)
            )
            self._db.executemany(
                "INSERT INTO user_grants (user_id, book_id) VALUES (?, ?)",
                [(user_id, b) for b in book_ids],
            )
            self._db.commit()

    # ------------------------------------------------------------- books

    def book_row(self, book_id: str) -> dict | None:
        """Return the full books row for a book id, or None when unconfigured."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM books WHERE id = ?", (book_id,)
            ).fetchone()
        return dict(row) if row else None

    def book_visibility(self, book_id: str) -> str:
        """The book's visibility: ``public``, or ``private`` when unconfigured."""
        row = self.book_row(book_id)
        return row["visibility"] if row else "private"

    def set_book_visibility(self, book_id: str, visibility: str) -> None:
        """Mark a book public or private (creating its row as needed)."""
        with self._lock:
            self._db.execute(
                """INSERT INTO books (id, visibility) VALUES (?, ?)
                   ON CONFLICT(id) DO UPDATE SET visibility = excluded.visibility""",
                (book_id, visibility),
            )
            self._db.commit()

    def update_book(self, book_id: str, **fields) -> None:
        """Update the given book fields (title/editor/rights/publication/notes).

        Only keys in the books table's column whitelist are applied; None values
        are ignored so partial updates don't wipe data. Creates the row if the
        book has no row yet (admin creates entries before configuring pages).
        """
        allowed = {
            "title", "visibility", "editor_id", "rights_holder_id",
            "publication_year", "notes",
        }
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        with self._lock:
            existing = self._db.execute(
                "SELECT 1 FROM books WHERE id = ?", (book_id,)
            ).fetchone()
            if existing is None:
                self._db.execute(
                    "INSERT INTO books (id) VALUES (?)", (book_id,)
                )
            if updates:
                cols = ", ".join(f"{k} = ?" for k in updates)
                self._db.execute(
                    f"UPDATE books SET {cols} WHERE id = ?",
                    (*updates.values(), book_id),
                )
            self._db.commit()

    # ------------------------------------------------------------- editors

    def get_editor(self, editor_id: int) -> dict | None:
        """Return the editor row for an id, or None."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM editors WHERE id = ?", (editor_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_editors(self) -> list[dict]:
        """All editors ordered by name."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM editors ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [dict(r) for r in rows]

    def create_editor(
        self, name: str, birth_year: int | None = None,
        death_year: int | None = None, notes: str | None = None,
    ) -> int:
        """Create an editor and return its id.

        Raises:
            ValueError: If the name is empty.
        """
        if not name.strip():
            raise ValueError("editor name is required")
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO editors (name, birth_year, death_year, notes) VALUES (?,?,?,?)",
                (name.strip(), birth_year, death_year, notes),
            )
            self._db.commit()
        return int(cur.lastrowid)

    def update_editor(self, editor_id: int, **fields) -> None:
        """Update editor fields (birth/death year, notes, name). None ignored."""
        allowed = {"name", "birth_year", "death_year", "notes"}
        self._update("editors", editor_id, fields, allowed)

    def delete_editor(self, editor_id: int) -> None:
        """Delete an editor; book/page references are set to NULL by the FK."""
        with self._lock:
            self._db.execute("DELETE FROM editors WHERE id = ?", (editor_id,))
            self._db.commit()

    # ------------------------------------------------------ rights holders

    def list_rights_holders(self) -> list[dict]:
        """All rights holders ordered by name."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM rights_holders ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [dict(r) for r in rows]

    def create_rights_holder(
        self, name: str, kind: str = "unknown",
        contact: str | None = None, notes: str | None = None,
    ) -> int:
        """Create a rights holder and return its id.

        Raises:
            ValueError: If the name is empty.
        """
        if not name.strip():
            raise ValueError("rights holder name is required")
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO rights_holders (name, kind, contact, notes) VALUES (?,?,?,?)",
                (name.strip(), kind, contact, notes),
            )
            self._db.commit()
        return int(cur.lastrowid)

    def update_rights_holder(self, holder_id: int, **fields) -> None:
        """Update rights-holder fields (name/kind/contact/notes). None ignored."""
        allowed = {"name", "kind", "contact", "notes"}
        self._update("rights_holders", holder_id, fields, allowed)

    def delete_rights_holder(self, holder_id: int) -> None:
        """Delete a rights holder; book references are set to NULL by the FK."""
        with self._lock:
            self._db.execute(
                "DELETE FROM rights_holders WHERE id = ?", (holder_id,)
            )
            self._db.commit()

    # ------------------------------------------------------------- users

    def list_users(self) -> list[dict]:
        """All accounts with id/username/created, ordered by username."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, username, created FROM users ORDER BY username COLLATE NOCASE"
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_user(self, user_id: int) -> None:
        """Delete an account; its grants cascade via the FK."""
        with self._lock:
            self._db.execute("DELETE FROM users WHERE id = ?", (user_id,))
            self._db.commit()

    # ------------------------------------------------------------- helpers

    def _update(self, table: str, row_id: int, fields: dict, allowed: set[str]) -> None:
        """Apply a whitelisted field update to one row (None values ignored)."""
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return
        cols = ", ".join(f"{k} = ?" for k in updates)
        with self._lock:
            self._db.execute(
                f"UPDATE {table} SET {cols} WHERE id = ?", (*updates.values(), row_id)
            )
            self._db.commit()

    # ------------------------------------------------------------- page rules

    def page_editors_map(self, book_id: str) -> dict[str, list[dict]]:
        """Map every page of a book to its per-page editors (many-to-many).

        Only rows with at least one editor and a concrete page name count (the
        book-level default editor lives in the books row). Used by the policy
        layer to resolve whole books without one query per page.

        Returns:
            ``{page_name: [editor row, ...]}``, editors ordered by name.
        """
        cols = ("id", "name", "birth_year", "death_year", "notes")
        with self._lock:
            rows = self._db.execute(
                """SELECT pr.page_name, e.id, e.name, e.birth_year, e.death_year, e.notes
                   FROM page_rights pr
                   JOIN page_editors pe ON pe.page_rights_id = pr.id
                   JOIN editors e ON e.id = pe.editor_id
                   WHERE pr.book_id = ? AND pr.page_name IS NOT NULL
                   ORDER BY e.name COLLATE NOCASE""",
                (book_id,),
            ).fetchall()
        out: dict[str, list[dict]] = {}
        for row in rows:
            out.setdefault(row["page_name"], []).append({c: row[c] for c in cols})
        return out

    def set_page_editors(
        self, book_id: str, page_name: str, editor_ids: list[int]
    ) -> None:
        """Replace a page's editor set (empty list clears the per-page rule).

        The legacy single ``editor_id`` column is kept in sync with the first
        editor so older readers and the CSV import keep working.

        Args:
            book_id: The book directory name.
            page_name: The page filename.
            editor_ids: Editor ids to attach; empty clears the override.
        """
        with self._lock:
            self._db.execute(
                "INSERT INTO books (id) VALUES (?) ON CONFLICT(id) DO NOTHING",
                (book_id,),
            )
            self._db.execute(
                """INSERT INTO page_rights (book_id, page_name) VALUES (?, ?)
                   ON CONFLICT(book_id, page_name) DO NOTHING""",
                (book_id, page_name),
            )
            pr_id = self._db.execute(
                "SELECT id FROM page_rights WHERE book_id = ? AND page_name = ?",
                (book_id, page_name),
            ).fetchone()["id"]
            self._db.execute(
                "DELETE FROM page_editors WHERE page_rights_id = ?", (pr_id,)
            )
            self._db.executemany(
                "INSERT INTO page_editors (page_rights_id, editor_id) VALUES (?, ?)",
                [(pr_id, eid) for eid in editor_ids],
            )
            self._db.execute(
                "UPDATE page_rights SET editor_id = ? WHERE id = ?",
                (editor_ids[0] if editor_ids else None, pr_id),
            )
            self._db.commit()

    def page_kinds_map(self, book_id: str) -> dict[str, str]:
        """Map every page of a book to its copyright kind (``editor`` default).

        Returns:
            ``{page_name: kind}`` where kind is ``editor``, ``holder`` or
            ``ad``; pages without a row default to ``editor``.
        """
        with self._lock:
            rows = self._db.execute(
                """SELECT page_name, copyright_kind FROM page_rights
                   WHERE book_id = ? AND page_name IS NOT NULL""",
                (book_id,),
            ).fetchall()
        return {row["page_name"]: row["copyright_kind"] for row in rows}

    def set_page_copyright_kind(
        self, book_id: str, page_name: str, kind: str
    ) -> None:
        """Set which copyright a page falls under.

        Args:
            book_id: The book directory name.
            page_name: The page filename.
            kind: ``editor`` (named editor(s), life + 70 in the UK/EU),
                ``holder`` (rights-holder/publisher copyright, fixed term from
                publication) or ``ad`` (advertisement, 28-year protection).

        Raises:
            ValueError: If ``kind`` is not one of the known values.
        """
        if kind not in ("editor", "holder", "ad"):
            raise ValueError(f"invalid copyright kind: {kind}")
        with self._lock:
            self._db.execute(
                "INSERT INTO books (id) VALUES (?) ON CONFLICT(id) DO NOTHING",
                (book_id,),
            )
            self._db.execute(
                """INSERT INTO page_rights (book_id, page_name, copyright_kind)
                   VALUES (?, ?, ?)
                   ON CONFLICT(book_id, page_name)
                   DO UPDATE SET copyright_kind = excluded.copyright_kind""",
                (book_id, page_name, kind),
            )
            self._db.commit()

    def set_page_editor(
        self, book_id: str, page_name: str | None, editor_id: int | None
    ) -> None:
        """Set (or clear) an editor rule for a page or for the whole book.

        For a specific page this replaces the page's editor set with exactly
        the single editor (or none) — kept for the bulk/default forms and the
        CSV import; the per-page screen uses :meth:`set_page_editors`.

        Args:
            book_id: The book directory name.
            page_name: The page filename, or None for the book-level default.
            editor_id: The editor id, or None to clear the rule.
        """
        if page_name is None:
            with self._lock:
                self._db.execute(
                    """INSERT INTO books (id, editor_id) VALUES (?, ?)
                       ON CONFLICT(id) DO UPDATE SET editor_id = excluded.editor_id""",
                    (book_id, editor_id),
                )
                self._db.commit()
            return
        self.set_page_editors(book_id, page_name, [editor_id] if editor_id else [])

    def page_defaults_map(self, book_id: str) -> dict[str, str]:
        """Map page names whose default access is ``public`` (block is default).

        Rows with ``default_access = 'public'`` mean the page is openly viewable
        by everyone unless an editor rule governs it; everything else falls back
        to ``block`` (fail closed).
        """
        with self._lock:
            rows = self._db.execute(
                """SELECT page_name FROM page_rights
                   WHERE book_id = ? AND page_name IS NOT NULL
                     AND default_access = 'public'""",
                (book_id,),
            ).fetchall()
        return {row["page_name"]: "public" for row in rows}

    def set_page_default(
        self, book_id: str, page_name: str | None, default_access: str
    ) -> None:
        """Set a page's (or the book's) default access: ``block`` or ``public``.

        The upsert preserves any editor rule already attached to the page.

        Args:
            book_id: The book directory name.
            page_name: The page filename, or None for the book-level default.
            default_access: ``block`` (fail closed) or ``public`` (open, with no
                editor rule governing it — the owner's own images).
        """
        if default_access not in ("block", "public"):
            raise ValueError(f"invalid default_access: {default_access}")
        if page_name is None:
            raise ValueError("default access is per page; pass a page name")
        with self._lock:
            self._db.execute(
                "INSERT INTO books (id) VALUES (?) ON CONFLICT(id) DO NOTHING",
                (book_id,),
            )
            self._db.execute(
                """INSERT INTO page_rights (book_id, page_name, default_access)
                   VALUES (?, ?, ?)
                   ON CONFLICT(book_id, page_name)
                   DO UPDATE SET default_access = excluded.default_access""",
                (book_id, page_name, default_access),
            )
            self._db.commit()
