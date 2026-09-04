"""Fetch a 3x3 block of Sentinel-2 tiles and store each as a quality-60 JPEG.

Experiment tooling (dev only, gitignored): for each 100km square of the block,
find its clearest recent acquisition by byte size of the 10m true-colour
composite (TCI.jp2 — clouds compress to almost nothing, clear land does not),
download it from the AWS open-data bucket, decode with OpenJPEG (opj_decompress,
far faster than Pillow for JP2), and re-encode as a progressive quality-60 JPEG
so the mosaic demo keeps source files small. Raw JP2s and intermediate PNGs are
deleted after conversion; an index.json records what was fetched.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import cv2

BASE = "https://sentinel-s2-l2a.s3.amazonaws.com/"
NS = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
OUT_DIR = Path(__file__).resolve().parent / "satellite"
SQUARES = [f"30U{c}{r}" for c in "WXY" for r in "BCD"]  # 3x3 block incl 30UYC
RECENT_YEARS = ("2026", "2025")


def _get(url: str) -> ET.Element:
    with urllib.request.urlopen(url, timeout=60) as r:
        return ET.fromstring(r.read())


def common_prefixes(prefix: str) -> list[str]:
    url = BASE + f"?list-type=2&prefix={prefix}&delimiter=/&max-keys=1000"
    root = _get(url)
    return [p.findtext("s:Prefix", "", NS) for p in root.findall("s:CommonPrefixes", NS)]


def tci_size(day_prefix: str) -> int | None:
    """Byte size of R10m/TCI.jp2 for one acquisition, or None if absent."""
    url = BASE + f"?list-type=2&prefix={day_prefix}R10m/&max-keys=50"
    root = _get(url)
    for c in root.findall("s:Contents", NS):
        if c.findtext("s:Key", "", NS).endswith("/TCI.jp2"):
            return int(c.findtext("s:Size", "0", NS))
    return None


def best_day(square: str) -> tuple[str, int] | None:
    """Clearest recent acquisition for a square: (day_prefix, tci_bytes)."""
    base = f"tiles/{square[0:2]}/{square[2]}/{square[3:5]}/"
    best: tuple[str, int] | None = None
    for year in RECENT_YEARS:
        months = sorted(
            (int(p.rsplit("/", 2)[-2]) for p in common_prefixes(base + year + "/") if p),
        )
        for month in months[-3:]:
            days = sorted(
                (int(p.rsplit("/", 2)[-2]) for p in common_prefixes(f"{base}{year}/{month}/") if p),
            )
            for day in days[-6:]:
                prefix = f"{base}{year}/{month}/{day}/0/"
                size = tci_size(prefix)
                if size:
                    date = f"{year}-{month:02d}-{day:02d}"
                    if best is None or size > best[1]:
                        best = (prefix, size)
    return best


def convert_to_jpeg(square: str, date: str, jp2_path: Path) -> Path:
    """JP2 -> PNG (opj_decompress) -> progressive q60 JPEG (cv2)."""
    png = jp2_path.with_suffix(".png")
    subprocess.run(
        ["opj_decompress", "-i", str(jp2_path), "-o", str(png)],
        check=True, capture_output=True,
    )
    img = cv2.imread(str(png))  # BGR
    if img is None:
        raise RuntimeError(f"cv2 could not read {png}")
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 60, cv2.IMWRITE_JPEG_PROGRESSIVE, 1])
    if not ok:
        raise RuntimeError("jpeg encode failed")
    jpg = OUT_DIR / f"{square}_{date}_q60.jpg"
    jpg.write_bytes(buf.tobytes())
    return jpg


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    index: dict[str, dict] = {}
    for square in SQUARES:
        start = time.time()
        try:
            day = best_day(square)
            if day is None:
                print(f"{square}: no recent acquisition found", flush=True)
                continue
            prefix, size = day
            parts = prefix.split("/")
            date = f"{parts[4]}-{parts[5]}-{parts[6]}"
            url = BASE + prefix + "R10m/TCI.jp2"
            jp2 = OUT_DIR / f"{square}_{date}_TCI.jp2"
            print(f"{square}: downloading {date} TCI ({size/1e6:.1f} MB)...", flush=True)
            t0 = time.time()
            with urllib.request.urlopen(url, timeout=300) as r, open(jp2, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            print(f"  downloaded in {time.time()-t0:.0f}s, converting...", flush=True)
            jpg = convert_to_jpeg(square, date, jp2)
            jp2.unlink()
            jpg.with_suffix(".png").unlink(missing_ok=True)
            index[square] = {"path": str(jpg.relative_to(OUT_DIR.parent)), "date": date,
                             "bytes": jpg.stat().st_size}
            print(f"  -> {jpg.name} ({jpg.stat().st_size/1e6:.1f} MB) in {time.time()-start:.0f}s", flush=True)
        except Exception as exc:  # noqa: BLE001 — keep the batch going
            print(f"{square}: FAILED: {exc!r}", flush=True)
    (OUT_DIR / "index.json").write_text(json.dumps(index, indent=2))
    print("done:", json.dumps(index, indent=2), flush=True)


if __name__ == "__main__":
    main()
