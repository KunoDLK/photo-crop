"""Application configuration.

All runtime knobs are read from the environment (with sensible defaults) into a
single :class:`Settings` object, which is the only place that knows about env
var names. Dependents receive a ``Settings`` instance rather than reading env
vars themselves, so the service is trivially configurable under Docker.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Immutable service configuration loaded from the environment.

    Attributes:
        archive_root: Root of the book library (one subfolder per book).
        cache_dir: Directory backing the disk LRU of encoded tiles.
        cache_gb: Maximum encoded-tile cache size in GiB (the "last used X GB").
        page_cache_bytes: RAM budget for decoded page mipmaps.
        page_idle_seconds: Idle time after which a decoded page is unloaded.
        tile_size: Square tile edge length in pixels.
        jpeg_quality: JPEG quality (0-100) used for tile encoding.
        jpeg_progressive: Emit progressive (SOF2) JPEGs.
        opencl: Whether to attempt OpenCL-accelerated resampling.
        ocr_cache_dir: Directory backing the on-disk OCR result cache (JSON).
        ocr_max_dim: Long-edge pixel target OCR downscales pages to before
            Tesseract; ``0`` (default) disables downscaling entirely — full-res
            OCR, slower but more accurate on small/freeform text. Scans are
            huge, so a nonzero value trades accuracy for speed.
        ocr_lang: Tesseract language code(s), e.g. "eng".
        ocr_psm: Tesseract page segmentation mode: 3 = automatic page layout
            (good for structured columns), 6 = uniform text block, 11 =
            sparse/freeform text. Freeform scans read better with 11.
        ocr_conf_threshold: Minimum word confidence (0-100) to keep a word;
            freeform output runs at lower confidences, so 25-30 keeps more.
        ocr_enabled: Master switch for OCR. When false the OCR endpoints return
            empty results and the worker thread never starts — used by local
            dev/test instances that lack the OCR engine (without it, every OCR
            request would decode a full page and fail).
        rights_db_path: SQLite rights database; defaults to ``cache_dir/rights.db``.
        archive_username: Owner login name (env ``ARCHIVE_USERNAME``).
        archive_password: Owner password; empty disables owner login.
        session_secret: Session signing secret; empty auto-generates and
            persists one next to the rights DB so sessions survive restarts.
        session_cookie_secure: Mark the session cookie ``Secure`` (turn off
            for plain-http local development).
        login_rate_limit: Failed login attempts allowed per IP before a
            temporary lockout (5-minute window).
        default_region: ISO country code assumed when no ``CF-IPCountry``
            header is present (local development); empty → ``unknown`` zone.
        dev_region_header: Honor an ``X-Test-Region`` country header so curl
            checks can simulate regions (off by default; never enable in prod).
        blur_strength: Base Gaussian sigma of the whole-page blur plane; the
            effective source-space blur is 4 × ``blur_strength`` px regardless
            of the plane's cap level. Larger = less detail survives. The
            client adds its own dark banner for text readability.
        blur_levels_from_coarsest: The blur plane is built this many levels in
            from the coarsest pyramid level (default 3), capping its resolution
            so fine blur tiles are cheap upscales of one seamless plane.
        public_base_url: Absolute base URL of the public site (e.g.
            ``https://archive.example.com``) used for canonical links, Open
            Graph image URLs, and the sitemap. Empty (default) derives it from
            the request, trusting ``X-Forwarded-Proto``/``X-Forwarded-Host``
            from the reverse proxy (Cloudflare tunnel) and falling back to the
            request itself — which is unreliable when non-proxied requests
            (health checks, local testing) hit the origin and poison the
            sitemap cache with ``http://`` URLs. Set this in production.
        host: Bind address.
        port: Bind port.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    archive_root: Path = Path("/archive/Library")
    cache_dir: Path = Path("/archive/cache")
    cache_gb: float = 8.0
    page_cache_bytes: int = 6 * 1024 * 1024 * 1024
    page_idle_seconds: float = 10.0
    tile_size: int = 256
    jpeg_quality: int = 82
    jpeg_progressive: bool = True
    opencl: bool = True
    ocr_cache_dir: Path = Path("/archive/cache/ocr")
    ocr_max_dim: int = 0
    ocr_lang: str = "eng"
    ocr_psm: int = 11
    ocr_conf_threshold: int = 25
    ocr_enabled: bool = True
    rights_db_path: Path | None = None
    archive_username: str = "admin"
    archive_password: str = ""
    session_secret: str = ""
    session_cookie_secure: bool = True
    login_rate_limit: int = 10
    default_region: str = ""
    dev_region_header: bool = False
    blur_strength: float = 20.0
    blur_levels_from_coarsest: int = 3
    public_base_url: str = ""
    host: str = "0.0.0.0"
    port: int = 8000

    @property
    def cache_bytes(self) -> int:
        """Encoded-tile cache budget in bytes (derived from ``cache_gb``)."""
        return int(self.cache_gb * 1024 * 1024 * 1024)

    @property
    def rights_db(self) -> Path:
        """Location of the rights SQLite database (defaults under ``cache_dir``)."""
        return self.rights_db_path or self.cache_dir / "rights.db"
