"""Bulk-seed the rights database from CSV files.

Handy for importing a rights spreadsheet: editors, books, and per-page rules in
three CSVs. Editor and rights-holder names are matched case-insensitively and
created when missing; books and page rules are upserted, so re-running the
import over the same files is safe. Rows without a name are skipped.

CSV formats (first line = header):

    editors.csv:    name,birth_year,death_year,notes
    books.csv:      book_id,title,visibility,publication_year,editor_name,rights_holder_name
    pages.csv:      book_id,page_name,editor_name

Usage:
    python -m server.scripts.import_rights --db /archive/cache/rights.db \
        --editors editors.csv --books books.csv --pages pages.csv
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from ..config import Settings
from ..rights.store import RightsStore


def _read_rows(path: Path) -> list[dict]:
    """Read a CSV into dicts, skipping blank rows and comments."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return [row for row in csv.DictReader(fh) if any((row.get(k) or "").strip() for k in row)]


def _int(row: dict, key: str) -> int | None:
    value = (row.get(key) or "").strip()
    return int(value) if value else None


def _editor_id(store: RightsStore, cache: dict[str, int], name: str | None) -> int | None:
    """The editor id for a name, creating it on first sight (case-insensitive)."""
    name = (name or "").strip()
    if not name:
        return None
    key = name.casefold()
    if key in cache:
        return cache[key]
    for editor in store.list_editors():
        if editor["name"].casefold() == key:
            cache[key] = editor["id"]
            return editor["id"]
    editor_id = store.create_editor(name)
    cache[key] = editor_id
    return editor_id


def _holder_id(store: RightsStore, cache: dict[str, int], name: str | None) -> int | None:
    """The rights-holder id for a name, creating it on first sight."""
    name = (name or "").strip()
    if not name:
        return None
    key = name.casefold()
    if key in cache:
        return cache[key]
    for holder in store.list_rights_holders():
        if holder["name"].casefold() == key:
            cache[key] = holder["id"]
            return holder["id"]
    holder_id = store.create_rights_holder(name)
    cache[key] = holder_id
    return holder_id


def main() -> None:
    """Run the CSV import against the rights database."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=None, help="rights.db path (default: settings)")
    parser.add_argument("--editors", type=Path, help="editors CSV")
    parser.add_argument("--books", type=Path, help="books CSV")
    parser.add_argument("--pages", type=Path, help="per-page rules CSV")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else Settings().rights_db
    store = RightsStore(db_path)

    editor_cache: dict[str, int] = {}
    holder_cache: dict[str, int] = {}

    if args.editors and args.editors.is_file():
        for row in _read_rows(args.editors):
            store.create_editor(
                row["name"].strip(),
                birth_year=_int(row, "birth_year"),
                death_year=_int(row, "death_year"),
                notes=(row.get("notes") or "").strip() or None,
            )
        print(f"editors: imported {args.editors}")

    if args.books and args.books.is_file():
        for row in _read_rows(args.books):
            book_id = (row.get("book_id") or "").strip()
            if not book_id:
                continue
            visibility = (row.get("visibility") or "private").strip().lower()
            store.update_book(
                book_id,
                title=(row.get("title") or "").strip() or None,
                visibility=visibility if visibility in ("public", "private") else None,
                publication_year=_int(row, "publication_year"),
                editor_id=_editor_id(store, editor_cache, row.get("editor_name")),
                rights_holder_id=_holder_id(store, holder_cache, row.get("rights_holder_name")),
            )
        print(f"books: imported {args.books}")

    if args.pages and args.pages.is_file():
        for row in _read_rows(args.pages):
            book_id = (row.get("book_id") or "").strip()
            page_name = (row.get("page_name") or "").strip()
            if not book_id or not page_name:
                continue
            store.set_page_editor(
                book_id, page_name, _editor_id(store, editor_cache, row.get("editor_name"))
            )
        print(f"pages: imported {args.pages}")


if __name__ == "__main__":
    main()
