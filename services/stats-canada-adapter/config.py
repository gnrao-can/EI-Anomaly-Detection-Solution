"""
services/stats-canada-adapter/config.py
=======================================
Centralised configuration for the Stats Canada Adapter.

All runtime configuration is read from environment variables (see
``.env.example``) using ``pydantic-settings``. This keeps connection settings
out of source code and lets the adapter degrade gracefully in local development
(serving from ``mock-data/stats-canada/`` fixtures) when the upstream Statistics
Canada LFS API or Redis is unavailable.

Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6
"""

from __future__ import annotations

import os
from functools import lru_cache

try:  # pydantic-settings is the canonical settings library for pydantic v2
    from pydantic_settings import BaseSettings, SettingsConfigDict

    _HAS_PYDANTIC_SETTINGS = True
except ImportError:  # pragma: no cover - fallback for minimal environments
    _HAS_PYDANTIC_SETTINGS = False


#: Default Statistics Canada Labour Force Survey (LFS) download URL.
_DEFAULT_LFS_URL = (
    "https://www150.statcan.gc.ca/t1/tbl1/en/dtbl/download/98100424.json"
)

#: Project-root-relative default location of the LFS mock fixtures.
_DEFAULT_MOCK_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "mock-data", "stats-canada"
    )
)


if _HAS_PYDANTIC_SETTINGS:

    class Settings(BaseSettings):
        """Environment-driven configuration for the Stats Canada Adapter."""

        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
            case_sensitive=False,
        )

        # ── Deployment ──────────────────────────────────────────────
        app_env: str = "development"
        service_name: str = "stats-canada-adapter"
        statscan_adapter_port: int = 8010

        # ── Upstream LFS API ────────────────────────────────────────
        statscan_lfs_url: str = _DEFAULT_LFS_URL
        #: Connection (connect) timeout, seconds.
        statscan_connect_timeout_seconds: float = 10.0
        #: Read timeout, seconds.
        statscan_read_timeout_seconds: float = 30.0
        #: Version string annotated onto every parsed record.
        statscan_api_version: str = "98100424"

        # ── Redis cache ─────────────────────────────────────────────
        redis_url: str = "redis://localhost:6379"
        redis_cluster_mode: bool = False
        redis_password: str | None = None

        # ── Mock data ───────────────────────────────────────────────
        #: When true (or in development), serve from local fixtures instead of
        #: calling the upstream LFS API.
        statscan_use_mock: bool = True
        statscan_mock_dir: str = _DEFAULT_MOCK_DIR

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local", "test"}

        @property
        def serve_from_mock(self) -> bool:
            """True when fixtures should be used rather than the live API."""
            return bool(self.statscan_use_mock) or self.is_development

else:  # pragma: no cover - executed only when pydantic-settings is absent

    class Settings:  # type: ignore[no-redef]
        """Minimal environment-backed settings fallback."""

        def __init__(self) -> None:
            self.app_env = os.environ.get("APP_ENV", "development")
            self.service_name = os.environ.get("SERVICE_NAME", "stats-canada-adapter")
            self.statscan_adapter_port = int(
                os.environ.get("STATSCAN_ADAPTER_PORT", "8010")
            )

            self.statscan_lfs_url = os.environ.get("STATSCAN_LFS_URL", _DEFAULT_LFS_URL)
            self.statscan_connect_timeout_seconds = float(
                os.environ.get("STATSCAN_CONNECT_TIMEOUT_SECONDS", "10.0")
            )
            self.statscan_read_timeout_seconds = float(
                os.environ.get("STATSCAN_READ_TIMEOUT_SECONDS", "30.0")
            )
            self.statscan_api_version = os.environ.get(
                "STATSCAN_API_VERSION", "98100424"
            )

            self.redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
            self.redis_cluster_mode = (
                os.environ.get("REDIS_CLUSTER_MODE", "false").lower() == "true"
            )
            self.redis_password = os.environ.get("REDIS_PASSWORD") or None

            self.statscan_use_mock = (
                os.environ.get("STATSCAN_USE_MOCK", "true").lower() == "true"
            )
            self.statscan_mock_dir = os.environ.get(
                "STATSCAN_MOCK_DIR", _DEFAULT_MOCK_DIR
            )

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local", "test"}

        @property
        def serve_from_mock(self) -> bool:
            return bool(self.statscan_use_mock) or self.is_development


@lru_cache
def get_settings() -> "Settings":
    """Return a cached ``Settings`` instance."""
    return Settings()


__all__ = ["Settings", "get_settings"]
