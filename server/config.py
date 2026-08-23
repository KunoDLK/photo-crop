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
    host: str = "0.0.0.0"
    port: int = 8000

    @property
    def cache_bytes(self) -> int:
        """Encoded-tile cache budget in bytes (derived from ``cache_gb``)."""
        return int(self.cache_gb * 1024 * 1024 * 1024)
