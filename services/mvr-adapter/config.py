"""
services/mvr-adapter/config.py
==============================
Centralised configuration for the MVR (Motor Vehicle Registry) Adapter.

The MVR Adapter queries the provincial Motor Vehicle Registry API that matches
the claimant's residential province and returns a strictly data-minimised
result: only ``vehicle_ownership_status`` and ``vehicle_class`` (Requirement
15.4). No vehicle history, insurance, or financial data is ever requested.

All runtime configuration is read from environment variables (see
``.env.example``) via ``pydantic-settings`` so that per-province base URLs, the
3-second timeout, the dev flag, and the mock-data directory stay out of source
control and can be overridden per environment.

In development and test no live provincial MVR endpoint (and no API key) is
available, so the adapter serves deterministic synthetic vehicle records from
``mock-data/motor-vehicle/`` keyed by the claimant's residential province — one
fixture per province (Requirement 15.6).

Requirements: 15.1, 15.2, 15.3, 15.4, 15.5, 15.6
"""

from __future__ import annotations

import os
from functools import lru_cache

try:  # pydantic-settings is the canonical settings library for pydantic v2
    from pydantic_settings import BaseSettings, SettingsConfigDict

    _HAS_PYDANTIC_SETTINGS = True
except ImportError:  # pragma: no cover - fallback for minimal environments
    _HAS_PYDANTIC_SETTINGS = False


# The ten Canadian provinces served by the MVR Adapter (Requirement 15.6).
PROVINCES: tuple[str, ...] = (
    "AB",
    "BC",
    "MB",
    "NB",
    "NL",
    "NS",
    "ON",
    "PE",
    "QC",
    "SK",
)


# Repository-root-relative default location of the MVR mock fixtures. Resolved
# to an absolute path so the adapter works regardless of the process CWD.
_DEFAULT_MOCK_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "mock-data", "motor-vehicle")
)


# Default per-province MVR API base URLs. In production these are overridden per
# environment; in dev/test they are unused because the synthetic generator runs.
_DEFAULT_PROVINCE_BASE_URLS: dict[str, str] = {
    p: f"https://mvr.{p.lower()}.ca/ei-ads/api" for p in PROVINCES
}


if _HAS_PYDANTIC_SETTINGS:

    class Settings(BaseSettings):
        """Environment-driven configuration for the MVR Adapter."""

        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
            case_sensitive=False,
        )

        # ── Deployment ──────────────────────────────────────────────
        app_env: str = "development"
        service_name: str = "mvr-adapter"
        mvr_adapter_port: int = 8015

        # ── Provincial MVR APIs ─────────────────────────────────────
        # A JSON-ish mapping of province code -> base URL may be supplied via
        # the ``MVR_PROVINCE_BASE_URLS`` env var; otherwise defaults are used.
        # Per-province API keys live in Kubernetes Secrets in production.
        mvr_province_base_urls: dict[str, str] = dict(_DEFAULT_PROVINCE_BASE_URLS)
        mvr_api_key: str = ""

        # Total request timeout in seconds (Requirements 15.3, 15.5).
        mvr_timeout: float = 3.0

        # ── Mock data ───────────────────────────────────────────────
        mvr_mock_data_dir: str = _DEFAULT_MOCK_DIR

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local", "test"}

        @property
        def has_api_key(self) -> bool:
            return bool(self.mvr_api_key)

        @property
        def use_synthetic(self) -> bool:
            """Use the deterministic synthetic generator in dev/test or when no
            API key is configured (never call a live provincial API then)."""
            return self.is_development or not self.has_api_key

        def base_url_for(self, province: str) -> str:
            """Return the MVR base URL for a province, defaulting sensibly."""
            province = (province or "").upper()
            return self.mvr_province_base_urls.get(
                province, _DEFAULT_PROVINCE_BASE_URLS.get(province, "")
            )

else:  # pragma: no cover - executed only when pydantic-settings is absent

    class Settings:  # type: ignore[no-redef]
        """Minimal environment-backed settings fallback (no pydantic-settings)."""

        def __init__(self) -> None:
            self.app_env = os.environ.get("APP_ENV", "development")
            self.service_name = os.environ.get("SERVICE_NAME", "mvr-adapter")
            self.mvr_adapter_port = int(os.environ.get("MVR_ADAPTER_PORT", "8015"))

            self.mvr_province_base_urls = dict(_DEFAULT_PROVINCE_BASE_URLS)
            self.mvr_api_key = os.environ.get("MVR_API_KEY", "")
            self.mvr_timeout = float(os.environ.get("MVR_TIMEOUT", "3"))
            self.mvr_mock_data_dir = os.environ.get(
                "MVR_MOCK_DATA_DIR", _DEFAULT_MOCK_DIR
            )

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local", "test"}

        @property
        def has_api_key(self) -> bool:
            return bool(self.mvr_api_key)

        @property
        def use_synthetic(self) -> bool:
            return self.is_development or not self.has_api_key

        def base_url_for(self, province: str) -> str:
            province = (province or "").upper()
            return self.mvr_province_base_urls.get(
                province, _DEFAULT_PROVINCE_BASE_URLS.get(province, "")
            )


@lru_cache
def get_settings() -> "Settings":
    """Return a cached ``Settings`` instance."""
    return Settings()


__all__ = ["Settings", "get_settings", "PROVINCES"]
