"""Client region detection and policy-zone mapping.

The production traffic passes through the Cloudflare tunnel, so the country is
read from the ``CF-IPCountry`` header (present on every proxied request);
``DEFAULT_REGION`` env covers local development, and a dev-only test header
(off by default) lets curl scripts simulate any region. Country codes map to
policy zones: ``US`` → us, ``GB``/``IE`` → uk, EU member states → eu, anything
else → unknown. Unknown regions fail closed (blurred) in the policy layer.
"""
from __future__ import annotations

import threading
import time

from fastapi import Request

US = "us"
UK = "uk"
EU = "eu"
UNKNOWN = "unknown"

#: EU member states (IE is deliberately absent: the spec maps IE to the UK zone).
_EU_COUNTRIES = frozenset(
    {
        "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE",
        "GR", "HU", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO",
        "SK", "SI", "ES", "SE",
    }
)

ZONE_BY_COUNTRY: dict[str, str] = {
    "US": US,
    "GB": UK,
    "IE": UK,
    **{code: EU for code in _EU_COUNTRIES},
}


def zone_of(country: str | None) -> str:
    """Map an ISO 3166-1 alpha-2 country code to a policy zone.

    Args:
        country: Uppercase country code (e.g. ``"DE"``); None/unknown → ``unknown``.

    Returns:
        One of ``us``, ``uk``, ``eu``, ``unknown``.
    """
    return ZONE_BY_COUNTRY.get((country or "").upper(), UNKNOWN)


class RegionDetector:
    """Resolves a request to a policy zone, with a short per-IP TTL cache.

    The header is read once per IP and cached for ``ttl`` seconds so a hot
    viewer does not re-trigger the (cheap) header + map lookup on every tile.
    """

    def __init__(
        self,
        default_region: str = "",
        dev_region_header: bool = False,
        ttl: float = 600.0,
    ) -> None:
        self._default_zone = zone_of(default_region) if default_region else None
        self._dev_header = dev_region_header
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, str]] = {}

    def country_of(self, request: Request) -> str | None:
        """The request's country code: dev test header, else ``CF-IPCountry``."""
        if self._dev_header:
            test = request.headers.get("X-Test-Region")
            if test:
                return test.strip().upper() or None
        return request.headers.get("CF-IPCountry")

    def zone_of_request(self, request: Request) -> str:
        """The policy zone for the request's region (fail-closed to ``unknown``).

        The result is cached per IP for ``ttl`` seconds — safe because the
        production ``CF-IPCountry`` header is stable per client IP. The dev
        test header intentionally bypasses the cache so curl checks can flip
        regions from a single machine.
        """
        country = self.country_of(request)
        if not country:
            return self._default_zone or UNKNOWN
        if self._dev_header:
            return zone_of(country)
        ip = request.client.host if request.client else country
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(ip)
            if cached is not None and cached[0] > now:
                return cached[1]
        zone = zone_of(country)
        with self._lock:
            self._cache[ip] = (now + self._ttl, zone)
        return zone
