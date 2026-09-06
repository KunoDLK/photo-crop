"""Build the wafer-scale mosaic test fixture from the satellite cells.

Experiment tooling (dev only). Takes the converted Sentinel-2 cell JPEGs in
``dev/archive/mosaic/cells`` and lays **copies** of them out on one giant
virtual canvas as an ``N x N`` grid (default 31x31 = 961 cells, a
"wafer-scale" stress test for the mosaic source's proxy-pyramid tile path;
see ``docs/wafer-mosaic-scaling.md``). Each grid slot copies one of the base
images into ``dev/archive/wafer/cells`` (cycling the base set so the canvas
isn't one repeated image) and the manifest records the grid under its own
book id (``wafer-mosaic``), so the demo mosaic and the wafer fixture can be
served side by side by the dev instance.

The fixture is intentionally *copies*, not references: hundreds of very large
source files on one canvas is exactly the scenario the proxy chain design
targets, and every cell keeps its own file so individual cells can be
re-saved (mtime bump) to test version invalidation.

``--prewarm`` additionally fills the dev tile cache (``dev/cache/cache.db``)
with every cell's proxy chain. Chains are a pure function of (source file,
manifest version), so the offline tool decodes each **distinct** source once
and writes the per-cell rows — 11 decodes instead of 961 — making the first
whole-canvas view instant when the dev server boots.

Usage::

    python3 dev/wafer_mosaic.py [--grid 31] [--prewarm]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

import cv2

REPO_ROOT = Path(__file__).resolve().parent.parent
DEV_ROOT = REPO_ROOT / "dev"
DEFAULT_OUT = DEV_ROOT / "archive" / "wafer"
DEFAULT_CELLS = DEV_ROOT / "archive" / "mosaic" / "cells"
DEFAULT_CACHE = DEV_ROOT / "cache"
TILE_SIZE = 256  # must match server/config.py and client config.js
TILE_RE = re.compile(r"(?:^|_)(\d{2}U[A-Z]{2})(?:_|$)")


def _sorted_cells(cells_dir: Path) -> list[Path]:
    """The base cell JPEGs, name-sorted (geographic-ish west-east order)."""
    return sorted(p for p in cells_dir.glob("*.jpg") if p.is_file())


def _cell_dims(paths: list[Path]) -> tuple[int, int]:
    """Header-only size check: every base cell must share one size."""
    from PIL import Image  # header-only open, no decode

    size: tuple[int, int] | None = None
    for path in paths:
        with Image.open(path) as im:
            if size is None:
                size = im.size
            elif im.size != size:
                raise SystemExit(
                    f"base cell sizes differ: {size} vs {path} {im.size}; "
                    "the wafer grid assumes uniform square cells"
                )
    assert size is not None
    return size


def build(out: Path, cells_dir: Path, grid: int) -> Path:
    """Copy the base cells onto an ``grid x grid`` canvas and write the manifest.

    Args:
        out: Output folder (``manifest.json`` + ``cells/`` inside).
        cells_dir: Folder holding the base cell JPEGs.
        grid: Cells per side (``grid * grid`` total cells).

    Returns:
        The written manifest path.
    """
    base = _sorted_cells(cells_dir)
    if not base:
        raise SystemExit(f"no base cell JPEGs in {cells_dir}")
    width, height = _cell_dims(base)
    if width != height:
        raise SystemExit(f"base cells must be square, got {width}x{height}")

    cells_out = out / "cells"
    if cells_out.is_dir():
        for stale in cells_out.iterdir():
            stale.unlink()
    cells_out.mkdir(parents=True, exist_ok=True)

    cells: list[dict] = []
    total = grid * grid
    for r in range(grid):
        for c in range(grid):
            src = base[(r * grid + c) % len(base)]
            tile = TILE_RE.search(src.name)
            tag = tile.group(1) if tile else f"cell{r:02d}c{c:02d}"
            cell_id = f"{tag}_{r:02d}x{c:02d}"
            dst = cells_out / f"{cell_id}.jpg"
            shutil.copy2(src, dst)  # preserves mtime: version stays stable
            cells.append({
                "id": cell_id,
                "x": c * width,
                "y": r * height,
                "width": width,
                "height": height,
                # Relative to the manifest, which lives beside the cells.
                "source": str(dst.relative_to(out)),
            })
            if (r * grid + c + 1) % 100 == 0:
                print(f"  copied {r * grid + c + 1}/{total}", flush=True)

    version = max((p.stat().st_mtime_ns for p in cells_out.iterdir()), default=0)
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "manifest.json.tmp"
    tmp.write_text(json.dumps({
        "version": version,
        "book": "wafer-mosaic",
        "name": f"Wafer mosaic {grid}x{grid}",
        "canvas": {"width": grid * width, "height": grid * height},
        "cells": cells,
    }, indent=2))
    tmp.replace(out / "manifest.json")  # atomic: reloads never see a half file
    print(
        f"wrote {out / 'manifest.json'}: {total} cells on a {grid * width}x{grid * height}"
        f" canvas ({total * width * height / 1e9:.1f} Gpx), {len(base)} base images copied"
    )
    return out / "manifest.json"


def prewarm(manifest_path: Path, cache_dir: Path) -> None:
    """Fill every cell's proxy chain in the dev tile store (O1/O3).

    Decodes each distinct source once and writes each of its cells' plane rows
    (identical bytes, per-cell geometry) through the same store API the server
    uses, so the first whole-canvas render finds every chain ready.

    Args:
        manifest_path: The wafer ``manifest.json``.
        cache_dir: Dev cache directory holding ``cache.db``.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from server.sources.mosaic import (  # noqa: PLC0415 — import after path setup
        PROVIDER_NS, MosaicManifest, build_chain_planes, plane_geometry,
    )
    from server.tiles.sqlite_cache import TileCache  # noqa: PLC0415

    manifest = MosaicManifest.load(manifest_path)
    store = TileCache(cache_dir, 8 * 1024 * 1024 * 1024)
    # Group by content-identical source: the fixture copies one base image per
    # grid slot, so all cells whose id shares the base tile tag decode once.
    by_source: dict[str, list] = {}
    for cell in manifest.cells:
        tag = TILE_RE.search(cell.id)
        by_source.setdefault(tag.group(1) if tag else str(cell.source), []).append(cell)

    done = 0
    for tag, cells in by_source.items():
        source = cells[0].source
        full = cv2.imread(str(source), cv2.IMREAD_COLOR)
        if full is None:
            raise SystemExit(f"undecodable cell source: {source}")
        # All cells of one tag are byte-identical copies: halve and encode the
        # plane pyramid once, then write each cell's rows (per-cell geometry).
        planes_by_level: dict[int, tuple[int, bytes]] | None = None
        for cell in cells:
            band = frozenset(
                level for level in range(manifest.max_level + 1)
                if max(cell.width, cell.height) < (TILE_SIZE << level)
            )
            if not band:
                continue  # whole-cell canvas level behaviour: no chain needed
            if planes_by_level is None:
                planes_by_level = build_chain_planes(full, band)
            planes = planes_by_level
            chain_id = (
                f"proxy/{PROVIDER_NS}/{manifest.book}/mosaic/{manifest.version}/{cell.id}"
            )
            rows = []
            for level, (plane_w, png) in sorted(planes.items()):
                tx0, tx1, ty0, ty1, expected = plane_geometry(cell, TILE_SIZE, level)
                rows.append((level, plane_w, png, tx0, tx1, ty0, ty1, expected))
            store.put_chain(chain_id, PROVIDER_NS, manifest.book, "mosaic",
                            manifest.version, rows)
            done += 1
    print(
        f"prewarmed {done} chains ({store.chain_count} in store) from "
        f"{len(by_source)} distinct decodes into {store.db_path}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--grid", type=int, default=31, help="cells per side (default 31)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="fixture folder")
    ap.add_argument("--cells-dir", type=Path, default=DEFAULT_CELLS,
                    help="folder of base satellite cell JPEGs")
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE,
                    help="dev cache folder holding cache.db")
    ap.add_argument("--prewarm", action="store_true",
                    help="build every proxy chain in the dev tile cache")
    ap.add_argument("--skip-copy", action="store_true",
                    help="only (re)write the manifest from existing cell files")
    args = ap.parse_args()
    if args.grid < 2:
        raise SystemExit("--grid must be at least 2")

    if not args.skip_copy:
        manifest_path = build(args.out, args.cells_dir, args.grid)
    else:
        manifest_path = args.out / "manifest.json"
        if not manifest_path.is_file():
            raise SystemExit(f"no existing fixture manifest at {manifest_path}")
        print(f"reusing existing cells under {args.out}")
    if args.prewarm:
        prewarm(manifest_path, args.cache_dir)


if __name__ == "__main__":
    main()
