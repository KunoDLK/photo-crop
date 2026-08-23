"""Archive scanning: enumerate books and pages.

The single place that knows the archive layout (one subfolder per book, images as
pages). Provides safe path resolution (no traversal outside the archive root) and
assembles the listing models consumed by the router.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from ..errors import BadRequest, NotFound
from ..models import BookSummary, CoverInfo, PageInfo
from ..tiles.geometry import max_level
from . import dimensions, naming


#: Cache of (size, mtime_ns) -> content hash, so re-requests don't re-read files.
_hash_cache: dict[Path, tuple[int, int, str]] = {}


def _content_hash(path: Path) -> str:
    """Return a stable ``sha256:<hex>`` digest of a file's bytes.

    Cached per path keyed on (size, mtime_ns), so a re-scanned page (same path,
    new bytes) is re-hashed on the next request.
    """
    stat = path.stat()
    cached = _hash_cache.get(path)
    if cached is not None and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
        return cached[2]
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    value = "sha256:" + digest.hexdigest()
    _hash_cache[path] = (stat.st_size, stat.st_mtime_ns, value)
    return value


def book_dir(archive_root: Path, book_id: str) -> Path:
    """Resolve a book id to a directory, refusing path traversal.

    Args:
        archive_root: The library root.
        book_id: URL-decoded book identifier (a directory name).

    Returns:
        The resolved directory path, guaranteed to sit inside ``archive_root``.

    Raises:
        errors.NotFound: If the id escapes the root or is not a directory.
    """
    root = archive_root.resolve()
    if not root.is_dir():
        raise NotFound(f"archive root missing: {root}")
    candidate = (root / book_id).resolve()
    if not candidate.is_relative_to(root) or candidate == root or not candidate.is_dir():
        raise NotFound(f"book not found: {book_id}")
    return candidate


def page_path(archive_root: Path, book_id: str, page_id: str) -> Path:
    """Resolve a page id to a file inside a book, refusing traversal.

    Args:
        archive_root: The library root.
        book_id: The book directory name.
        page_id: The page filename (a plain basename).

    Returns:
        The resolved file path.

    Raises:
        errors.BadRequest: If ``page_id`` is not a plain basename.
        errors.NotFound: If the page is missing or not a regular file.
    """
    if page_id != Path(page_id).name or page_id in ("", ".", ".."):
        raise BadRequest(f"invalid page id: {page_id}")
    bdir = book_dir(archive_root, book_id)
    candidate = (bdir / page_id).resolve()
    if candidate.parent != bdir or not candidate.is_file():
        raise NotFound(f"page not found: {page_id}")
    return candidate


def _scan_pages(book_dir: Path, tile_size: int) -> list[PageInfo]:
    """Scan one book directory into a sorted list of :class:`PageInfo`.

    Non-conventional filenames form a trailing "extra" group ordered by name.
    """
    matched: list[dict] = []
    extra: list[dict] = []

    for entry in sorted(book_dir.iterdir()):
        if not entry.is_file() or not naming.is_image(entry.name):
            continue
        try:
            w, h = dimensions.image_dims(entry)
        except (NotFound, BadRequest):
            continue
        record = {
            "page_id": entry.name,
            "name": entry.name,
            "group": 0,
            "order": 0,
            "width": w,
            "height": h,
            "max_level": max_level(w, h, tile_size),
        }
        parsed = naming.parse_name(entry.name)
        if parsed is not None:
            record["group"] = parsed.group
            record["order"] = parsed.order
            record["name"] = parsed.name
            matched.append(record)
        else:
            extra.append(record)

    matched = naming.sort_pages(matched)
    if extra:
        extra.sort(key=lambda r: r["name"].lower())
        max_group = matched[-1]["group"] if matched else 0
        for i, record in enumerate(extra, start=1):
            record["group"] = max_group + 1
            record["order"] = i

    return [
        PageInfo(
            page_id=r["page_id"],
            name=r["name"],
            group=r["group"],
            order=r["order"],
            width=r["width"],
            height=r["height"],
            max_level=r["max_level"],
        )
        for r in matched + extra
    ]


def list_books(archive_root: Path, tile_size: int) -> list[BookSummary]:
    """Enumerate books (immediate subdirectories) with their cover metadata.

    Args:
        archive_root: The library root.
        tile_size: Tile edge length, used to compute each cover's ``max_level``.

    Returns:
        Book summaries sorted by folder name.
    """
    if not archive_root.is_dir():
        raise NotFound(f"archive root missing: {archive_root}")

    books: list[BookSummary] = []
    for entry in sorted(archive_root.iterdir()):
        if not entry.is_dir():
            continue
        pages = _scan_pages(entry, tile_size)
        if not pages:
            continue
        cover = pages[0]
        books.append(
            BookSummary(
                id=entry.name,
                name=entry.name,
                cover=CoverInfo(
                    page_id=cover.page_id,
                    width=cover.width,
                    height=cover.height,
                    max_level=cover.max_level,
                ),
            )
        )
    return books


def list_pages(archive_root: Path, book_id: str, tile_size: int) -> list[PageInfo]:
    """Enumerate pages within a book, sorted by ``(group, order)``.

    Args:
        archive_root: The library root.
        book_id: The book directory name.
        tile_size: Tile edge length, used to compute each page's ``max_level``.

    Returns:
        Page records sorted for display; non-conventional names form a trailing
        group.
    """
    return _scan_pages(book_dir(archive_root, book_id), tile_size)


def image_info(archive_root: Path, book_id: str, page_id: str, tile_size: int) -> dict:
    """Return detailed metadata (dims, size, content hash) for one image.

    Args:
        archive_root: The library root.
        book_id: The book directory name.
        page_id: The page filename.
        tile_size: Tile edge length, used to compute ``max_level``.

    Returns:
        A dict with ``page_id``, ``width``, ``height``, ``max_level``,
        ``file_size`` and ``hash``.
    """
    path = page_path(archive_root, book_id, page_id)
    width, height = dimensions.image_dims(path)
    return {
        "page_id": page_id,
        "width": width,
        "height": height,
        "max_level": max_level(width, height, tile_size),
        "file_size": path.stat().st_size,
        "hash": _content_hash(path),
    }
