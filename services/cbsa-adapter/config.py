"""
services/cbsa-adapter/config.py
===============================
Centralised configuration for the CBSA Adapter.

The CBSA Adapter fetches cross-border travel (crossing) records for a claimant
over the CBSA Protected B secure channel using mutual TLS (mTLS). All runtime
configuration is read from environment variables (see ``.env.example``) via
``pydantic-settings`` so that certificate paths, the base URL, and the request
timeout stay out of source control and can be overridden per environment.

In development and test, no live CBSA endpoint (and no mTLS certificate) is
available, so the adapter serves deterministic synthetic travel records from
``mock-data/cbsa/`` keyed by the claimant's province of employment.

Requirements: 13.1, 13.2, 13.4, 13.6
"""

from __future__ import annotations

import os
from functools import lru_cache

try:  # pydantic-settings is the canonical settings library for pydantic v2
    from pydantic_settings import BaseSettings, SettingsConfigDict

    _HAS_PYDANTIC_SETTINGS = True
except ImportError:  # pragma: no cover - fallback for minimal environments
    _HAS_PYDANTIC_SETTINGS = False


# Repository-root-relative default location of the CBSA mock fixtures. Resolved
# to an absolute path so the adapter works regardless of the process CWD.
_DEFAULT_MOCK_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "mock-data", "cbsa")
)


if _HAS_PYDANTIC_SETTINGS:

    class Settings(BaseSettings):
        """Environment-driven configuration for the CBSA Adapter."""

        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
            case_sensitive=False,
        )

        # ── Deployment ──────────────────────────────────────────────
        app_env: str = "development"
        service_name: str = "cbsa-adapter"
        cbsa_adapter_port: int = 8013

        # ── CBSA Protected B secure channel (mTLS) ──────────────────
        # In production these point at the CBSA Protected B endpoint and the
        # GC-PKI client certificate/key/CA bundle. In dev/test they are blank
        # and the deterministic synthetic generator is used instead.
        cbsa_api_base_url: str = "https://cbsa-asfc.gc.ca/ei-ads/api"
        cbsa_client_cert_path: str = ""
        cbsa_client_key_path: str = ""
        cbsa_ca_cert_path: str = ""
        cbsa_timeout: float = 5.0  # seconds — total request timeout (Req 13.1)

        # ── Mock data ───────────────────────────────────────────────
        cbsa_mock_data_dir: str = _DEFAULT_MOCK_DIR

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local", "test"}

        @property
        def mtls_configured(self) -> bool:
            """True only when a full client cert + key are configured."""
            return bool(self.cbsa_client_cert_path and self.cbsa_client_key_path)

        @property
        def use_synthetic(self) -> bool:
            """Use the deterministic synthetic generator in dev/test or when
            no mTLS material is configured (never call the live channel then)."""
            return self.is_development or not self.mtls_configured

else:  # pragma: no cover - executed only when pydantic-settings is absent

    class Settings:  # type: ignore[no-redef]
        """Minimal environment-backed settings fallback (no pydantic-settings)."""

        def __init__(self) -> None:
            self.app_env = os.environ.get("APP_ENV", "development")
            self.service_name = os.environ.get("SERVICE_NAME", "cbsa-adapter")
            self.cbsa_adapter_port = int(os.environ.get("CBSA_ADAPTER_PORT", "8013"))

            self.cbsa_api_base_url = os.environ.get(
                "CBSA_API_BASE_URL", "https://cbsa-asfc.gc.ca/ei-ads/api"
            )
            self.cbsa_client_cert_path = os.environ.get("CBSA_CLIENT_CERT_PATH", "")
            self.cbsa_client_key_path = os.environ.get("CBSA_CLIENT_KEY_PATH", "")
            self.cbsa_ca_cert_path = os.environ.get("CBSA_CA_CERT_PATH", "")
            self.cbsa_timeout = float(os.environ.get("CBSA_TIMEOUT", "5"))
            self.cbsa_mock_data_dir = os.environ.get(
                "CBSA_MOCK_DATA_DIR", _DEFAULT_MOCK_DIR
            )

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local", "test"}

        @property
        def mtls_configured(self) -> bool:
            return bool(self.cbsa_client_cert_path and self.cbsa_client_key_path)

        @property
        def use_synthetic(self) -> bool:
            return self.is_development or not self.mtls_configured


@lru_cache
def get_settings() -> "Settings":
    """Return a cached ``Settings`` instance."""
    return Settings()


__all__ = ["Settings", "get_settings"]
