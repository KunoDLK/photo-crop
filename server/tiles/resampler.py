"""GPU-agnostic area resampling backend.

This is the only module aware of acceleration hardware. When OpenCL is available
(and enabled) resizes run through ``cv2.UMat`` (works on NVIDIA, AMD, Intel, and
CPU pocl devices); otherwise it transparently falls back to CPU ``INTER_AREA``.
Every caller uses :func:`resize_area` and never checks the backend itself.
"""
from __future__ import annotations

import cv2
import numpy as np


def opencl_available() -> bool:
    """Return True when an OpenCL device can be used for acceleration.

    Returns:
        True if OpenCV reports an OpenCL backend and a usable device exists.
    """
    try:
        return bool(cv2.ocl.haveOpenCL() and cv2.ocl.useOpenCL())
    except Exception:  # noqa: BLE001 — OpenCL probing must never raise
        return False


def resize_area(src: np.ndarray, out_w: int, out_h: int, use_opencl: bool = True) -> np.ndarray:
    """Resample ``src`` to ``(out_w, out_h)`` with area averaging.

    Area resampling ensures every source pixel contributes (full super-sampling),
    matching the anti-aliasing of the old client-side progressive halving.

    Args:
        src: Input image (H, W, C) uint8.
        out_w: Output width in pixels.
        out_h: Output height in pixels.
        use_opencl: Attempt OpenCL acceleration before falling back to CPU.

    Returns:
        The resampled image.
    """
    out_w = max(1, int(out_w))
    out_h = max(1, int(out_h))
    if use_opencl and opencl_available():
        try:
            umat = cv2.UMat(src)
            res = cv2.resize(umat, (out_w, out_h), interpolation=cv2.INTER_AREA)
            return res.get()
        except Exception:  # noqa: BLE001 — fall back to CPU on any OpenCL issue
            pass
    return cv2.resize(src, (out_w, out_h), interpolation=cv2.INTER_AREA)
