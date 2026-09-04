"""
services/cra-adapter/config.py
==============================
Centralised configuration for the CRA Adapter.

All runtime configuration is read from environment variables (see ``.env.example``)
using ``pydantic-settings``. This keeps secrets and connection settings out of
source code and lets the service degrade gracefully in local development when
external systems (the CRA secure data-exchange endpoint, ClickHouse, Redis) are
unavailable.

The CRA data exchange runs over the Government of Canada secure data-exchange
channel using **mTLS 1.3 with client certificates** (design "CRA Adapter"). The
paths to the client certificate, client private key, and the trusted CA bundle
are provided via the environment and applied to the outbound ``httpx`` client in
``client.py``. In development and test we never perform real mTLS — synthetic T4
records are generated locally with the same field structure as production.

Requirements: 11.1, 11.2, 11.6, 11.8
"""

from __future__ import annotations

from functools import lru_cache

try:  # pydantic-settings is the canonical settings library for pydantic v2
    from pydantic_settings import BaseSettings, SettingsConfigDict

    _HAS_PYDANTIC_SETTINGS = True
except ImportError:  # pragma: no cover - fallback for minimal environments
    _HAS_PYDANTIC_SETTINGS = False


if _HAS_PYDANTIC_SETTINGS:

    class Settings(BaseSettings):
        """Environment-driven configuration for the CRA Adapter."""

        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
            case_sensitive=False,
        )

        # ── Deployment ──────────────────────────────────────────────
        app_env: str = "development"
        service_name: str = "cra-adapter"
        cra_adapter_port: int = 8011

        # ── CRA secure data exchange (mTLS 1.3) ─────────────────────
        # Base URL of the CRA secure T4 data-exchange endpoint.
        cra_base_url: str = "https://cra-secure-exchange.canada.ca"
        # Paths to the mTLS client certificate, private key, and trusted CA
        # bundle. In production these are mounted from Kubernetes Secrets. When
        # empty (local dev), no real mTLS handshake is attempted.
        cra_client_cert_path: str = ""
        cra_client_key_path: str = ""
        cra_ca_cert_path: str = ""

        # Total request timeout in seconds (Requirement 11.3).
        cra_timeout_seconds: float = 5.0
        # How far before the timeout threshold a structured UNAVAILABLE must be
        # returned (Requirement 11.4): within ~200 ms of the threshold.
        cra_unavailable_margin_ms: float = 200.0
        # Maximum age (years) of a T4 record before it is discarded (Req 11.7).
        cra_max_record_age_years: int = 10

        # ── Redis (optional caching, per design cra:t4:* keys) ──────
        redis_url: str = "redis://localhost:6379"

        # ── ClickHouse audit trail (adapter_audit_log) ──────────────
        clickhouse_host: str = "localhost"
        clickhouse_port: int = 8123
        clickhouse_user: str = "default"
        clickhouse_password: str = ""
        clickhouse_database: str = "ei_ads_analytics"
        # Budget within which every access must be logged (Requirement 11.5).
        audit_budget_ms: float = 1000.0

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local", "test"}

        @property
        def has_mtls_material(self) -> bool:
            """True when a client cert + key are configured for real mTLS."""
            return bool(self.cra_client_cert_path and self.cra_client_key_path)

else:  # pragma: no cover - executed only when pydantic-settings is absent

    import os

    class Settings:  # type: ignore[no-redef]
        """Minimal environment-backed settings fallback (no pydantic-settings)."""

        def __init__(self) -> None:
            self.app_env = os.environ.get("APP_ENV", "development")
            self.service_name = os.environ.get("SERVICE_NAME", "cra-adapter")
            self.cra_adapter_port = int(os.environ.get("CRA_ADAPTER_PORT", "8011"))

            self.cra_base_url = os.environ.get(
                "CRA_BASE_URL", "https://cra-secure-exchange.canada.ca"
            )
            self.cra_client_cert_path = os.environ.get("CRA_CLIENT_CERT_PATH", "")
            self.cra_client_key_path = os.environ.get("CRA_CLIENT_KEY_PATH", "")
            self.cra_ca_cert_path = os.environ.get("CRA_CA_CERT_PATH", "")

            self.cra_timeout_seconds = float(
                os.environ.get("CRA_TIMEOUT_SECONDS", "5.0")
            )
            self.cra_unavailable_margin_ms = float(
                os.environ.get("CRA_UNAVAILABLE_MARGIN_MS", "200.0")
            )
            self.cra_max_record_age_years = int(
                os.environ.get("CRA_MAX_RECORD_AGE_YEARS", "10")
            )

            self.redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")

            self.clickhouse_host = os.environ.get("CLICKHOUSE_HOST", "localhost")
            self.clickhouse_port = int(os.environ.get("CLICKHOUSE_PORT", "8123"))
            self.clickhouse_user = os.environ.get("CLICKHOUSE_USER", "default")
            self.clickhouse_password = os.environ.get("CLICKHOUSE_PASSWORD", "")
            self.clickhouse_database = os.environ.get(
                "CLICKHOUSE_DATABASE", "ei_ads_analytics"
            )
            self.audit_budget_ms = float(os.environ.get("AUDIT_BUDGET_MS", "1000.0"))

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local", "test"}

        @property
        def has_mtls_material(self) -> bool:
            return bool(self.cra_client_cert_path and self.cra_client_key_path)


@lru_cache
def get_settings() -> "Settings":
    """Return a cached ``Settings`` instance."""
    return Settings()


__all__ = ["Settings", "get_settings"]
