"""Build the mosaic source manifest from the satellite cells.

Experiment tooling (dev only). Reads ``dev/archive/mosaic/index.json`` (written
by :mod:`dev.satellite_ingest` from Copernicus Data Space mosaic downloads) and
lays the converted quality-60 JPEGs out on one virtual canvas as a grid of
adjacent Sentinel-2 tiles.

Placement is by MGRS letters while every cell shares one UTM zone (columns
west to east, rows north to south, north at the top). When the set spans
zones, letters restart per zone so cells are positioned from the geographic
centroids in ``index.json`` instead: an in-zone letter grid anchors the axes
and out-of-zone cells are solved onto that grid.

The manifest records the canvas size, each cell's placement, its source image,
and a stable version (the newest source-file mtime), which also namespaces
cached mosaic tiles. No derived files are written: the tile server renders
every zoom level from the source images and relies on its tile cache for
repeat views, so re-running this script only rewrites the manifest.

Usage::

    python3 dev/mosaic_build.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from PIL import Image

MOSAIC_DIR = Path(__file__).resolve().parent / "archive" / "mosaic"
CELLS_DIR = MOSAIC_DIR / "cells"
MANIFEST_PATH = MOSAIC_DIR / "manifest.json"
CELL_PX = 10008  # product pixels per granule


def _place_single_zone(cells: list[dict]) -> list[dict]:
    """Letter-grid placement for a set within one UTM zone."""
    cols = sorted({c["col"] for c in cells})
    rows = sorted({c["row"] for c in cells})
    placed = []
    for cell in cells:
        placed.append({
            "id": cell["id"],
            "x": cols.index(cell["col"]) * CELL_PX,
            # Highest row letter is the northernmost tile: put it at the top.
            "y": (len(rows) - 1 - rows.index(cell["row"])) * CELL_PX,
            "width": CELL_PX,
            "height": CELL_PX,
            "source": cell["source"],
        })
    return placed


def _local_meters(lon: float, lat: float, ref: tuple[float, float]) -> tuple[float, float]:
    """Planar metres from a reference point (equirectangular, zone-agnostic)."""
    lon0, lat0 = ref
    coslat = math.cos(math.radians(lat0))
    x = (lon - lon0) * 111_320.0 * coslat
    y = (lat - lat0) * 111_132.0
    return x, y


def _place_multi_zone(cells: list[dict]) -> list[dict]:
    """Geographic placement when cells span UTM zones.

    A reference zone's letter grid (cells sharing that zone) defines the
    column/row axes; each out-of-zone cell is solved onto those axes from its
    centroid and snapped to the nearest grid slot.

    Args:
        cells: Cells with ``lon``/``lat`` centroids and letter fields.

    Returns:
        Placed cells with pixel ``x``/``y``.

    Raises:
        SystemExit: If the reference zone lacks the neighbours needed to
            define the axes.
    """
    zones = sorted({c["tile"][:2] for c in cells})
    ref_zone = zones[0]
    ref = [c for c in cells if c["tile"][:2] == ref_zone]
    cols = sorted({c["col"] for c in ref})
    rows = sorted({c["row"] for c in ref})
    col_num = {col: i for i, col in enumerate(cols)}
    row_num = {row: i for i, row in enumerate(rows)}

    # One grid step east, from a same-row pair of adjacent columns.
    e_steps: list[tuple[float, float]] = []
    for a in ref:
        for b in ref:
            if a["row"] == b["row"] and col_num[b["col"]] == col_num[a["col"]] + 1:
                e_steps.append((b["lon"] - a["lon"], b["lat"] - a["lat"]))
    # One grid step north, from a same-column pair of adjacent rows.
    n_steps: list[tuple[float, float]] = []
    for a in ref:
        for b in ref:
            if a["col"] == b["col"] and row_num[b["row"]] == row_num[a["row"]] + 1:
                n_steps.append((b["lon"] - a["lon"], b["lat"] - a["lat"]))
    if not e_steps or not n_steps:
        raise SystemExit(
            "multi-zone mosaic needs two same-row columns and two same-column "
            "rows within one zone to anchor the grid"
        )
    e = (sum(s[0] for s in e_steps) / len(e_steps), sum(s[1] for s in e_steps) / len(e_steps))
    n = (sum(s[0] for s in n_steps) / len(n_steps), sum(s[1] for s in n_steps) / len(n_steps))

    # Solve [c, r] such that (lon, lat) = anchor + c*e + r*n, then map every
    # cell (in-zone cells land on their letter indices exactly).
    def solve(cell: dict, anchor: dict) -> tuple[int, int]:
        p = (cell["lon"] - anchor["lon"], cell["lat"] - anchor["lat"])
        det = e[0] * n[1] - e[1] * n[0]
        c = (p[0] * n[1] - p[1] * n[0]) / det
        r = (e[0] * p[1] - e[1] * p[0]) / det
        c0 = col_num[anchor["col"]]
        r0 = row_num[anchor["row"]]
        return c0 + round(c), r0 + round(r)

    anchor = min(
        (c for c in ref if c["col"] in col_num and c["row"] in row_num),
        key=lambda c: (col_num[c["col"]], row_num[c["row"]]),
    )
    grid: dict[tuple[int, int], dict] = {}
    for cell in cells:
        if cell["tile"][:2] == ref_zone:
            idx = (col_num[cell["col"]], row_num[cell["row"]])
        else:
            idx = solve(cell, anchor)
        if idx in grid:
            raise SystemExit(f"cells {grid[idx]['id']} and {cell['id']} land on the same slot {idx}")
        grid[idx] = cell
    top_row = max(r for _, r in grid)
    placed = []
    for (ci, ri), cell in grid.items():
        placed.append({
            "id": cell["id"],
            "x": ci * CELL_PX,
            "y": (top_row - ri) * CELL_PX,  # north (high row) at the top
            "width": CELL_PX,
            "height": CELL_PX,
            "source": cell["source"],
        })
    return placed


def main() -> None:
    index = json.loads((MOSAIC_DIR / "index.json").read_text())
    cells: list[dict] = []
    version = 0
    size: tuple[int, int] | None = None
    for stem, entry in index.items():
        if entry.get("part") != "0_0":
            print(f"skip {stem}: part {entry.get('part')} has no grid placement", flush=True)
            continue
        src = (MOSAIC_DIR / entry["path"]).resolve()
        if not src.is_file():
            raise SystemExit(f"missing source image: {src}")
        tile = entry["tile"]  # e.g. 30UWF: zone 30, column W, row F
        with Image.open(src) as im:
            dims = im.size
        if size is None:
            size = dims
        elif size != dims:
            raise SystemExit(f"cell sizes differ: {size} vs {src} {dims}")
        version = max(version, src.stat().st_mtime_ns)
        cells.append({
            "id": tile,
            "tile": tile,
            "col": tile[3],
            "row": tile[4],
            "lon": float(entry["lon"]),
            "lat": float(entry["lat"]),
            # Relative to the manifest, which lives beside the cells.
            "source": str(src.relative_to(MOSAIC_DIR)),
        })
    if not cells:
        raise SystemExit("no whole mosaic tiles in dev/archive/mosaic/index.json")

    if len({c["tile"][:2] for c in cells}) == 1:
        placed = _place_single_zone(cells)
    else:
        placed = _place_multi_zone(cells)

    canvas_w = max(c["x"] + c["width"] for c in placed)
    canvas_h = max(c["y"] + c["height"] for c in placed)
    out_cells = [{k: c[k] for k in ("id", "x", "y", "width", "height", "source")} for c in placed]
    MOSAIC_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "version": version,
        "canvas": {"width": canvas_w, "height": canvas_h},
        "cells": out_cells,
    }, indent=2))
    tmp.replace(MANIFEST_PATH)  # atomic: reloads never see a half-written file
    ids = ", ".join(f"{c['id']}@({c['x']},{c['y']})" for c in placed)
    print(f"wrote {MANIFEST_PATH} ({len(placed)} cells, canvas {canvas_w}x{canvas_h})")
    print("  " + ids)


if __name__ == "__main__":
    main()
