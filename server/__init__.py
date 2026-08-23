"""Server-side tiled book viewer.

This package implements a FastAPI service that lists scanned books/pages and
serves progressive-JPEG image tiles cropped and resampled on demand, with a
GPU-agnostic (OpenCL/CPU) resampling backend and a byte-limited encoded-tile
cache.
"""
