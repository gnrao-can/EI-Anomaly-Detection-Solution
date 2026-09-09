"""
services/geonames-adapter/config.py
===================================
Centralised configuration for the GeoNames Adapter.

All runtime configuration is read from environment variables (see
``.env.example``) using ``pydantic-settings``. The adapter resolves Canadian
postal codes / FSAs to (latitude, longitude) via the GeoNames REST API, and
falls back to a local dump in ``mock-data/geonames/`` when the API is
unavailable. In dev/test it always uses the local dump (no network).

Design references (see design.md — "GeoNames Adapter"):
  - Primary API: GeoNames REST API (``api.geonames.org``)
  - Auth: GeoNames API username (Kubernetes Secret in prod)
  - Local fallback: ``mock-data/geonames/`` (refreshed every 90 days)
  - Coordinate bounds: lat in [-90.0, 90.0], lon in [-180.0, 180.0]
  - Distance precision: Haversine rounded to 3 decimal places (km)
  - Rate limiting: 2,000 credits/hour; Redis counter per hour; backoff on 429

Requirements: 14.1, 14.2, 14.3, 14.4, 14.5, 14.6
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

try:  # pydantic-settings is the canonical settings library for pydantic v2
    from pydantic_settings import BaseSettings, SettingsConfigDict

    _HAS_PYDANTIC_SETTINGS = True
except ImportError:  # pragma: no cover - fallback for minimal environments
    _HAS_PYDANTIC_SETTINGS = False


# Repository root: services/geonames-adapter/config.py -> parents[2] == repo root
# Repository-root-relative default location of the local GeoNames dump.
# In the source tree this file lives at services/geonames-adapter/config.py, so
# parents[2] is the repo root. In a built image the service source is flattened
# to /app/config.py (no such ancestor), so guard the lookup: fall back to a
# CWD-relative default that is expected to be overridden by GEONAMES_LOCAL_DUMP_
# PATH (the dev compose bind-mounts the dump and sets that env var).
_here = Path(__file__).resolve()
_REPO_ROOT = _here.parents[2] if len(_here.parents) >= 3 else _here.parent
_DEFAULT_LOCAL_DUMP = _REPO_ROOT / "mock-data" / "geonames" / "local-dump.json"


if _HAS_PYDANTIC_SETTINGS:

    class Settings(BaseSettings):
        """Environment-driven configuration for the GeoNames Adapter."""

        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
            case_sensitive=False,
        )

        # ── Deployment ──────────────────────────────────────────────
        app_env: str = "development"
        service_name: str = "geonames-adapter"
        geonames_adapter_port: int = 8110

        # ── GeoNames REST API ───────────────────────────────────────
        geonames_api_url: str = "http://api.geonames.org"
        geonames_username: str = "demo"
        geonames_connect_timeout_seconds: float = 5.0
        geonames_read_timeout_seconds: float = 10.0

        # ── Local dump fallback ─────────────────────────────────────
        geonames_local_dump_path: str = str(_DEFAULT_LOCAL_DUMP)
        # A local dump older than this is considered stale (design: <= 90 days).
        geonames_local_dump_max_age_days: int = 90

        # ── Redis ───────────────────────────────────────────────────
        redis_url: str = "redis://localhost:6379"

        # ── Rate limiting ───────────────────────────────────────────
        # GeoNames free tier: 2,000 credits/hour. Redis counter per hour.
        geonames_rate_limit_per_hour: int = 2000

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local", "test"}

        @property
        def local_dump_path(self) -> Path:
            return Path(self.geonames_local_dump_path)

else:  # pragma: no cover - executed only when pydantic-settings is absent

    import os

    class Settings:  # type: ignore[no-redef]
        """Minimal environment-backed settings fallback."""

        def __init__(self) -> None:
            self.app_env = os.environ.get("APP_ENV", "development")
            self.service_name = os.environ.get("SERVICE_NAME", "geonames-adapter")
            self.geonames_adapter_port = int(
                os.environ.get("GEONAMES_ADAPTER_PORT", "8110")
            )

            self.geonames_api_url = os.environ.get(
                "GEONAMES_API_URL", "http://api.geonames.org"
            )
            self.geonames_username = os.environ.get("GEONAMES_USERNAME", "demo")
            self.geonames_connect_timeout_seconds = float(
                os.environ.get("GEONAMES_CONNECT_TIMEOUT_SECONDS", "5.0")
            )
            self.geonames_read_timeout_seconds = float(
                os.environ.get("GEONAMES_READ_TIMEOUT_SECONDS", "10.0")
            )

            self.geonames_local_dump_path = os.environ.get(
                "GEONAMES_LOCAL_DUMP_PATH", str(_DEFAULT_LOCAL_DUMP)
            )
            self.geonames_local_dump_max_age_days = int(
                os.environ.get("GEONAMES_LOCAL_DUMP_MAX_AGE_DAYS", "90")
            )

            self.redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
            self.geonames_rate_limit_per_hour = int(
                os.environ.get("GEONAMES_RATE_LIMIT_PER_HOUR", "2000")
            )

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local", "test"}

        @property
        def local_dump_path(self) -> Path:
            return Path(self.geonames_local_dump_path)


@lru_cache
def get_settings() -> "Settings":
    """Return a cached ``Settings`` instance."""
    return Settings()


__all__ = ["Settings", "get_settings"]
