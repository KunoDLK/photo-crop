"""OCR extraction and text search.

Runs Tesseract over decoded page images (downscaled for speed), caches results to
disk keyed by page version, and exposes per-page OCR plus a book-wide search
endpoint. Boxes are reported in source-pixel coordinates so the client can map
them through its own fit/zoom transform.
"""
