"""RapidOCR extraction: image -> words and lines.

The single place that talks to an OCR engine. This replaced Tesseract with
RapidOCR, which runs PaddleOCR's PP-OCRv4 detection + recognition models via
ONNX Runtime. Its DBNet text detector handles freeform/scattered layouts far
better than Tesseract's page segmentation, and it runs on CPU with no PyTorch
dependency. Model files ship inside the pip wheel, so the container needs no
runtime downloads.

RapidOCR detects text regions (typically whole lines) and recognizes each
one; every region is returned as a 4-corner box in original-image pixels.
Those become our "lines". Each line's text is then split into words and each
word gets a box interpolated proportionally across the line box, so the
client's word-based search highlighting keeps working. Confidence scores
(0-1 softmax) are kept on Tesseract's 0-100 scale.

The engine instance is a lazy module-level singleton: construction loads the
ONNX models (~1 s), which must not happen at import time, and the OCR worker
is a single thread so sharing one engine is safe.
"""
from __future__ import annotations

import threading

import numpy as np

from ..tiles import resampler

_engine = None
_engine_lock = threading.Lock()


def _get_engine():
    """Return the process-wide RapidOCR engine, loading it on first use."""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from rapidocr import RapidOCR

                _engine = RapidOCR()
    return _engine


def _region_words(text: str, x0: int, y0: int, x1: int, y1: int, conf: float) -> list[dict]:
    """Split one recognized region into words with proportionally split boxes.

    RapidOCR recognizes whole lines, so per-word boxes have to be estimated:
    each token gets a width proportional to its character count (including
    the inter-word spaces), which leaves natural gaps between words.
    """
    tokens = text.split()
    if not tokens or x1 <= x0:
        return []
    total_chars = len(text)
    avail = x1 - x0
    words: list[dict] = []
    cx = x0
    for tok in tokens:
        w = max(1, round(avail * len(tok) / total_chars))
        words.append(
            {
                "x": cx,
                "y": y0,
                "w": w,
                "h": y1 - y0,
                "text": tok,
                "conf": conf,
            }
        )
        cx += round(avail * (len(tok) + 1) / total_chars)
    return words


def ocr_image(
    img: np.ndarray, lang: str, max_dim: int, conf_threshold: int, psm: int = 3
) -> dict:
    """Recognize text in a BGR page image.

    Args:
        img: Decoded page image as a ``(H, W, 3)`` uint8 BGR ndarray.
        lang: Unused by RapidOCR (retained for call-site compatibility).
        max_dim: Long-edge target (px) for the pre-OCR downscale; ``0``
            disables downscaling entirely. RapidOCR resizes internally, so
            this only matters for very large scans.
        conf_threshold: Minimum word confidence (0-100) to keep a word.
        psm: Unused by RapidOCR (Tesseract-only, retained for compatibility).

    Returns:
        ``{"words": [...], "lines": [...]}`` where each word is
        ``{x, y, w, h, text, conf}`` and each line is ``{x, y, w, h, text}``,
        all in source-pixel coordinates.
    """
    src_h, src_w = img.shape[:2]
    scale = 1.0
    work = img
    long_edge = max(src_w, src_h)
    if max_dim > 0 and long_edge > max_dim:
        scale = max_dim / long_edge
        out_w = max(1, round(src_w * scale))
        out_h = max(1, round(src_h * scale))
        work = resampler.resize_area(img, out_w, out_h, use_opencl=False)

    result = _get_engine()(work, text_score=conf_threshold / 100)
    if not result or result.boxes is None:
        return {"words": [], "lines": []}

    # Reading order: top-to-bottom rows, left-to-right within a row.
    boxes: np.ndarray = result.boxes  # (N, 4, 2)
    txts = result.txts or ()
    scores = result.scores or ()
    order = sorted(
        range(len(boxes)),
        key=lambda i: (round(float(boxes[i][:, 1].mean()) / 40), float(boxes[i][:, 0].min())),
    )

    words: list[dict] = []
    lines: list[dict] = []
    for i in order:
        text = (txts[i] or "").strip()
        if not text:
            continue
        conf = round(float(scores[i]) * 100, 1)
        if conf < conf_threshold:
            continue
        quad = boxes[i] / scale
        x0 = int(quad[:, 0].min())
        y0 = int(quad[:, 1].min())
        x1 = int(quad[:, 0].max())
        y1 = int(quad[:, 1].max())
        if x1 <= x0 or y1 <= y0:
            continue
        lines.append({"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "text": text})
        words.extend(_region_words(text, x0, y0, x1, y1, conf))

    return {"words": words, "lines": lines}
