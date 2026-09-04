"""
services/business-registry-adapter/config.py
=============================================
Centralised configuration for the Business Registry Adapter.

The adapter queries the federal Corporations Canada registry plus 13
provincial / territorial business registries concurrently. All runtime
configuration is read from environment variables (see ``.env.example``) via
``pydantic-settings`` so registry base URLs, the GeoNames adapter URL, the
development flag, and the mock-data directories stay out of source code.

Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6
"""

from __future__ import annotations

import os
from functools import lru_cache

# The 13 provincial / territorial registry jurisdiction codes (10 provinces +
# 3 territories) queried alongside the federal Corporations Canada registry.
PROVINCIAL_JURISDICTIONS: tuple[str, ...] = (
    "AB",  # Alberta
    "BC",  # British Columbia
    "MB",  # Manitoba
    "NB",  # New Brunswick
    "NL",  # Newfoundland and Labrador
    "NS",  # Nova Scotia
    "ON",  # Ontario
    "PE",  # Prince Edward Island
    "QC",  # Quebec
    "SK",  # Saskatchewan
    "NT",  # Northwest Territories
    "NU",  # Nunavut
    "YT",  # Yukon
)

FEDERAL_JURISDICTION: str = "CA"


def _default_provincial_base_urls() -> dict[str, str]:
    """Return the default per-jurisdiction registry base URL map."""
    return {
        code: f"https://registry.{code.lower()}.gc.ca/api/v1"
        for code in PROVINCIAL_JURISDICTIONS
    }


try:  # pydantic-settings is the canonical settings library for pydantic v2
    from pydantic_settings import BaseSettings, SettingsConfigDict

    _HAS_PYDANTIC_SETTINGS = True
except ImportError:  # pragma: no cover - fallback for minimal environments
    _HAS_PYDANTIC_SETTINGS = False


if _HAS_PYDANTIC_SETTINGS:

    class Settings(BaseSettings):
        """Environment-driven configuration for the Business Registry Adapter."""

        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
            case_sensitive=False,
        )

        # ── Deployment ──────────────────────────────────────────────
        app_env: str = "development"
        service_name: str = "business-registry-adapter"
        business_registry_port: int = 8012

        # ── Federal registry (Corporations Canada) ─────────────────
        corporations_canada_base_url: str = "https://ised-isde.canada.ca/cc/api/v1"

        # ── Provincial / territorial registries (13) ───────────────
        provincial_registry_base_urls: dict[str, str] = _default_provincial_base_urls()

        # ── GeoNames Adapter (for residential/commercial zoning) ────
        geonames_adapter_url: str = "http://geonames-adapter:8014"

        # ── Timeouts ────────────────────────────────────────────────
        registry_timeout_seconds: float = 5.0
        geonames_timeout_seconds: float = 3.0

        # ── Mock-data (dev/test) ────────────────────────────────────
        # Directories are resolved relative to the monorepo root by default.
        business_registries_dir: str = os.path.join(
            "mock-data", "business-registries"
        )
        provincial_registries_dir: str = os.path.join(
            "mock-data", "provincial-registries"
        )

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local", "test"}

else:  # pragma: no cover - executed only when pydantic-settings is absent

    class Settings:  # type: ignore[no-redef]
        """Minimal environment-backed settings fallback (no pydantic-settings)."""

        def __init__(self) -> None:
            self.app_env = os.environ.get("APP_ENV", "development")
            self.service_name = os.environ.get(
                "SERVICE_NAME", "business-registry-adapter"
            )
            self.business_registry_port = int(
                os.environ.get("BUSINESS_REGISTRY_PORT", "8012")
            )
            self.corporations_canada_base_url = os.environ.get(
                "CORPORATIONS_CANADA_BASE_URL",
                "https://ised-isde.canada.ca/cc/api/v1",
            )
            self.provincial_registry_base_urls = _default_provincial_base_urls()
            self.geonames_adapter_url = os.environ.get(
                "GEONAMES_ADAPTER_URL", "http://geonames-adapter:8014"
            )
            self.registry_timeout_seconds = float(
                os.environ.get("REGISTRY_TIMEOUT_SECONDS", "5.0")
            )
            self.geonames_timeout_seconds = float(
                os.environ.get("GEONAMES_TIMEOUT_SECONDS", "3.0")
            )
            self.business_registries_dir = os.environ.get(
                "BUSINESS_REGISTRIES_DIR",
                os.path.join("mock-data", "business-registries"),
            )
            self.provincial_registries_dir = os.environ.get(
                "PROVINCIAL_REGISTRIES_DIR",
                os.path.join("mock-data", "provincial-registries"),
            )

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local", "test"}


@lru_cache
def get_settings() -> "Settings":
    """Return a cached ``Settings`` instance."""
    return Settings()


__all__ = [
    "Settings",
    "get_settings",
    "PROVINCIAL_JURISDICTIONS",
    "FEDERAL_JURISDICTION",
]
