"""Tesseract extraction: image -> words and lines.

The single place that talks to Tesseract. It downscales the page (full-resolution
scans are far larger than Tesseract needs and make it slow), runs word-level
recognition, filters low-confidence/empty words, and scales every box back into
source-pixel coordinates. Words are grouped into lines by Tesseract's
(block, paragraph, line) ids so the client can lay out natural reading order.
"""
from __future__ import annotations

import cv2
import numpy as np
import pytesseract

from ..tiles import resampler


def ocr_image(img: np.ndarray, lang: str, max_dim: int, conf_threshold: int) -> dict:
    """Recognize text in a BGR page image.

    Args:
        img: Decoded page image as a ``(H, W, 3)`` uint8 BGR ndarray.
        lang: Tesseract language code(s), e.g. ``"eng"``.
        max_dim: Long-edge target (px) for the pre-OCR downscale.
        conf_threshold: Minimum word confidence (0-100) to keep a word.

    Returns:
        ``{"words": [...], "lines": [...]}`` where each word is
        ``{x, y, w, h, text, conf}`` and each line is ``{x, y, w, h, text}``,
        all in source-pixel coordinates.
    """
    src_h, src_w = img.shape[:2]
    scale = 1.0
    work = img
    long_edge = max(src_w, src_h)
    if long_edge > max_dim:
        scale = max_dim / long_edge
        out_w = max(1, round(src_w * scale))
        out_h = max(1, round(src_h * scale))
        work = resampler.resize_area(img, out_w, out_h, use_opencl=False)
    gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)

    data = pytesseract.image_to_data(
        gray,
        lang=lang,
        output_type=pytesseract.Output.DICT,
        config="--oem 3 --psm 3",
    )

    words: list[dict] = []
    line_groups: dict[tuple[int, int, int], list[dict]] = {}
    for i, raw in enumerate(data["text"]):
        text = (raw or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if conf < conf_threshold:
            continue
        w = int(data["width"][i])
        h = int(data["height"][i])
        if w <= 0 or h <= 0:
            continue
        word = {
            "x": round(int(data["left"][i]) / scale),
            "y": round(int(data["top"][i]) / scale),
            "w": round(w / scale),
            "h": round(h / scale),
            "text": text,
            "conf": conf,
        }
        words.append(word)
        key = (int(data["block_num"][i]), int(data["par_num"][i]), int(data["line_num"][i]))
        line_groups.setdefault(key, []).append(word)

    lines: list[dict] = []
    for key in sorted(line_groups):
        ws = sorted(line_groups[key], key=lambda w: w["x"])
        x0 = min(w["x"] for w in ws)
        y0 = min(w["y"] for w in ws)
        x1 = max(w["x"] + w["w"] for w in ws)
        y1 = max(w["y"] + w["h"] for w in ws)
        lines.append(
            {
                "x": x0,
                "y": y0,
                "w": x1 - x0,
                "h": y1 - y0,
                "text": " ".join(w["text"] for w in ws),
            }
        )

    return {"words": words, "lines": lines}
