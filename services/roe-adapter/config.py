"""
services/roe-adapter/config.py
==============================
Configuration for the ROE Adapter service.

Uses ``pydantic-settings`` (falling back to ``pydantic.BaseSettings`` when the
standalone package is unavailable) so that all runtime configuration is sourced
from environment variables with sensible development defaults.

Requirements: 1.6, 1.7, 1.8
"""

from __future__ import annotations

from functools import lru_cache

try:  # pydantic v2 split settings into a dedicated package
    from pydantic_settings import BaseSettings, SettingsConfigDict

    _HAS_SETTINGS_PKG = True
except ImportError:  # pragma: no cover - fallback for environments without the package
    from pydantic import BaseSettings  # type: ignore[no-redef]

    SettingsConfigDict = None  # type: ignore[assignment,misc]
    _HAS_SETTINGS_PKG = False


class Settings(BaseSettings):
    """Runtime settings for the ROE Adapter.

    All values may be overridden via environment variables prefixed with
    ``ROE_ADAPTER_`` (e.g. ``ROE_ADAPTER_KAFKA_BOOTSTRAP_SERVERS``).
    """

    # --- Service identity ---
    service_name: str = "roe-adapter"
    environment: str = "dev"  # dev | test | staging | prod

    # --- Kafka ---
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_roe_topic: str = "roe.received"
    # When True (default in dev), a failure to connect/publish to Kafka is logged
    # and degraded gracefully instead of raising. Set False in prod.
    kafka_degrade_gracefully: bool = True

    # --- SOAP ---
    # Optional WSDL location for the ROE Web SOAP service. When unset the SOAP
    # endpoint is still served (spyne generates its own WSDL).
    roe_web_wsdl_url: str | None = None

    if _HAS_SETTINGS_PKG:
        model_config = SettingsConfigDict(  # type: ignore[misc]
            env_prefix="ROE_ADAPTER_",
            env_file=".env",
            extra="ignore",
        )
    else:  # pragma: no cover - pydantic v1 style config

        class Config:
            env_prefix = "ROE_ADAPTER_"
            env_file = ".env"


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance."""
    return Settings()
