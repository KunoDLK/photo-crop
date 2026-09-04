"""Build the mosaic source manifest from downloaded satellite tiles.

Experiment tooling (dev only). Reads ``dev/satellite/index.json`` (produced by
:mod:`dev.satellite_fetch`) and lays the 3x3 block of quality-60 JPEGs out on
one virtual canvas: columns W, X, Y run west to east, rows D, C, B run north to
south (so the previously downloaded 30UYC sits on the canvas).

The manifest records the canvas size, each cell's placement, its source image,
and a stable version (the newest source-file mtime), which also namespaces
cached mosaic tiles. No derived files are written: the tile server renders
every zoom level from the source images and relies on its tile cache for
repeat views, so re-running this script (or editing it) only rewrites the
manifest.

Usage::

    python3 dev/mosaic_build.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEV = Path(__file__).resolve().parent
SATELLITE = DEV / "satellite"
OUT_DIR = DEV / "mosaic"

#: Layout of the 3x3 block on the canvas: rows north-to-south, columns west-to-east.
ROWS = ("D", "C", "B")
COLS = ("W", "X", "Y")
CELL_SIZE = 10980  # Sentinel-2 granules at 10 m


def main() -> None:
    index = json.loads((SATELLITE / "index.json").read_text())
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    cells: list[dict] = []
    version = 0
    for row, row_id in enumerate(ROWS):
        for col, col_id in enumerate(COLS):
            tile_id = f"30U{col_id}{row_id}"
            entry = index.get(tile_id)
            if entry is None:
                print(f"skip {tile_id}: not in dev/satellite/index.json", flush=True)
                continue
            src = (DEV / entry["path"]).resolve()
            if not src.is_file():
                raise SystemExit(f"missing source image: {src}")
            version = max(version, src.stat().st_mtime_ns)
            cells.append({
                "id": tile_id,
                "x": col * CELL_SIZE,
                "y": row * CELL_SIZE,
                "width": CELL_SIZE,
                "height": CELL_SIZE,
                # Relative to the manifest file so the layout is portable.
                "source": str(src.relative_to(OUT_DIR) if src.is_relative_to(OUT_DIR)
                              else os.path.relpath(src, OUT_DIR)),
            })

    width = height = len(COLS) * CELL_SIZE
    manifest_path = OUT_DIR / "manifest.json"
    manifest_path.write_text(json.dumps({
        "version": version,
        "canvas": {"width": width, "height": height},
        "cells": cells,
    }, indent=2))
    print(f"wrote {manifest_path} ({len(cells)} cells, canvas {width}x{height})")


if __name__ == "__main__":
    main()
