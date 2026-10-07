"""Ingest Copernicus Data Space Sentinel-2 mosaic zips into the archive.

Experiment tooling (dev only). Takes the quarterly cloud-free mosaic products
the user downloads from the Copernicus Browser (``Sentinel-2_mosaic_<q>_<tile>
_<x>_<y>.zip``, per-band 16-bit GeoTIFFs B02/B03/B04/B08) and converts each
into a single true-colour quality-60 progressive JPEG under
``dev/archive/mosaic/cells``, ready for the mosaic source.

Reflectance is stored as DN x 10000 with small negative nodata fillers; the
conversion clamps to [0,1], applies a display gamma, and rescales to 8-bit.
``index.json`` in the mosaic folder records each cell's file, tile id, and the
centroid of its geographic footprint (parsed from the product's
``userdata.json``), so the mosaic builder can place cells from neighbouring
UTM zones on one canvas.

Usage::

    python3 dev/satellite_ingest.py [download-dir]     # default ~/Downloads
"""
from __future__ import annotations

import argparse
import io
import json
import re
import zipfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

MOSAIC_DIR = Path(__file__).resolve().parent / "archive" / "mosaic"
CELLS_DIR = MOSAIC_DIR / "cells"
JPEG_QUALITY = 60
STEM_RE = re.compile(r"Sentinel-2_mosaic_(\d{4}_Q\d)_(\d{2}U[A-Z]{2})_(\d+)_(\d+)$")


def _read_band(zip_path: Path, stem: str, band: str) -> np.ndarray:
    """Decode one GeoTIFF band to float reflectance (0..1 scale)."""
    Image.MAX_IMAGE_PIXELS = None
    with zipfile.ZipFile(zip_path) as z:
        raw = z.read(f"{stem}/{band}.tif")
    with Image.open(io.BytesIO(raw)) as im:
        data = np.asarray(im).astype(np.float32)
    np.clip(data, 0.0, 10_000.0, out=data)  # nodata/negative fillers -> black
    return data / 10_000.0


def _to_8bit(reflectance: np.ndarray) -> np.ndarray:
    """Display curve: gamma brightens dark land so it reads like the archive."""
    return (np.power(reflectance, 1.0 / 2.2) * 255.0 + 0.5).astype(np.uint8)


def _centroid(zip_path: Path, stem: str) -> tuple[float, float]:
    """``(lon, lat)`` centre of the product's geographic footprint."""
    with zipfile.ZipFile(zip_path) as z:
        data = json.loads(z.read(f"{stem}/userdata.json"))
    ring = data["GeoFootprint"]["coordinates"][0]
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    return sum(lons) / len(lons), sum(lats) / len(lats)


def ingest(zip_path: Path) -> Path:
    """Convert one mosaic zip into a q60 JPEG in the mosaic cells folder.

    Args:
        zip_path: The downloaded ``Sentinel-2_mosaic_*_*.zip``.

    Returns:
        The written JPEG path.

    Raises:
        ValueError: If the zip name is not a recognised mosaic product.
    """
    stem = zip_path.stem
    match = STEM_RE.match(stem)
    if match is None:
        raise ValueError(f"unrecognised mosaic product: {zip_path.name}")
    out = CELLS_DIR / f"{stem}_q60.jpg"
    if out.is_file() and out.stat().st_mtime_ns >= zip_path.stat().st_mtime_ns:
        return out  # already converted from this source

    blue = _read_band(zip_path, stem, "B02")
    green = _read_band(zip_path, stem, "B03")
    red = _read_band(zip_path, stem, "B04")
    bgr = cv2.merge([_to_8bit(blue), _to_8bit(green), _to_8bit(red)])
    ok, buf = cv2.imencode(
        ".jpg", bgr,
        [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY, cv2.IMWRITE_JPEG_PROGRESSIVE, 1],
    )
    if not ok:
        raise SystemExit("jpeg encode failed")
    CELLS_DIR.mkdir(parents=True, exist_ok=True)
    out.write_bytes(buf.tobytes())
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", nargs="?", type=Path, default=Path.home() / "Downloads",
                    help="folder holding the downloaded mosaic zips")
    args = ap.parse_args()

    index_path = MOSAIC_DIR / "index.json"
    old_index = json.loads(index_path.read_text()) if index_path.is_file() else {}
    zips = sorted(args.source.glob("Sentinel-2_mosaic_*.zip"))
    if not zips:
        raise SystemExit(f"no Sentinel-2 mosaic zips found in {args.source}")
    index: dict[str, dict] = {}
    for zip_path in zips:
        stem = zip_path.stem
        match = STEM_RE.match(stem)
        if match is None:
            print(f"{zip_path.name}: unrecognised product, skipped", flush=True)
            continue
        try:
            out = ingest(zip_path)
            quarter, tile, px, py = match.groups()
            lon, lat = _centroid(zip_path, stem)
            index[f"{stem}_q60"] = {
                # Relative to the mosaic folder (manifest and cells sit together).
                "path": str(out.relative_to(MOSAIC_DIR)),
                "tile": tile,
                "part": f"{px}_{py}",
                "quarter": quarter.replace("_", " "),
                "lon": round(lon, 6),
                "lat": round(lat, 6),
            }
            print(f"{zip_path.name}: {out.name} ({out.stat().st_size/1e6:.1f} MB)", flush=True)
        except Exception as exc:  # noqa: BLE001 — keep going, one bad zip only
            print(f"{zip_path.name}: FAILED: {exc!r}", flush=True)
    # Keep previous cells whose source file still exists: the raw zips are
    # disposable after conversion, so deleting a product no longer removes its
    # cell — only deleting the cell JPEG does.
    for stem, entry in old_index.items():
        if stem not in index and (MOSAIC_DIR / entry["path"]).is_file():
            index[stem] = entry
    index_path.write_text(json.dumps(index, indent=2))
    print(f"index: {len(index)} cells")


if __name__ == "__main__":
    main()
