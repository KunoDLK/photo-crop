"""A virtual page composed server-side from many separate source images.

The "mosaic" image source presents a grid of independent images (e.g. a 31x31
block of Sentinel-2 tiles) as one seamless zoomable page. No giant stitched
file and no precomputed zoom proxies ever exist: every requested tile is
rendered on demand by decoding the full-resolution source images its rectangle
overlaps, cropping each overlap, and downsampling onto the 256px output. The
shared SQLite tile cache is what makes repeat views cheap — a tile is rendered
once per zoom level and served from disk afterwards.

At the coarse end (wafer-scale canvases of hundreds of cells) full-res decodes
would be absurd — one overview tile over N cells would cost N decodes of
~300 MB each. So the mosaic source also maintains a **proxy chain** per cell
in the tile store (see :mod:`~server.tiles.sqlite_cache` and
``docs/wafer-mosaic-scaling.md``): one lossless PNG plane per coarse-band
level, built once from a single full-res decode by successive halving. A tile
whose overlapping cells all render sub-tile-wide (band rule G1 below) draws
from those tiny planes instead of the sources — N PNG decodes instead of N
full decodes. The choice is a pure function of the manifest and level, never
of what happens to be cached, so every tile key maps to one byte stream.

When the chains are missing, :meth:`MosaicSource.proxy_state` reports how far
generation has progressed and :meth:`MosaicSource.ensure_warming` starts a
background sweep; the state is attached to page listings, exposed at
``/api/mosaic/{book}/proxy``, and echoed on every provider tile response as
``X-Mosaic-*`` headers, so a client can hold off tile requests while the page
warms and show the percentage as image progress.

A manifest describes a **book**: its id/name and one or more **pages**, each
page being its own virtual canvas with its own cells. The single-canvas shape
is kept for older manifests (it becomes one page, id ``mosaic``); a ``pages``
list defines several::

    {
      "book": "siliconprawn",
      "name": "Silicon wafer",
      "pages": [
        {"id": "overview", "name": "Overview",
         "canvas": {"width": 34000, "height": 38500},
         "cells": [{"id": "00x00", "x": 0, "y": 0, "width": 4096, "height": 4096,
                    "source": "cells/00x00.jpg"}, ...]},
        {"id": "detail", "name": "Detail corner",
         "canvas": {"width": 8192, "height": 8192},
         "cells": [...]}
      ]
    }

The legacy single-canvas shape::

    {
      "version": 1788564183968674999,
      "book": "satellite-mosaic",
      "name": "Satellite mosaic",
      "canvas": {"width": 32940, "height": 32940},
      "cells": [{"id": "30UWB", "x": 0, "y": 21960, "width": 10980, "height": 10980,
                 "source": "../satellite/30UWB_2025-10-27_q60.jpg"}, ...]
    }

``book``/``name`` default to ``satellite-mosaic``/``Satellite mosaic`` so
older manifests keep working. Cell ``source`` paths are resolved relative to
the manifest file. Each page's ``version`` (its tile/URL namespace) is
recomputed on every load from the manifest file mtime and its cell source
mtimes, so re-saving a cell image or editing the manifest is picked up on the
next reload; a manifest may still pin ``version`` explicitly (it is treated as
a floor). Renders are pure functions of the tile request (source images are
immutable for a given version), so tiles cache immutably like every other
source.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from ..errors import BadRequest, NotFound
from ..licenses import read_license_text
from ..models import (
    AccessInfo, BookSummary, CoverInfo, ImageInfo, PageInfo, ProxyStatus,
)
from ..tiles import geometry
from ..tiles.page_cache import PageCache
from ..tiles.sqlite_cache import TileCache
from .base import ImageSource, TileRequest

#: Mosaic renders are reported at INFO on this logger (wired to stdout by the
#: app); cold inits that build proxy chains get one summary line per render.
logger = logging.getLogger("server.sources.mosaic")

#: A render that took at least this long is logged even when it built no
#: chains (e.g. a deep tile whose full-res decodes missed the page cache).
LOG_RENDER_MIN_SECONDS = 1.0

#: Variant namespace of provider tiles in the shared store (their keys are
#: ``p/<source>/...``). Proxy chains must carry the same ``ns``/book/page/
#: version family as the tiles they feed, so redundancy queries match.
PROVIDER_NS = "p"

#: Seconds a probe of the page's chain-family count is reused before it is
#: re-read from the store. Keeps the per-tile response headers cheap while the
#: client polls (which itself passes ``fresh=True``), and bounds how long a
#: completed generation can go unnoticed to ~seconds.
PROXY_STATE_TTL = 1.0

#: Page id of a legacy single-canvas manifest (kept stable so existing tile
#: URLs, OCR caches and share links do not change).
DEFAULT_PAGE_ID = "mosaic"


@dataclass(frozen=True)
class Cell:
    """One source image placed on the virtual canvas."""

    id: str
    x: int
    y: int
    width: int
    height: int
    source: Path

    @property
    def size(self) -> int:
        """Band membership and chain widths use the largest edge (G5)."""
        return max(self.width, self.height)


def _slug(text: str) -> str:
    """Turn a name into a URL/file-safe page id (alnum + single dashes).

    Args:
        text: The raw name to slugify.

    Returns:
        A lowercase slug, or ``""`` when the text has no alphanumerics.
    """
    out: list[str] = []
    prev_dash = False
    for ch in text.strip().lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-")


@dataclass(frozen=True)
class MosaicPage:
    """One mosaic image: a virtual canvas and the cells placed on it."""

    id: str
    name: str
    version: int
    canvas_w: int
    canvas_h: int
    cells: tuple[Cell, ...]

    @property
    def max_level(self) -> int:
        """Coarsest pyramid level (the whole canvas on one tile)."""
        return geometry.max_level(self.canvas_w, self.canvas_h, 256)

    def overlapping(self, x0: int, y0: int, x1: int, y1: int) -> list[Cell]:
        """Cells whose canvas box intersects the given integer canvas rect."""
        return [
            cell for cell in self.cells
            if cell.x < x1 and cell.x + cell.width > x0
            and cell.y < y1 and cell.y + cell.height > y0
        ]


def _load_page(
    path: Path, spec: dict, index: int, multi: bool, mtime: int,
) -> MosaicPage:
    """Parse and validate one page spec (a legacy manifest, or one ``pages`` item).

    Args:
        path: The manifest file the page belongs to (resolves cell sources).
        spec: The page mapping (``canvas`` + ``cells`` + optional ``id``/
            ``name``/``version``), or the whole legacy manifest when ``multi``
            is false.
        index: Position in the page list (naming fallback).
        multi: Whether the manifest used the multi-page ``pages`` shape.
        mtime: The manifest file's mtime (a version floor).

    Returns:
        The validated page.

    Raises:
        errors.BadRequest: If the page is malformed, its cells do not fit the
            canvas, a cell source is missing, or its id is invalid.
    """
    try:
        cw, ch = int(spec["canvas"]["width"]), int(spec["canvas"]["height"])
        cells = tuple(
            Cell(
                id=str(c["id"]),
                x=int(c["x"]),
                y=int(c["y"]),
                width=int(c["width"]),
                height=int(c["height"]),
                source=(path.parent / c["source"]).resolve(),
            )
            for c in spec["cells"]
        )
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise BadRequest(f"invalid mosaic manifest: {exc}") from exc

    if cw <= 0 or ch <= 0 or not cells:
        raise BadRequest("mosaic manifest needs a canvas and at least one cell")
    for cell in cells:
        if (
            cell.x < 0 or cell.y < 0
            or cell.x + cell.width > cw or cell.y + cell.height > ch
        ):
            raise BadRequest(f"mosaic cell {cell.id} lies outside the canvas")
        if not cell.source.is_file():
            raise BadRequest(f"mosaic cell {cell.id} source missing: {cell.source}")

    if multi:
        page_id = str(spec.get("id") or _slug(str(spec.get("name", ""))) or f"page{index + 1}")
        name = str(spec.get("name") or page_id)
    else:
        page_id, name = DEFAULT_PAGE_ID, "Mosaic"
    if "/" in page_id or not page_id:
        raise BadRequest("mosaic page id must be a non-empty slug")

    # Version is the newest of: any explicit version, the manifest file mtime
    # and every cell source mtime — so a re-saved cell or an edited manifest
    # produces fresh tile URLs on the next load (no stale CDN/browser bytes).
    declared = spec.get("version")
    versions = [mtime]
    if declared is not None:
        try:
            versions.append(int(declared))
        except (TypeError, ValueError):
            pass
    for cell in cells:
        try:
            versions.append(cell.source.stat().st_mtime_ns)
        except OSError:
            continue
    return MosaicPage(page_id, name, max(versions), cw, ch, cells)


@dataclass(frozen=True)
class MosaicManifest:
    """A mosaic book: its identity plus one or more mosaic pages."""

    book: str
    name: str
    pages: tuple[MosaicPage, ...]

    @property
    def default_page_id(self) -> str:
        """Id of the cover page (the first)."""
        return self.pages[0].id

    @staticmethod
    def load(path: Path) -> "MosaicManifest":
        """Parse and validate a manifest file (single-canvas or ``pages`` list).

        Args:
            path: Path to ``manifest.json``.

        Returns:
            The validated book.

        Raises:
            errors.BadRequest: If the manifest is malformed, has no pages, or a
                page does not validate.
        """
        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            raise BadRequest(f"invalid mosaic manifest: {exc}") from exc
        if not isinstance(raw, dict):
            raise BadRequest("mosaic manifest must be a JSON object")

        book = str(raw.get("book", "satellite-mosaic"))
        name = str(raw.get("name", "Satellite mosaic"))
        if "/" in book or not book:
            raise BadRequest("mosaic manifest book id must be a non-empty slug")

        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            mtime = 0

        pages_spec = raw.get("pages")
        multi = isinstance(pages_spec, list)
        specs = pages_spec if multi else [raw]

        pages: list[MosaicPage] = []
        seen: set[str] = set()
        for index, spec in enumerate(specs):
            if not isinstance(spec, dict):
                raise BadRequest("mosaic page must be a JSON object")
            page = _load_page(path, spec, index, multi, mtime)
            if page.id in seen:
                raise BadRequest(f"duplicate mosaic page id: {page.id}")
            seen.add(page.id)
            pages.append(page)
        if not pages:
            raise BadRequest("mosaic manifest needs at least one page")
        return MosaicManifest(book, name, tuple(pages))


def plane_geometry(cell: Cell, tile_size: int, level: int) -> tuple[int, int, int, int, int]:
    """The pure manifest geometry of a plane's dependent tile set (G4).

    A plane for ``(cell, level)`` feeds exactly the level-``level`` grid tiles
    whose canvas rectangle intersects the cell: tile columns ``[tx0, tx1]``,
    rows ``[ty0, ty1]``, where a level's tile edge is ``tile_size * 2**level``
    canvas pixels. ``expected`` is the product range size — the number of
    tiles that exist once the cell is fully rendered at that level.

    Args:
        cell: The source cell the plane belongs to.
        tile_size: Output tile edge (``TILE``).
        level: Pyramid level of the plane.

    Returns:
        A ``(tx0, tx1, ty0, ty1, expected)`` tuple.
    """
    slot = tile_size << level
    tx0 = cell.x // slot
    tx1 = (cell.x + cell.width - 1) // slot
    ty0 = cell.y // slot
    ty1 = (cell.y + cell.height - 1) // slot
    expected = (tx1 - tx0 + 1) * (ty1 - ty0 + 1)
    return tx0, tx1, ty0, ty1, expected


@dataclass
class _RenderProbe:
    """Per-render cold-path counters, accumulated on the rendering thread.

    Renders (and chain builds) run on worker threads and each request resets
    its own probe, so concurrent requests never mix statistics. Used only for
    the server-side cold-init log lines.
    """

    cells_drawn: int = 0
    chains_built: int = 0
    planes_stored: int = 0
    plane_bytes: int = 0
    decode_count: int = 0
    decode_seconds: float = 0.0
    chain_build_seconds: float = 0.0
    skipped_chains: int = 0

    def reset(self) -> None:
        for name in self.__dataclass_fields__:  # noqa: SLF001 — dataclass helper
            setattr(self, name, 0)


def build_chain_planes(
    full: np.ndarray, band_levels: set[int],
) -> dict[int, tuple[int, bytes]]:
    """Produce every plane of a cell's chain from one full-res decode (B2).

    Planes are built by successive INTER_AREA halving so the whole chain costs
    roughly a third of one extra full-res pass and no source pixels are ever
    skipped. Only the band levels are kept and encoded lossless (PNG, B3) so
    the halving chain never compounds JPEG artefacts; larger intermediates are
    computed and discarded. The result is a pure function of the decoded image
    and the band, so a chain rebuilt after eviction reproduces identical bytes
    (V2).

    Args:
        full: The cell's full-resolution BGR image.
        band_levels: Levels whose planes are stored (G2); may include 0 when
            the whole cell is sub-tile (its full image is the level-0 plane).

    Returns:
        A ``{level: (width, png_bytes)}`` map, one entry per band level.
    """
    planes: dict[int, tuple[int, bytes]] = {}
    if 0 in band_levels:
        ok, buf = cv2.imencode(".png", full)
        if not ok:
            raise BadRequest("proxy plane encode failed")
        planes[0] = (full.shape[1], buf.tobytes())
    prev = full
    top = max(band_levels) if band_levels else 0
    for level in range(1, top + 1):
        h, w = prev.shape[:2]
        prev = cv2.resize(prev, ((w + 1) // 2, (h + 1) // 2), interpolation=cv2.INTER_AREA)
        if level in band_levels:
            ok, buf = cv2.imencode(".png", prev)
            if not ok:
                raise BadRequest("proxy plane encode failed")
            planes[level] = (prev.shape[1], buf.tobytes())
    return planes


class MosaicSource(ImageSource):
    """A pluggable image source serving a mosaic book from many images.

    Attributes:
        manifest: The validated book (pages/cells).
        tile_size: Output tile edge length (must match the client's ``TILE``).
    """

    key = "mosaic"
    cacheable = True
    cache_control = "public, max-age=31536000, immutable"

    def __init__(
        self,
        manifest_path: Path,
        tile_size: int = 256,
        page_cache: PageCache | None = None,
        store: TileCache | None = None,
        proxies_enabled: bool = True,
    ) -> None:
        """Attach the source to a manifest and the shared decoded/store caches.

        Args:
            manifest_path: Path to ``manifest.json``.
            tile_size: Output tile edge length.
            page_cache: Shared decoded-image LRU (cells' full-res bitmaps).
            store: Shared SQLite tile store; proxy chains live here. Without
                one the source always renders from full-res decodes.
            proxies_enabled: When false, today's behaviour: every level
                renders from full-res decodes.
        """
        self._manifest_path = manifest_path
        self.tile_size = tile_size
        self._page_cache = page_cache
        self._store = store
        self._proxies_enabled = proxies_enabled and store is not None
        self._decode_locks: dict[str, threading.Lock] = {}
        self._decode_locks_guard = threading.Lock()
        self._probe_local = threading.local()
        # Proxy-generation state, per page id: chain builds executing right now,
        # the on-demand warm sweep thread, and short-lived count caches.
        self._builds_guard = threading.Lock()
        self._active_builds: dict[str, int] = {}
        self._warm_lock = threading.Lock()
        self._warm_threads: dict[str, threading.Thread] = {}
        self._state_guard = threading.Lock()
        self._state_cache: dict[str, tuple[int, float, int]] = {}
        self._chain_total_cache: dict[str, tuple[MosaicPage, int]] = {}
        self._source_bytes_cache: dict[str, tuple[MosaicPage, int]] = {}
        self._load()

    def _load(self) -> None:
        """(Re)read the manifest into ``self.manifest`` and the page index."""
        self.manifest = MosaicManifest.load(self._manifest_path)
        self._pages = {page.id: page for page in self.manifest.pages}

    def owns(self, book_id: str) -> bool:
        return book_id == self.manifest.book

    @property
    def provider_identity(self) -> str:
        """Stable identity = the manifest path, so a force reload reuses this
        instance (its decode locks and warm sweeps) while the path is unchanged.

        Returns:
            ``mosaic:<manifest path>``.
        """
        return f"{self.key}:{self._manifest_path}"

    def refresh(self) -> None:
        """Reload the manifest so force-reloads see new/edited mosaic content.

        Re-reads the file and recomputes every page's version from the manifest
        and cell source mtimes, so a re-saved cell image or an added/removed/
        renamed page surfaces with fresh URLs and a new listing signature. A
        manifest that fails to parse (e.g. caught mid-write) leaves the current
        layout serving; the next reload retries.
        """
        try:
            manifest = MosaicManifest.load(self._manifest_path)
        except BadRequest:
            return
        self.manifest = manifest
        self._pages = {page.id: page for page in manifest.pages}

    def has_page(self, page_id: str) -> bool:
        """True when this book serves a page with the given id."""
        return page_id in self._pages

    @property
    def default_page_id(self) -> str:
        """Id of the book's cover page (the first)."""
        return self.manifest.default_page_id

    def _access(self) -> AccessInfo:
        """Provider pages are unrestricted demo content: full, never region-locked."""
        return AccessInfo(status="full", zone="", region_locked=False)

    @property
    def signature(self) -> str:
        """Change signature: every page's id/version plus its cell placements."""
        digest = hashlib.sha256()
        digest.update(f"{self.manifest.book}\n".encode())
        for page in self.manifest.pages:
            digest.update(f"{page.id}\n{page.version}\n".encode())
            for cell in page.cells:
                digest.update(f"{cell.id}{cell.x}{cell.y}".encode())
        return digest.hexdigest()

    def list_books(self) -> list[BookSummary]:
        access = self._access()
        page = self.manifest.pages[0]
        return [
            BookSummary(
                id=self.manifest.book,
                name=self.manifest.name,
                cover=CoverInfo(
                    page_id=page.id,
                    width=page.canvas_w,
                    height=page.canvas_h,
                    max_level=page.max_level,
                    mtime=page.version,
                    access=access,
                    source=self.key,
                    proxy=self._proxy_payload(page),
                ),
                visibility="public",
            )
        ]

    def pages(self, book_id: str) -> tuple[str, list[PageInfo]] | None:
        if not self.owns(book_id):
            return None
        infos = [
            PageInfo(
                page_id=page.id,
                name=page.name,
                group=1,
                order=str(index),
                width=page.canvas_w,
                height=page.canvas_h,
                max_level=page.max_level,
                mtime=page.version,
                access=self._access(),
                source=self.key,
                proxy=self._proxy_payload(page),
            )
            for index, page in enumerate(self.manifest.pages, 1)
        ]
        return self.signature, infos

    def image_info(self, book_id: str, page_id: str) -> ImageInfo | None:
        page = self._pages.get(page_id)
        if not self.owns(book_id) or page is None:
            return None
        return ImageInfo(
            page_id=page_id,
            width=page.canvas_w,
            height=page.canvas_h,
            max_level=page.max_level,
            file_size=self._source_bytes(page),
            hash=f"provider:{self.key}:{self.signature}",
            access=self._access(),
            source=self.key,
            proxy=self._proxy_payload(page),
            license=self._page_license(page),
        )

    def _page_license(self, page: MosaicPage) -> str | None:
        """A mosaic page's licence text, from a ``.LICENSE`` sidecar, or ``None``.

        Looked up beside the manifest as ``<page-id>.LICENSE`` (checked first),
        then inside the page's cell folder as ``LICENSE``.

        Args:
            page: The page whose licence to read.

        Returns:
            The licence text, or ``None``.
        """
        base = self._manifest_path.parent
        for candidate in (base / f"{page.id}.LICENSE", base / page.id / "LICENSE"):
            text = read_license_text(candidate)
            if text:
                return text
        return None

    def _source_bytes(self, page: MosaicPage) -> int:
        """Aggregate on-disk size of one page's cell source images.

        A mosaic has no single file; its "file size" is the sum of the source
        images it composes (the byte budget a full stitched equivalent would
        need). Computed once per page instance — cell files are immutable for a
        given page version, and a reload replaces the page.

        Args:
            page: The page whose cells to size.

        Returns:
            The sum of the cell source file sizes in bytes.
        """
        cached = self._source_bytes_cache.get(page.id)
        if cached is not None and cached[0] is page:
            return cached[1]
        total = 0
        for cell in page.cells:
            try:
                total += cell.source.stat().st_size
            except OSError:
                continue
        self._source_bytes_cache[page.id] = (page, total)
        return total

    def proxy_state(self, page_id: str, fresh: bool = False) -> ProxyStatus:
        """One page's proxy-generation state (ready cells, progress, activity).

        Reports how many of the page's cells that need a proxy chain actually
        have one stored, so a client can hold off tile requests while the
        coarse band is still being generated and show the percentage as image
        progress. ``ready_cells`` comes from the tile store (a family-scoped
        chain count); ``generating`` is true while any chain build for the page
        is running, including render-driven cold builds.

        Args:
            page_id: The page to report (unknown pages report ``enabled=False``).
            fresh: Re-read the chain count from the store instead of reusing
                the ~1 s probe cache. Listings and the status endpoint pass
                True; per-tile response headers use the cached value.

        Returns:
            The page's :class:`~models.ProxyStatus`. When proxies are disabled
            (or the page is unknown) it reports ``enabled=False`` and
            ``ready=True`` so clients never gate on it.
        """
        page = self._pages.get(page_id)
        book = self.manifest.book
        if page is None or not self._proxies_enabled:
            return ProxyStatus(
                book=book, page=page_id, version=page.version if page else 0,
                enabled=False, ready=True, ready_cells=0, total_cells=0,
                percent=100, generating=False,
            )
        total = self._chain_need_total(page)
        ready = min(self._ready_cells(page, fresh), total)
        percent = 100 if total <= 0 else int(round(100.0 * ready / total))
        return ProxyStatus(
            book=book, page=page.id, version=page.version,
            enabled=True, ready=ready >= total, ready_cells=ready,
            total_cells=total, percent=percent, generating=self._generating(page.id),
        )

    def _proxy_payload(self, page: MosaicPage) -> ProxyStatus | None:
        """Proxy state for a listing record, or ``None`` when not applicable.

        Non-proxy sources (and proxy-disabled mosaics) leave the field unset,
        so the client falls back to today's plain tile fetching.

        Args:
            page: The page the listing record describes.

        Returns:
            The page's proxy state, or ``None``.
        """
        if not self._proxies_enabled:
            return None
        return self.proxy_state(page.id, fresh=True)

    def _ready_cells(self, page: MosaicPage, fresh: bool) -> int:
        """Stored-chain count for the page family, with a short-lived cache.

        Args:
            page: The page whose family is counted.
            fresh: Bypass the probe cache and re-read the store.

        Returns:
            The number of cells whose chain is stored (0 without a store).
        """
        store = self._store
        if store is None:
            return 0
        now = time.monotonic()
        with self._state_guard:
            cached = self._state_cache.get(page.id)
            if not fresh and cached is not None and cached[0] == page.version and now < cached[1]:
                return cached[2]
        count = store.count_family_chains(
            PROVIDER_NS, self.manifest.book, page.id, page.version
        )
        with self._state_guard:
            self._state_cache[page.id] = (page.version, now + PROXY_STATE_TTL, count)
        return count

    def _chain_need_total(self, page: MosaicPage) -> int:
        """Number of a page's cells that need a proxy chain (any band level).

        Computed once per page instance, like :meth:`_source_bytes`: cell
        geometry is immutable for a given page version.

        Args:
            page: The page to inspect.

        Returns:
            The number of cells whose :meth:`_band_levels` is non-empty.
        """
        cached = self._chain_total_cache.get(page.id)
        if cached is not None and cached[0] is page:
            return cached[1]
        total = sum(1 for cell in page.cells if self._band_levels(cell, page))
        self._chain_total_cache[page.id] = (page, total)
        return total

    def _generating(self, page_id: str) -> bool:
        """True while any chain build for the page is running (or its sweep is live).

        Args:
            page_id: The page to test.

        Returns:
            Whether generation activity is currently underway for the page.
        """
        with self._builds_guard:
            active = self._active_builds.get(page_id, 0)
        thread = self._warm_threads.get(page_id)
        return active > 0 or (thread is not None and thread.is_alive())

    def ensure_warming(self, page_id: str) -> bool:
        """Start one background chain sweep for the page when none is running.

        Called by the status endpoint so a client that asks about a page whose
        chains are missing gets generation started instead of having to open
        with a cold render. The sweep is the same pass used at boot: it skips
        cells that already have a chain and shares the per-cell decode locks
        with any concurrent render, so a caller that starts one changes nothing
        for an already-running sweep.

        Args:
            page_id: The page to warm.

        Returns:
            Whether generation is (or was already) underway.
        """
        if self._store is None or not self._proxies_enabled or page_id not in self._pages:
            return False
        with self._warm_lock:
            thread = self._warm_threads.get(page_id)
            if thread is not None and thread.is_alive():
                return True
            thread = threading.Thread(
                target=self._warm_page,
                args=(page_id,),
                name=f"mosaic-warm:{self.manifest.book}/{page_id}",
                daemon=True,
            )
            self._warm_threads[page_id] = thread
            thread.start()
        return True

    def _build_started(self, page_id: str) -> None:
        """Mark a chain build as running (thread-safe, never blocks renders)."""
        with self._builds_guard:
            self._active_builds[page_id] = self._active_builds.get(page_id, 0) + 1

    def _build_finished(self, page_id: str) -> None:
        """Mark a chain build for the page as done."""
        with self._builds_guard:
            remaining = self._active_builds.get(page_id, 0) - 1
            if remaining > 0:
                self._active_builds[page_id] = remaining
            else:
                self._active_builds.pop(page_id, None)

    def tile_zoom(self, level: int, page: str | None = None) -> int:
        """Cache eviction depth: canvas levels are archive-style (0 = 1:1)."""
        target = self._pages.get(page) if page is not None else None
        if target is None:
            target = self.manifest.pages[0]
        return target.max_level - level

    def render_tile(self, req: TileRequest) -> np.ndarray:
        """Compose one tile from every source image it overlaps.

        Args:
            req: Tile coordinate and size (from the shared tile service).

        Returns:
            A ``(tile_size, tile_size, 3)`` uint8 BGR image.

        Raises:
            errors.NotFound: For unknown book/page ids or missing sources.
            errors.BadRequest: For out-of-range levels or coordinates.
        """
        page = self._pages.get(req.page)
        if not self.owns(req.book) or page is None:
            raise NotFound(f"provider page not found: {req.book}/{req.page}")
        if not (0 <= req.level <= page.max_level):
            raise BadRequest(f"level {req.level} out of range (0..{page.max_level})")
        cols, rows = geometry.grid_extent(
            page.canvas_w, page.canvas_h, req.tile_size, req.level
        )
        if not (0 <= req.tx < cols and 0 <= req.ty < rows):
            raise BadRequest(f"tile ({req.tx},{req.ty}) out of range for level {req.level}")

        started = time.perf_counter()
        self._probe().reset()
        canvas = np.zeros((req.tile_size, req.tile_size, 3), dtype=np.uint8)
        scale = 1 << req.level  # canvas pixels per level pixel
        # The tile's rectangle in the level image, clamped to the canvas.
        rect = geometry.crop_rect(
            page.canvas_w, page.canvas_h, req.tile_size, req.level, req.tx, req.ty
        )
        # Same rectangle in 1:1 canvas pixels (level pixels times the scale).
        c_x0, c_y0 = rect.x * scale, rect.y * scale
        c_x1, c_y1 = (rect.x + rect.w) * scale, (rect.y + rect.h) * scale

        for cell in page.overlapping(c_x0, c_y0, c_x1, c_y1):
            self._draw_cell(canvas, req, cell, page, rect, scale)
        self._log_render(req, started, page)
        return canvas

    def _log_render(self, req: TileRequest, started: float, page: MosaicPage) -> None:
        """Log one summary line when a render paid a cold cost.

        A "cold init" (at least one proxy chain built, e.g. the first
        whole-canvas tile over a large mosaic) logs its total time split into
        source decoding and chain halving/encode/store so operators see where
        the cost went; slow renders with no chain builds (deep tiles whose
        full-res decode missed the page cache) are logged too.

        Args:
            req: The tile request that was rendered.
            started: ``time.perf_counter()`` at the start of the render.
            page: The page that was rendered.
        """
        probe = self._probe()
        total = time.perf_counter() - started
        if probe.chains_built == 0 and total < LOG_RENDER_MIN_SECONDS:
            return
        logger.info(
            "mosaic %s book=%s page=%s level=%d tile=%d,%d: %d/%d cells, built %d chains "
            "(%d planes, %.1f MB) in %.1fs (source decode %.1fs over %d, "
            "halve+encode+store %.1fs)",
            "cold init" if probe.chains_built else "slow render",
            req.book, page.id, req.level, req.tx, req.ty,
            probe.cells_drawn, len(page.cells),
            probe.chains_built, probe.planes_stored, probe.plane_bytes / 1048576.0,
            total, probe.decode_seconds, probe.decode_count,
            probe.chain_build_seconds,
        )

    def _uses_plane(self, cell: Cell, level: int) -> bool:
        """Band membership (G1): render from a proxy plane iff the cell's
        footprint is narrower than one tile at this level.

        Args:
            cell: The source image.
            level: Pyramid level.

        Returns:
            Whether level ``level`` falls in the cell's proxy band.
        """
        return cell.size < (self.tile_size << level)

    def _band_levels(self, cell: Cell, page: MosaicPage) -> frozenset[int]:
        """Every level whose render would draw this cell from a plane (G2).

        Args:
            cell: The source image.
            page: The page the cell belongs to (its max level bounds the band).

        Returns:
            The proxy band: ``{0..max_level}`` where ``cell.size < TILE << L``
            (level 0 joins the band only for sub-tile cells).
        """
        return frozenset(
            level for level in range(page.max_level + 1)
            if self._uses_plane(cell, level)
        )

    def _chain_id(self, cell: Cell, page: MosaicPage) -> str:
        """Cache key of the cell's proxy chain (page- and version-scoped)."""
        return (
            f"proxy/{PROVIDER_NS}/{self.manifest.book}/{page.id}/{page.version}/{cell.id}"
        )

    def _chain_family(self, page: MosaicPage) -> tuple[str, str, str, int]:
        """The tile-store family of this page's rows (ns, book, page, version)."""
        return PROVIDER_NS, self.manifest.book, page.id, page.version

    def _draw_cell(
        self,
        canvas: np.ndarray,
        req: TileRequest,
        cell: Cell,
        page: MosaicPage,
        rect: geometry.Rect,
        scale: int,
    ) -> None:
        """Resample one cell's overlap onto the output tile.

        Level pixels are exactly ``2**level`` canvas pixels wide, so every
        level pixel a cell touches maps back to one output column; consecutive
        cells abut with no gaps and no overlap. In the proxy band the cell is
        drawn from its chain's level plane (tiny PNG decode); in the deep band
        from the full-res bitmap in the shared decoded-image LRU (same cache
        and budget as archive page mipmaps), so panning and zooming reuse
        resident sources.

        Args:
            canvas: The output tile being painted (BGR uint8).
            req: The tile request.
            cell: The source image to draw.
            page: The page the cell belongs to.
            rect: The tile's rectangle in level-image space.
            scale: ``2 ** req.level``.
        """
        scale_m1 = scale - 1
        # Output columns/rows this cell covers (clamped to the tile).
        j0 = max(rect.x, cell.x >> req.level)
        j1 = min(rect.x + rect.w, (cell.x + cell.width + scale_m1) >> req.level)
        k0 = max(rect.y, cell.y >> req.level)
        k1 = min(rect.y + rect.h, (cell.y + cell.height + scale_m1) >> req.level)
        if j0 >= j1 or k0 >= k1:
            return
        self._probe().cells_drawn += 1

        # The cell's full-resolution pixels behind those output pixels.
        fx0 = min(cell.width, max(0, j0 * scale - cell.x))
        fx1 = min(cell.width, max(0, j1 * scale - cell.x))
        fy0 = min(cell.height, max(0, k0 * scale - cell.y))
        fy1 = min(cell.height, max(0, k1 * scale - cell.y))
        if fx0 >= fx1 or fy0 >= fy1:
            return

        if self._uses_plane(cell, req.level):
            plane = self._plane(cell, req.level, page)
            if plane is not None:
                # Plane pixel i covers cell-local source pixels
                # [i*2**L, (i+1)*2**L) (R3): fx/fy are already cell-local, so
                # crop the overlap out of the plane and let the shared resize
                # step absorb the boundary sliver.
                px0 = fx0 >> req.level
                px1 = (fx1 + scale_m1) >> req.level
                py0 = fy0 >> req.level
                py1 = (fy1 + scale_m1) >> req.level
                region = plane[py0:py1, px0:px1]
            else:
                region = None
        else:
            region = None

        if region is None:  # deep band (or proxy fallback): full-res source
            image = self._cell_image(cell, page)
            region = image[fy0:fy1, fx0:fx1]
        out_w, out_h = j1 - j0, k1 - k0
        if region.shape[1] != out_w or region.shape[0] != out_h:
            region = cv2.resize(region, (out_w, out_h), interpolation=cv2.INTER_AREA)
        canvas[k0 - rect.y : k1 - rect.y, j0 - rect.x : j1 - rect.x] = region

    def _plane(self, cell: Cell, level: int, page: MosaicPage) -> np.ndarray | None:
        """The cell's decoded plane for one band level, building its chain first.

        A chain build decodes the full-res source exactly once (B2/B5: through
        the shared decoded-image LRU), stores every band plane atomically
        (B6), and runs under the cell's decode lock so concurrent misses wait
        and re-check before building (B4).

        Args:
            cell: The source image.
            level: A band level of ``cell``.
            page: The page the cell belongs to.

        Returns:
            The decoded BGR plane, or ``None`` when the source has no store
            attached (the caller falls back to a full-res render).
        """
        store = self._store
        if store is None:
            return None
        chain_id = self._chain_id(cell, page)
        planes = self._ensure_chain(cell, chain_id, page)
        if planes is None or level not in planes:
            return None
        # The render that is about to be generated uses this plane (E6b):
        # generation refreshes the chain, cache hits never reach this point.
        store.touch_chain(chain_id)
        _, png = planes[level]
        image = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise NotFound(f"mosaic proxy plane undecodable: {chain_id}")
        return image

    def _ensure_chain(
        self, cell: Cell, chain_id: str, page: MosaicPage,
    ) -> dict[int, tuple[int, bytes]] | None:
        """Return the cell's complete chain, building it if missing or partial.

        Args:
            cell: The source image.
            chain_id: The chain's cache key.
            page: The page the cell belongs to.

        Returns:
            The chain's ``{level: (width, png)}`` map, or ``None`` when the
            source has no store attached.
        """
        store = self._store
        if store is None:
            return None
        band = self._band_levels(cell, page)
        planes = store.get_chain(chain_id)
        if planes is not None and all(level in planes for level in band):
            return planes
        with self._cell_lock(page, cell):
            planes = store.get_chain(chain_id)
            if planes is not None and all(level in planes for level in band):
                return planes
            full = self._cell_image_locked(cell, page)
            build_started = time.perf_counter()
            self._build_started(page.id)
            try:
                built = build_chain_planes(full, set(band))
                ns, book, pg, version = self._chain_family(page)
                rows = []
                for level, (width, png) in sorted(built.items()):
                    tx0, tx1, ty0, ty1, expected = plane_geometry(cell, self.tile_size, level)
                    rows.append((level, width, png, tx0, tx1, ty0, ty1, expected))
                store.put_chain(chain_id, ns, book, pg, version, rows)
            finally:
                # Generation activity covers render-driven cold builds too, so
                # the status endpoint reports "generating" even when no warm
                # sweep thread is alive.
                self._build_finished(page.id)
            probe = self._probe()
            probe.chains_built += 1
            probe.chain_build_seconds += time.perf_counter() - build_started
            probe.planes_stored += len(rows)
            probe.plane_bytes += sum(len(row[2]) for row in rows)
            return built

    def prewarm(self) -> None:
        """Build chains for every page's cells not yet in the store (O2).

        Runs on a caller-managed background thread (the app starts one when
        ``mosaic_proxy_prewarm`` is set); each cell decodes once through the
        shared decoded-image LRU and its chain is skipped when already stored.
        Also the sweep used by :meth:`ensure_warming` for on-demand generation.
        Progress and a final summary are logged per page on the mosaic logger.
        Cells sharing one source file still decode per cell here — the offline
        dev prewarmer (``dev/wafer_mosaic.py --prewarm``) dedupes by source.
        """
        for page_id in list(self._pages):
            self._warm_page(page_id)

    def _warm_page(self, page_id: str) -> None:
        """Build every missing chain of one page (boot prewarm + on demand).

        Args:
            page_id: The page to warm.
        """
        store = self._store
        page = self._pages.get(page_id)
        if store is None or page is None or not self._proxies_enabled:
            return
        probe = self._probe()
        probe.reset()
        started = time.perf_counter()
        logger.info(
            "mosaic prewarm start book=%s page=%s cells=%d",
            self.manifest.book, page_id, len(page.cells),
        )
        for n, cell in enumerate(page.cells, 1):
            if not self._band_levels(cell, page):
                probe.skipped_chains += 1
                continue
            built_before = probe.chains_built
            self._ensure_chain(cell, self._chain_id(cell, page), page)
            if probe.chains_built == built_before:
                probe.skipped_chains += 1
            if n % 200 == 0:
                logger.info(
                    "mosaic prewarm book=%s page=%s %d/%d cells (built %d, %d cached, %.1fs)",
                    self.manifest.book, page_id, n, len(page.cells),
                    probe.chains_built, probe.skipped_chains,
                    time.perf_counter() - started,
                )
        logger.info(
            "mosaic prewarm done book=%s page=%s cells=%d: built %d chains (%d planes, "
            "%.1f MB) in %.1fs (source decode %.1fs over %d), %d already cached",
            self.manifest.book, page_id, len(page.cells), probe.chains_built,
            probe.planes_stored, probe.plane_bytes / 1048576.0,
            time.perf_counter() - started, probe.decode_seconds,
            probe.decode_count, probe.skipped_chains,
        )

    def _probe(self) -> _RenderProbe:
        """This thread's cold-path counter (reset at each render start)."""
        probe = getattr(self._probe_local, "stats", None)
        if probe is None:
            probe = _RenderProbe()
            self._probe_local.stats = probe
        return probe

    def _cell_key(self, cell: Cell, page: MosaicPage) -> str:
        """Shared-cache key for one cell's decoded bitmap (page+version scoped)."""
        return f"mosaic:{page.id}:{page.version}:{cell.id}"

    def _cell_lock(self, page: MosaicPage, cell: Cell) -> threading.Lock:
        """The per-cell decode lock (created on first use, shared by renders
        and chain builds so a cell decodes once under concurrent requests).

        Args:
            page: The page the cell belongs to.
            cell: The source image.

        Returns:
            The cell's threading lock.
        """
        key = f"{page.id}:{cell.id}"
        with self._decode_locks_guard:
            return self._decode_locks.setdefault(key, threading.Lock())

    def _cell_image(self, cell: Cell, page: MosaicPage) -> np.ndarray:
        """The cell's decoded full-resolution BGR image, from the shared LRU.

        Args:
            cell: The source image.
            page: The page the cell belongs to.

        Returns:
            The BGR uint8 image.

        Raises:
            errors.NotFound: If the source file is missing or undecodable.
        """
        with self._cell_lock(page, cell):
            return self._cell_image_locked(cell, page)

    def _cell_image_locked(self, cell: Cell, page: MosaicPage) -> np.ndarray:
        """Decode one cell assuming the caller holds its decode lock.

        Args:
            cell: The source image.
            page: The page the cell belongs to.

        Returns:
            The BGR uint8 image (cached in the shared LRU when attached).

        Raises:
            errors.NotFound: If the source file is missing or undecodable.
        """
        cache = self._page_cache
        key = self._cell_key(cell, page)
        if cache is not None:
            image = cache.get(key)
            if image is not None:
                return image
        started = time.perf_counter()
        image = cv2.imread(str(cell.source), cv2.IMREAD_COLOR)
        probe = self._probe()
        probe.decode_seconds += time.perf_counter() - started
        probe.decode_count += 1
        if image is None:
            raise NotFound(f"mosaic cell source undecodable: {cell.source}")
        if cache is not None:
            cache.put(key, image, size_bytes=image.nbytes)
        return image
