"""Page filename parsing and ordering.

Pages are named ``<group>_<order>-<name>.<ext>`` (e.g. ``2_001-Page.jpg``). The
order may carry a letter suffix for inserted pages (e.g. ``2_064A-Page.jpg``,
which sorts right after ``2_064`` because orders are zero-padded and compared
lexicographically). This module provides the single regex that defines that
convention, the image extension filter, and the ordering/cover rules derived
from it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: Recognized image extensions (case-insensitive).
IMAGE_EXT_RE = re.compile(r"\.(png|jpe?g|tiff?|bmp|webp|gif|avif)$", re.IGNORECASE)

#: ``<group>_<order>-<name>`` convention used across the archive. The order is
#: zero-padded digits with an optional trailing letter (``064``, ``064A``), so
#: plain string comparison keeps the archive sequence correct.
NAME_RE = re.compile(r"^(\d+)_(\d+[A-Za-z]*)-(.*)$")


@dataclass(frozen=True)
class PageName:
    """Parsed page identity.

    Attributes:
        group: Numeric grouping (e.g. front matter, chapter).
        order: Position within the group, zero-padded and optionally suffixed
            with a letter (``"064"``, ``"064A"``); sorts lexicographically.
        name: Free-form label shown in the viewer.
        extra: True when the filename did not match the convention.
    """

    group: int
    order: str
    name: str
    extra: bool = False


def is_image(filename: str) -> bool:
    """Return True if ``filename`` has a recognized image extension.

    Args:
        filename: Basename of a file in the archive.

    Returns:
        True when the file should be treated as an image page.
    """
    return bool(IMAGE_EXT_RE.search(filename))


def parse_name(filename: str) -> PageName | None:
    """Parse a page filename into its group/order/name parts.

    Args:
        filename: Basename to parse (extension stripped internally).

    Returns:
        A :class:`PageName` on a conventional name, else ``None``.
    """
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    noext = re.sub(r"\.[^.]+$", "", base)
    m = NAME_RE.match(noext)
    if not m:
        return None
    return PageName(
        group=int(m.group(1)),
        order=m.group(2),
        name=m.group(3) or noext,
        extra=False,
    )


def _order_key(order: str) -> tuple[int, str]:
    """Split an order string into its numeric prefix and the raw token.

    Orders are zero-padded and may carry a letter suffix (``"064"``,
    ``"064A"``). Comparing the numeric prefix first keeps unpadded names
    (``"1"``, ``"10"``) in sequence too; the raw token breaks ties so
    ``"064A"`` sorts right after ``"064"``.
    """
    m = re.match(r"(\d+)", order)
    return (int(m.group(1)) if m else 0, order)


def sort_pages(pages: list[dict]) -> list[dict]:
    """Sort page records by ``(group, order)`` ascending.

    Orders may carry a letter suffix (``"064"``, ``"064A"``) so a suffixed page
    sorts right after its base page; leading digits are compared numerically
    first, which also keeps unpadded names (``"1"``, ``"10"``) in sequence.

    Args:
        pages: Page records, each carrying at least ``group`` and ``order`` keys.

    Returns:
        A new list sorted stably by group then order.
    """
    return sorted(pages, key=lambda p: (p["group"], _order_key(p["order"])))


def cover_of(pages: list[dict]) -> dict | None:
    """Select the cover page: lowest ``(group, order)``.

    Args:
        pages: Already-sorted page records.

    Returns:
        The first page record, or ``None`` if the list is empty.
    """
    return pages[0] if pages else None
