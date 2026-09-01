"""Generate a small local archive and seed its rights.

Creates a couple of sample books (gradient pages with page numbers, both
portrait and landscape, spread over two groups plus an "extra" page) under the
local dev archive root, then seeds the dev rights database so every sample
book/page resolves to full access for anonymous viewers — matching what the
real owner's own scans look like.

Idempotent: existing books are left alone unless ``force=True`` regenerates
them. Rights seeding is an upsert, so re-running is always safe.
"""

from __future__ import annotations

import random
import zlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# book dir name -> (page count, group count, portrait mix)
# The group count splits the pages into multiple named groups so the grid has
# group-boundary gaps to exercise the cross lattice; "Cover.png" is an extra
# page (non-conventional name) that lands in its own trailing group.
BOOKS: dict[str, tuple[str, int, int]] = {
    "sample-book": ("Sample Book", 12, 2),
    "portrait-book": ("Portrait Book", 6, 1),
    # Client-stability stress test: a single 32x32 grid of 1000 pages.
    "stress-book": ("Stress Test", 1000, 1),
}

PAGE_W, PAGE_H = 1024, 1360  # portrait page; landscape swaps the axes
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/Adwaita/AdwaitaSans-Regular.ttf",
]

_FONT_CACHE: dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Best available TrueType font at ``size``, else the tiny default bitmap.

    Fonts are cached so generating hundreds of pages doesn't re-open files.
    """
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    for path in FONT_CANDIDATES:
        try:
            if Path(path).is_file():
                font = ImageFont.truetype(path, size)
                _FONT_CACHE[size] = font
                return font
        except OSError:
            continue
    font = ImageFont.load_default()
    _FONT_CACHE[size] = font
    return font


def _gradient(w: int, h: int, c1: tuple, c2: tuple) -> Image.Image:
    """Vertical gradient page background (fast: one horizontal line per row)."""
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        fill = tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))
        d.line([(0, y), (w, y)], fill=fill)
    return img


def _page_text(d: ImageDraw.ImageDraw, w: int, h: int, lines: list[tuple[str, int, tuple]]) -> None:
    """Draw centered text lines (label, baseline y offset, color)."""
    for text, size, color in lines:
        f = _font(size)
        box = d.textbbox((0, 0), text, font=f)
        tw, th = box[2] - box[0], box[3] - box[1]
        d.text(((w - tw) / 2 - box[0], (h - th) / 2 - box[1]), text, font=f, fill=color)


def _write_page(path: Path, order_label: str, book_title: str, rng: random.Random) -> None:
    """Render one gradient page (portrait unless the seed says landscape)."""
    landscape = rng.random() < 0.3
    w, h = (PAGE_H, PAGE_W) if landscape else (PAGE_W, PAGE_H)
    c1 = (rng.randint(60, 200), rng.randint(60, 200), rng.randint(60, 200))
    c2 = (rng.randint(20, 90), rng.randint(20, 90), rng.randint(20, 90))
    img = _gradient(w, h, c1, c2)
    d = ImageDraw.Draw(img)
    # A few decorative lines so tiles have something to sharpen against.
    for _ in range(5):
        x0 = rng.randint(0, w - 1)
        y0 = rng.randint(0, h - 1)
        x1 = rng.randint(0, w - 1)
        y1 = rng.randint(0, h - 1)
        d.line([(x0, y0), (x1, y1)], fill=(255, 255, 255), width=2)
    _page_text(d, w, h, [
        (order_label, max(48, min(w, h) // 5), (255, 255, 255)),
        (book_title, max(20, min(w, h) // 12), (255, 255, 255)),
    ])
    img.save(path, "JPEG", quality=85, progressive=True)


def ensure(archive_root: Path, cache_dir: Path, force: bool = False) -> list[str]:
    """Generate the sample books (unless present) and seed their rights.

    Returns the list of book ids present after the call.
    """
    archive_root.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    for book_id, (title, count, groups) in BOOKS.items():
        book_dir = archive_root / book_id
        if book_dir.exists() and not force:
            continue
        book_dir.mkdir(parents=True, exist_ok=True)
        # Deterministic seed: hash() is randomized per process (PYTHONHASHSEED),
        # so use crc32 for stable page output across runs.
        rng = random.Random(zlib.crc32(book_id.encode()))
        for i in range(1, count + 1):
            group = (i - 1) % groups + 1
            order = (i - 1) // groups + 1
            name = f"{group}_{order:03d}-Page {group}.{order:03d}.png"
            _write_page(book_dir / name, f"{group}.{order:03d}", title, rng)
        # An "extra" page (non-conventional name) exercises the extra group.
        _write_page(book_dir / "Cover.png", "Cover", title, rng)

    _seed_rights(archive_root, cache_dir)
    return sorted(p.name for p in archive_root.iterdir() if p.is_dir())


def _seed_rights(archive_root: Path, cache_dir: Path) -> None:
    """Open every sample book/page as fully public in the dev rights DB.

    Idempotent and incremental: a book whose pages are already all public is
    skipped, so starting the dev server with the 1000-page stress book stays
    fast (no 1000 upserts on every boot).
    """
    from server.rights.store import RightsStore  # import here: server dep optional

    store = RightsStore(cache_dir / "rights.db")
    for book_dir in archive_root.iterdir():
        if not book_dir.is_dir():
            continue
        files = [f.name for f in sorted(book_dir.iterdir()) if f.is_file()]
        seeded = store.page_defaults_map(book_dir.name)
        if set(seeded) == set(files):
            store.update_book(book_dir.name, visibility="public")
            continue
        store.update_book(book_dir.name, visibility="public")
        for name in files:
            store.set_page_default(book_dir.name, name, "public")
