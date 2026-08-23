"""Page filename parsing and ordering.

Pages are named ``<group>_<order>-<name>.<ext>`` (e.g. ``2_001-Page.jpg``). This
module provides the single regex that defines that convention, the image
extension filter, and the ordering/cover rules derived from it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: Recognized image extensions (case-insensitive).
IMAGE_EXT_RE = re.compile(r"\.(png|jpe?g|tiff?|bmp|webp|gif|avif)$", re.IGNORECASE)

#: ``<group>_<order>-<name>`` convention used across the archive.
NAME_RE = re.compile(r"^(\d+)_(\d+)-(.*)$")


@dataclass(frozen=True)
class PageName:
    """Parsed page identity.

    Attributes:
        group: Numeric grouping (e.g. front matter, chapter).
        order: Position within the group.
        name: Free-form label shown in the viewer.
        extra: True when the filename did not match the convention.
    """

    group: int
    order: int
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
        order=int(m.group(2)),
        name=m.group(3) or noext,
        extra=False,
    )


def sort_pages(pages: list[dict]) -> list[dict]:
    """Sort page records by ``(group, order)`` ascending.

    Args:
        pages: Page records, each carrying at least ``group`` and ``order`` keys.

    Returns:
        A new list sorted stably by group then order.
    """
    return sorted(pages, key=lambda p: (p["group"], p["order"]))


def cover_of(pages: list[dict]) -> dict | None:
    """Select the cover page: lowest ``(group, order)``.

    Args:
        pages: Already-sorted page records.

    Returns:
        The first page record, or ``None`` if the list is empty.
    """
    return pages[0] if pages else None
