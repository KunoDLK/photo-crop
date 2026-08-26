"""Public-domain year calculation per policy zone.

The access rule for a page is "whitelist only": a page is shown in full when an
allow rule exists whose public-domain date has passed in the viewer's zone.
This module computes that date's year; :mod:`policy` turns it into a decision.

Which rule applies depends on the page's copyright kind (``page_rights.
copyright_kind``):

- ``editor`` (default): named editor(s) hold the copyright.
  - ``uk`` / ``eu``: the LAST editor's death year + 70 (life + 70).
  - ``us``: publication year <= 1929 is public domain (95-year rule).
- ``holder``: the rights holder / publisher owns the copyright (a fixed term
  from publication, not tied to any editor).
  - ``uk`` / ``eu``: publication year + 70 (works of unknown authorship).
  - ``us``: publication year + 95 (corporate works).
- ``ad``: an advertisement, not covered by the book's copyright notice;
  protected for 28 years from publication in every zone.

``unknown`` zones always fail closed.
"""
from __future__ import annotations

from typing import Any

from .geo import EU, UK, US

US_CUTOFF_YEAR = 1929


def pd_year(
    editors: list[dict[str, Any]] | None,
    kind: str,
    zone: str,
    publication_year: int | None = None,
) -> int | None:
    """The year copyright expires for a page, per its kind and policy zone.

    Args:
        editors: The page's editor rows (``death_year`` used); may be empty
            when the page has no per-page editor rule. For the ``editor`` kind
            in the UK/EU the LAST death year decides (life + 70).
        kind: The page's copyright kind: ``editor``, ``holder`` or ``ad``.
        zone: Policy zone (``us``/``uk``/``eu``/``unknown``).
        publication_year: The book's publication year (holder/ad terms, and
            the US 95-year rule).

    Returns:
        The year the page enters the public domain, or None when no rule
        grants access in this zone.
    """
    editors = editors or []
    if kind == "ad":
        # Advertisements were protected for 28 years from publication; the
        # book's copyright notice does not cover them.
        return publication_year + 28 if publication_year is not None else None
    if kind == "holder":
        # Rights-holder/publisher copyright: a fixed term from publication,
        # independent of any editor's lifespan.
        if zone in (UK, EU):
            return publication_year + 70 if publication_year is not None else None
        if zone == US:
            return publication_year + 95 if publication_year is not None else None
        return None
    # kind == "editor": named editor(s), life + 70 in the UK/EU.
    if zone in (UK, EU):
        deaths = [e["death_year"] for e in editors if e.get("death_year")]
        return max(deaths) + 70 if deaths else None
    if zone == US:
        if publication_year is not None and publication_year <= US_CUTOFF_YEAR:
            return publication_year + 95
        return None
    return None  # unknown zone and every other case: fail closed
