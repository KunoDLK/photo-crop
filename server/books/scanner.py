"""Archive scanning: enumerate books and pages.

The single place that knows the archive layout (one subfolder per book, images as
pages). Provides safe path resolution (no traversal outside the archive root),
content hashing, and a TTL-cached catalog so listings are only re-scanned when
stale (every 10 minutes) or when a reload forces a fresh check.
"""
from __future__ import annotations

import hashlib
import threading
import time
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


def _sig(parts: list[str]) -> str:
    """A deterministic digest over a set of change-signature strings."""
    return hashlib.sha256("\n".join(sorted(parts)).encode()).hexdigest()


def _file_sig(entry: Path) -> str:
    stat = entry.stat()
    return f"{entry.name}:{stat.st_size}:{stat.st_mtime_ns}"


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


def _scan_pages(book_dir: Path, tile_size: int) -> tuple[str, list[PageInfo]]:
    """Scan one book directory into ``(signature, sorted pages)``.

    Non-conventional filenames form a trailing "extra" group ordered by name.
    The signature changes when a page is added, removed, or re-saved.
    """
    matched: list[dict] = []
    extra: list[dict] = []
    sig_parts: list[str] = []

    for entry in sorted(book_dir.iterdir()):
        if not entry.is_file() or not naming.is_image(entry.name):
            continue
        try:
            stat = entry.stat()
            w, h = dimensions.image_dims(entry)
        except (NotFound, BadRequest, OSError):
            continue
        sig_parts.append(_file_sig(entry))
        record = {
            "page_id": entry.name,
            "name": entry.name,
            "group": 0,
            "order": "",
            "width": w,
            "height": h,
            "max_level": max_level(w, h, tile_size),
            "_size": stat.st_size,
            "_mtime": stat.st_mtime_ns,
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
            record["order"] = str(i)

    pages = [
        PageInfo(
            page_id=r["page_id"],
            name=r["name"],
            group=r["group"],
            order=r["order"],
            width=r["width"],
            height=r["height"],
            max_level=r["max_level"],
            mtime=r["_mtime"],
        )
        for r in matched + extra
    ]
    return _sig(sig_parts), pages


def _scan_books(archive_root: Path, tile_size: int) -> tuple[str, list[BookSummary]]:
    """Scan the library root into ``(signature, sorted book summaries)``."""
    if not archive_root.is_dir():
        raise NotFound(f"archive root missing: {archive_root}")

    books: list[BookSummary] = []
    sig_parts: list[str] = []
    for entry in sorted(archive_root.iterdir()):
        if not entry.is_dir():
            continue
        pages_sig, pages = _scan_pages(entry, tile_size)
        if not pages:
            continue
        sig_parts.append(f"{entry.name}\u0000{pages_sig}")
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
                    mtime=cover.mtime,
                ),
            )
        )
    return _sig(sig_parts), books


class Catalog:
    """TTL-cached book/page listings with change signatures.

    Listings are re-scanned only when older than ``ttl`` seconds, or when a
    caller passes ``force=True`` (the Reload button). The signature lets the
    client skip a rebuild when nothing changed.
    """

    def __init__(self, archive_root: Path, tile_size: int, ttl: float = 600.0) -> None:
        self.archive_root = archive_root
        self.tile_size = tile_size
        self.ttl = ttl
        self._lock = threading.Lock()
        self._books: tuple[float, str, list[BookSummary]] | None = None
        self._pages: dict[Path, tuple[float, str, list[PageInfo]]] = {}

    def books(self, force: bool = False) -> tuple[str, list[BookSummary]]:
        """Return ``(signature, books)``, using the cache when fresh."""
        with self._lock:
            now = time.monotonic()
            if not force and self._books is not None and now - self._books[0] < self.ttl:
                return self._books[1], self._books[2]
            signature, books = _scan_books(self.archive_root, self.tile_size)
            self._books = (now, signature, books)
            return signature, books

    def pages(self, book_id: str, force: bool = False) -> tuple[str, list[PageInfo]]:
        """Return ``(signature, pages)`` for a book, using the cache when fresh."""
        bdir = book_dir(self.archive_root, book_id)
        with self._lock:
            now = time.monotonic()
            cached = self._pages.get(bdir)
            if not force and cached is not None and now - cached[0] < self.ttl:
                return cached[1], cached[2]
            signature, pages = _scan_pages(bdir, self.tile_size)
            self._pages[bdir] = (now, signature, pages)
            return signature, pages


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
