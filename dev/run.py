"""Local dev server entry point: ``python -m dev.run``.

Generates a small sample archive (``dev/archive``) and seeds its rights, points
the server at repo-local state (``dev/cache``), and serves the viewer on
localhost only — the production Docker container on port 8471 is never touched.

Usage::

    python -m dev.run [--port 8765] [--force-sample] [--no-sample]

Notes:
    - OCR needs rapidocr (``pip install -r server/requirements.txt``); without
      it the viewer runs fine but OCR search returns no text.
    - Owner login for testing admin/shares: admin / devpass.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _banner(cfg, books) -> None:
    print(f"Book viewer (local dev)  ->  http://{cfg.host}:{cfg.port}")
    print(f"  archive: {cfg.archive_root}  ({len(books)} books: {', '.join(books)})")
    print(f"  cache:   {cfg.cache_dir}")
    print("  owner:   admin / devpass    (admin UI, share links)")
    print("  prod container on :8471 is untouched")


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the book viewer locally (dev only).")
    ap.add_argument("--port", type=int, default=8765, help="bind port (default 8765)")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    ap.add_argument("--force-sample", action="store_true", help="regenerate sample books")
    ap.add_argument("--no-sample", action="store_true", help="skip sample data generation")
    args = ap.parse_args()

    # Package-relative imports (server.*) require the repo root on sys.path.
    sys.path.insert(0, str(REPO_ROOT))
    os.chdir(REPO_ROOT)

    from dev import config, sample_data  # noqa: PLC0415 (import after path setup)

    cfg = config.LocalDev(REPO_ROOT, host=args.host, port=args.port)
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)

    books: list[str] = []
    if not args.no_sample:
        books = sample_data.ensure(cfg.archive_root, cfg.cache_dir, force=args.force_sample)

    # Apply every local override BEFORE the app module is imported, so the
    # module-level `create_app(Settings())` in server/main.py sees them.
    os.environ.update(cfg.env())

    import uvicorn  # noqa: PLC0415

    _banner(cfg, books)
    uvicorn.run("server.main:app", host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
