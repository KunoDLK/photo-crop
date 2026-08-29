"""The fractal generator: a demo :class:`~sources.base.ImageSource`.

A Mandelbrot "book" (``fractals``) with one page (``mandelbrot``) whose tiles
are rendered on demand by :mod:`sources.fractal.mandelbrot` at any depth.
Isolated here so the books/tiles routers and the archive pipeline know
nothing about fractals — registering this source in ``app.py`` is the whole
integration.
"""
from __future__ import annotations

import hashlib

import numpy as np

from ...errors import BadRequest, NotFound
from ...models import AccessInfo, BookSummary, CoverInfo, ImageInfo, PageInfo
from ..base import ImageSource, TileRequest
from . import mandelbrot

#: Bump to invalidate every cached tile (new URL space, like a page mtime).
VERSION = 2


def _full_access() -> AccessInfo:
    """Provider pages are unrestricted demo content: full, never region-locked."""
    return AccessInfo(status="full", zone="", region_locked=False)


class FractalSource(ImageSource):
    """A procedurally generated Mandelbrot book with unbounded zoom depth.

    Level 0 is the whole fractal on one tile; each deeper level halves the
    covered span (level ``-k`` = a ``2^k x 2^k`` grid). ``MIN_LEVEL`` is the
    JS safe-integer wall (tile indices must stay below 2^53), i.e. no
    practical limit.
    """

    key = "fractal"
    #: Never store or reuse fractal bytes: re-render on every request. The
    #: Cache-Control header stays the public-immutable default so browsers
    #: and edge caches may still hold the identical bytes.
    cacheable = False
    BOOK_ID = "fractals"
    PAGE_ID = "mandelbrot"
    WIDTH = 256
    HEIGHT = 256
    MIN_LEVEL = -52
    VERSION = VERSION

    def __init__(self, params: mandelbrot.FractalParams | None = None) -> None:
        self.params = params or mandelbrot.FractalParams()

    def owns(self, book_id: str) -> bool:
        return book_id == self.BOOK_ID

    @property
    def signature(self) -> str:
        return hashlib.sha256(f"{self.key}:{self.VERSION}".encode()).hexdigest()

    def list_books(self) -> list[BookSummary]:
        access = _full_access()
        return [
            BookSummary(
                id=self.BOOK_ID,
                name="Fractals",
                cover=CoverInfo(
                    page_id=self.PAGE_ID,
                    width=self.WIDTH,
                    height=self.HEIGHT,
                    max_level=0,
                    mtime=self.VERSION,
                    access=access,
                    source=self.key,
                ),
                visibility="public",
            )
        ]

    def pages(self, book_id: str) -> tuple[str, list[PageInfo]] | None:
        if not self.owns(book_id):
            return None
        pages = [
            PageInfo(
                page_id=self.PAGE_ID,
                name="Mandelbrot",
                group=1,
                order="1",
                width=self.WIDTH,
                height=self.HEIGHT,
                max_level=0,
                mtime=self.VERSION,
                access=_full_access(),
                source=self.key,
            )
        ]
        return self.signature, pages

    def image_info(self, book_id: str, page_id: str) -> ImageInfo | None:
        if not self.owns(book_id) or page_id != self.PAGE_ID:
            return None
        return ImageInfo(
            page_id=page_id,
            width=self.WIDTH,
            height=self.HEIGHT,
            max_level=0,
            file_size=0,
            hash=f"provider:{self.key}:{self.PAGE_ID}:{self.VERSION}",
            access=_full_access(),
            source=self.key,
        )

    def render_tile(self, req: TileRequest) -> np.ndarray:
        if not self.owns(req.book) or req.page != self.PAGE_ID:
            raise NotFound(f"provider page not found: {req.book}/{req.page}")
        if not (self.MIN_LEVEL <= req.level <= 0):
            raise BadRequest(f"level {req.level} out of range ({self.MIN_LEVEL}..0)")
        grid = 2 ** (-req.level)
        if not (0 <= req.tx < grid and 0 <= req.ty < grid):
            raise BadRequest(
                f"tile ({req.tx},{req.ty}) out of range for level {req.level}"
            )
        return mandelbrot.render_tile(
            self.params, req.level, req.tx, req.ty, req.tile_size
        )
