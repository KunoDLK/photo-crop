"""Local dev instance configuration.

Holds every local-only path and environment override in one place. Nothing in
this module touches the production archive (``/archive``) or its Docker
container; the server is pointed at a repo-local archive/cache instead and the
only secrets defined here are throwaway dev credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


@dataclass(frozen=True)
class LocalDev:
    """Paths + environment for one local instance of the viewer.

    Args:
        root: Repo root; all generated state lives under ``root/dev``.
        host: Bind address (localhost only by default).
        port: Bind port (the prod container listens on 8471, so the default
            never collides).
    """

    root: Path
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    @property
    def archive_root(self) -> Path:
        """Book library root: one subfolder per book (sample books generated here)."""
        return self.root / "dev" / "archive" / "Library"

    @property
    def cache_dir(self) -> Path:
        """Tile/OCR/rights cache root, kept entirely under the repo."""
        return self.root / "dev" / "cache"

    def env(self) -> dict[str, str]:
        """Environment overrides applied before the server boots.

        Every knob points at repo-local state; the owner credentials are
        throwaway dev values so the admin/share flows can be exercised.
        """
        return {
            "ARCHIVE_ROOT": str(self.archive_root),
            "CACHE_DIR": str(self.cache_dir),
            "OCR_CACHE_DIR": str(self.cache_dir / "ocr"),
            "RIGHTS_DB_PATH": str(self.cache_dir / "rights.db"),
            # Plain-http localhost: no Secure cookies, region header enabled.
            "SESSION_COOKIE_SECURE": "false",
            "DEV_REGION_HEADER": "true",
            "DEFAULT_REGION": "gb",
            # Absolute base URL so canonical/OG links are sane locally.
            "PUBLIC_BASE_URL": f"http://{self.host}:{self.port}",
            # Owner login for testing admin + share links.
            "ARCHIVE_USERNAME": "admin",
            "ARCHIVE_PASSWORD": "devpass",
            # Deterministic CPU rendering, small caches.
            "OPENCL": "false",
            "CACHE_GB": "1",
            "PAGE_CACHE_BYTES": str(512 * 1024 * 1024),
            # No rapidocr on the dev machine: OCR off, so the endpoints return
            # empty instead of decoding a full page and 500ing.
            "OCR_ENABLED": "false",
            "HOST": self.host,
            "PORT": str(self.port),
        }
