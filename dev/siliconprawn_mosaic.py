"""Build a dev mosaic book from tiled wafer scans stored as ``<x>/<y>.jpg``.

Experiment tooling (dev only). A flatbed/tile-scan workflow saves one small
JPEG per scan position under integer-named folders: ``<scan>/<x>/<y>.jpg``
(e.g. ``Siliconprawn/8/0/0.jpg`` … ``8/135/153.jpg``), where the folder name is
the tile column and the filename stem is the row. This tool stitches one scan
into square **chunk** images (default 4096x4096, i.e. a power-of-two multiple
of the 256 px tile) and adds it as one **page** of a mosaic book: it writes the
chunks into a named folder under ``--out`` and merges a page into that folder's
``manifest.json`` (creating it on first use). Several scans can therefore share
one book, each shown as a page.

Each chunk becomes one mosaic cell; the canvas is the stitched scan at 1:1, so
the chunk grid (and edge chunks) is derived from the tile layout. The manifest
page omits ``version`` — the server recomputes each page's version from the
cell source mtimes, so re-saving a chunk is picked up on the next reload.

Usage::

    python3 dev/siliconprawn_mosaic.py --tiles DIR [--name SLUG]
                                       [--chunk 4096] [--quality 60]
                                       [--out DIR] [--book ID] [--book-name NAME]

``--tiles`` is the scan folder (e.g. ``.../Siliconprawn/8`` or
``.../mc68sz328_antifuse_mz_ms10x/7``). ``--name`` is the page id and the
chunk folder (default: the tiles' parent folder slugified, which is the scan
name for a ``<scan>/<n>/<x>/<y>.jpg`` layout). Run it once per scan to build a
multi-page book.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "dev" / "archive" / "siliconprawn"
#: Where the original fixture's scans live (outside the repo); pass
#: ``--tiles`` for any ``<x>/<y>.jpg`` scan, e.g.
#: ``/home/kuno/Docker-Server/Archive/Library/Siliconprawn/8``.
DEFAULT_CHUNK = 4096
DEFAULT_QUALITY = 60
DEFAULT_BOOK = "siliconprawn"
DEFAULT_BOOK_NAME = "Silicon wafer"


def _slug(text: str) -> str:
    """Lowercase URL/file-safe slug (alnum + single dashes)."""
    out: list[str] = []
    prev = False
    for ch in text.strip().lower():
        if ch.isalnum():
            out.append(ch)
            prev = False
        elif not prev:
            out.append("-")
            prev = True
    return "".join(out).strip("-")


def _tile_grid(root: Path) -> tuple[dict[tuple[int, int], Path], tuple[int, int]]:
    """Map every ``<x>/<y>.jpg`` under ``root`` to its grid cell.

    Only files whose immediate parent folder name and own stem are both
    integers count. The tile size comes from the first file (mismatched tiles
    are resized to it).

    Args:
        root: Folder holding the per-column subfolders of tile JPEGs.

    Returns:
        ``{(x, y): path}`` and the ``(tile_width, tile_height)`` first tile size.

    Raises:
        SystemExit: If no ``<x>/<y>.jpg`` tiles are found.
    """
    tiles: dict[tuple[int, int], Path] = {}
    for path in root.rglob("*.jpg"):
        stem = path.stem
        parent = path.parent.name
        if not (stem.lstrip("-").isdigit() and parent.lstrip("-").isdigit()):
            continue
        tiles[(int(parent), int(stem))] = path
    if not tiles:
        raise SystemExit(f"no <x>/<y>.jpg tiles found under {root}")
    probe = cv2.imread(str(next(iter(tiles.values()))), cv2.IMREAD_COLOR)
    if probe is None:
        raise SystemExit(f"cannot decode a tile under {root}")
    return tiles, (probe.shape[1], probe.shape[0])


def _combine(
    tiles_root: Path, cells_out: Path, chunk: int, quality: int,
) -> tuple[int, int, list[dict]]:
    """Stitch the tiles into ``chunk``-sized cell JPEGs under ``cells_out``.

    Args:
        tiles_root: Folder holding the ``<x>/<y>.jpg`` tiles.
        cells_out: Destination folder for the chunk images.
        chunk: Cell edge length in canvas pixels.
        quality: JPEG quality for the written cell images.

    Returns:
        The ``(canvas_w, canvas_h, cells)`` where ``cells`` are the page's cell
        records (``id``/``x``/``y``/``width``/``height``/``source``), with
        ``source`` relative to ``cells_out.parent``.

    Raises:
        SystemExit: If the scan has no tiles.
    """
    tiles, (tw, th) = _tile_grid(tiles_root)
    min_x = min(x for x, _ in tiles)
    min_y = min(y for _, y in tiles)
    cols = max(x for x, _ in tiles) - min_x + 1
    rows = max(y for _, y in tiles) - min_y + 1
    canvas_w, canvas_h = cols * tw, rows * th

    if cells_out.is_dir():
        for stale in cells_out.iterdir():
            stale.unlink()
    cells_out.mkdir(parents=True, exist_ok=True)

    n_cx = (canvas_w + chunk - 1) // chunk
    n_cy = (canvas_h + chunk - 1) // chunk
    cells: list[dict] = []
    for cy in range(n_cy):
        for cx in range(n_cx):
            x0, y0 = cx * chunk, cy * chunk
            cw = min(chunk, canvas_w - x0)
            ch = min(chunk, canvas_h - y0)
            canvas = np.zeros((ch, cw, 3), dtype=np.uint8)
            tx_lo, tx_hi = x0 // tw, (x0 + cw - 1) // tw
            ty_lo, ty_hi = y0 // th, (y0 + ch - 1) // th
            for ty in range(ty_lo, ty_hi + 1):
                for tx in range(tx_lo, tx_hi + 1):
                    path = tiles.get((tx + min_x, ty + min_y))
                    if path is None:
                        continue
                    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
                    if img is None:
                        print(f"  warning: undecodable tile {path}", file=sys.stderr)
                        continue
                    if img.shape[1] != tw or img.shape[0] != th:
                        img = cv2.resize(img, (tw, th), interpolation=cv2.INTER_AREA)
                    t_x0, t_y0 = tx * tw, ty * th
                    ox0, ox1 = max(x0, t_x0), min(x0 + cw, t_x0 + tw)
                    oy0, oy1 = max(y0, t_y0), min(y0 + ch, t_y0 + th)
                    if ox0 >= ox1 or oy0 >= oy1:
                        continue
                    canvas[oy0 - y0:oy1 - y0, ox0 - x0:ox1 - x0] = img[
                        oy0 - t_y0:oy1 - t_y0, ox0 - t_x0:ox1 - t_x0
                    ]
            cell_id = f"{cx:02d}x{cy:02d}"
            dst = cells_out / f"{cell_id}.jpg"
            cv2.imwrite(str(dst), canvas, [cv2.IMWRITE_JPEG_QUALITY, quality])
            cells.append({
                "id": cell_id,
                "x": x0,
                "y": y0,
                "width": cw,
                "height": ch,
                "source": str(dst.relative_to(cells_out.parent)),
            })
            if len(cells) % 20 == 0:
                print(f"  wrote {len(cells)}/{n_cx * n_cy} chunks", flush=True)

    print(
        f"combined {cols}x{rows} tiles of {tw}x{th} into {len(cells)} chunks "
        f"({n_cx}x{n_cy} grid of {chunk}px) on a {canvas_w}x{canvas_h} canvas "
        f"({canvas_w * canvas_h / 1e9:.2f} Gpx)"
    )
    return canvas_w, canvas_h, cells


def _merge_page(
    out: Path, name: str, canvas_w: int, canvas_h: int, cells: list[dict],
    book: str, book_name: str,
) -> Path:
    """Add/replace one page in ``out/manifest.json`` (creating the book).

    Args:
        out: Fixture folder holding ``manifest.json`` and the page folders.
        name: The page id (and chunk folder name).
        canvas_w: Page canvas width in pixels.
        canvas_h: Page canvas height in pixels.
        cells: The page's cell records.
        book: Book id to use when creating the manifest.
        book_name: Book display name to use when creating the manifest.

    Returns:
        The manifest path.
    """
    manifest_path = out / "manifest.json"
    manifest: dict = {}
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text())
            if isinstance(loaded, dict):
                manifest = loaded
        except (json.JSONDecodeError, OSError):
            manifest = {}
    # A legacy single-canvas manifest has no pages; start a fresh page list
    # (its old cells belong to whatever folder it referenced).
    pages = manifest.get("pages")
    if not isinstance(pages, list):
        pages = []
    page = {
        "id": name,
        "name": name,
        "canvas": {"width": canvas_w, "height": canvas_h},
        "cells": cells,
    }
    pages = [p for p in pages if not (isinstance(p, dict) and p.get("id") == name)]
    pages.append(page)
    out.mkdir(parents=True, exist_ok=True)
    tmp = manifest_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "book": manifest.get("book", book),
        "name": manifest.get("name", book_name),
        "pages": pages,
    }, indent=2))
    tmp.replace(manifest_path)  # atomic: reloads never see a half file
    print(f"wrote {manifest_path}: {len(pages)} page(s) [{[p['id'] for p in pages]}]")
    return manifest_path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tiles", type=Path, required=True,
                    help="scan folder holding <x>/<y>.jpg tiles")
    ap.add_argument("--name", default=None,
                    help="page id + chunk folder (default: tiles' parent folder slug)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help="fixture folder (manifest + per-page cell folders)")
    ap.add_argument("--chunk", type=int, default=DEFAULT_CHUNK,
                    help="cell edge length in canvas px (default 4096)")
    ap.add_argument("--quality", type=int, default=DEFAULT_QUALITY,
                    help="JPEG quality for the cell images (default 60)")
    ap.add_argument("--book", default=DEFAULT_BOOK,
                    help="book id (used when creating the manifest)")
    ap.add_argument("--book-name", default=DEFAULT_BOOK_NAME,
                    help="book display name (used when creating the manifest)")
    args = ap.parse_args()

    tiles = args.tiles
    if not tiles.is_dir():
        raise SystemExit(f"tiles folder not found: {tiles}")
    name = args.name or _slug(tiles.resolve().parent.name) or _slug(tiles.resolve().name)
    if not name or "/" in name:
        raise SystemExit(f"invalid page name: {name!r}")
    if args.chunk < 256:
        raise SystemExit("--chunk must be at least one tile (256)")

    cells_out = args.out / name
    canvas_w, canvas_h, cells = _combine(tiles, cells_out, args.chunk, args.quality)
    _merge_page(args.out, name, canvas_w, canvas_h, cells, args.book, args.book_name)


if __name__ == "__main__":
    main()
