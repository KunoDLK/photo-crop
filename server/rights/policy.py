"""Access policy: resolves a (viewer, book, page, zone, date) to a decision.

Two orthogonal axes, both fail-closed:

- Book visibility: a ``private`` book does not exist to anyone without a grant
  (owner, or an account granted the book) — no tiles, OCR, previews, nothing.
- Page access: whitelist only — a page of a public book is ``full`` only when
  an allow rule exists whose public-domain date has passed in the viewer's
  zone; otherwise ``blurred`` (with the PD date when one is known).

The decision is recomputed from the current date on every request, so pages
flip to ``full`` automatically on their PD date with no manual step.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from ..auth.service import Viewer
from . import rules
from .store import RightsStore

FULL = "full"
BLURRED = "blurred"
NONEXISTENT = "nonexistent"


class Policy:
    """Resolves access decisions against the rights store."""

    def __init__(self, store: RightsStore) -> None:
        self._store = store

    def resolve(
        self,
        viewer: Viewer,
        book_id: str,
        page_id: str,
        zone: str,
        today: date | None = None,
    ) -> dict[str, Any]:
        """Resolve access for one page.

        Args:
            viewer: The requester's identity (owner/account/anonymous).
            book_id: The book directory name.
            page_id: The page filename.
            zone: The viewer's policy zone (``us``/``uk``/``eu``/``unknown``).
            today: The reference date (defaults to today).

        Returns:
            ``{"status": "full"|"blurred"|"nonexistent", "zone": ...}`` with an
            optional ``"until": "1 Jan YYYY"`` on blurred pages that have a
            known public-domain date.
        """
        return self.resolve_pages(viewer, book_id, [page_id], zone, today)[page_id]

    def resolve_pages(
        self,
        viewer: Viewer,
        book_id: str,
        page_ids: list[str],
        zone: str,
        today: date | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Resolve access for many pages of one book in a single DB pass.

        Args:
            viewer: The requester's identity.
            book_id: The book directory name.
            page_ids: Page filenames to resolve.
            zone: The viewer's policy zone.
            today: The reference date (defaults to today).

        Returns:
            One access dict per page id, in input order. Each carries
            ``region_locked``: True unless the page resolves ``full`` for an
            anonymous viewer in *every* zone — the only case where a cached
            real tile is safe to serve to anyone.
        """
        today = today or date.today()
        book_row = self._store.book_row(book_id)
        editors_map = self._store.page_editors_map(book_id)
        kinds_map = self._store.page_kinds_map(book_id)
        defaults_map = self._store.page_defaults_map(book_id)
        out = {}
        for pid in page_ids:
            access = self._resolve_page(
                viewer, book_id, book_row, editors_map, kinds_map,
                defaults_map, pid, zone, today,
            )
            access["region_locked"] = not self._public_full(
                book_row, editors_map, kinds_map, defaults_map, pid, today
            )
            out[pid] = access
        return out

    def _public_full(
        self,
        book_row: dict[str, Any] | None,
        editors_map: dict[str, list[dict[str, Any]]],
        kinds_map: dict[str, str],
        defaults_map: dict[str, str],
        page_id: str,
        today: date,
    ) -> bool:
        """True when the page is ``full`` for an anonymous viewer in every zone.

        Only then is the real tile's cached response safe to serve to any
        future requester: everyone is entitled to the same bytes. Any zone
        where anonymous is not ``full`` (region rules pending, private book,
        ``block`` default, or the fail-closed ``unknown`` zone) means the real
        bytes must not be shared-cached.
        """
        anon = Viewer(kind="anonymous")
        return all(
            self._resolve_page(anon, "", book_row, editors_map, kinds_map,
                               defaults_map, page_id, zone, today)["status"]
            == FULL
            for zone in ("us", "uk", "eu", "unknown")
        )

    def _resolve_page(
        self,
        viewer: Viewer,
        book_id: str,
        book_row: dict[str, Any] | None,
        editors_map: dict[str, list[dict[str, Any]]],
        kinds_map: dict[str, str],
        defaults_map: dict[str, str],
        page_id: str,
        zone: str,
        today: date,
    ) -> dict[str, Any]:
        # Owner always wins; a granted account wins on its granted books.
        if viewer.kind == "owner":
            return {"status": FULL, "zone": zone}
        if viewer.kind == "account" and book_id in viewer.grants:
            return {"status": FULL, "zone": zone}
        # Private (or unknown) books do not exist without a grant.
        if book_row is None or book_row["visibility"] != "public":
            return {"status": NONEXISTENT, "zone": zone}
        # Public book: a governing rule takes precedence and applies the
        # region/date rules. The rule is the page's own copyright kind (its
        # per-page editors for ``editor`` pages, or the fixed ad/holder term)
        # or, absent any per-page setup, the book's default editor.
        kind = kinds_map.get(page_id, "editor")
        editors = editors_map.get(page_id, [])
        if not editors and kind == "editor" and book_row.get("editor_id") is not None:
            rule_editor = self._store.get_editor(book_row["editor_id"])
            editors = [rule_editor] if rule_editor else []
        if editors or kind != "editor":
            year = rules.pd_year(editors, kind, zone, book_row.get("publication_year"))
            if year is not None and year <= today.year:
                return {"status": FULL, "zone": zone}
            result: dict[str, Any] = {"status": BLURRED, "zone": zone}
            if year is not None:
                result["until"] = f"1 Jan {year}"
            return result
        # No governing rule: the page's own default access decides. ``public``
        # (the owner's own images) is open everywhere; ``block`` fails closed.
        if defaults_map.get(page_id) == "public":
            return {"status": FULL, "zone": zone}
        return {"status": BLURRED, "zone": zone}
