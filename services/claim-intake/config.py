"""
services/claim-intake/config.py
===============================
Centralised configuration for the Claim Intake Service.

All runtime configuration is read from environment variables (see ``.env.example``)
using ``pydantic-settings``. This keeps secrets and connection settings out of
source code and lets the service degrade gracefully in local development when
external systems (PostgreSQL, Kafka, Redis) are unavailable.

Requirements: 1.1, 1.11, 19.7
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
        """Environment-driven configuration for the Claim Intake Service."""

        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
            case_sensitive=False,
        )

        # ── Deployment ──────────────────────────────────────────────
        app_env: str = "development"
        service_name: str = "claim-intake"
        claim_intake_port: int = 8001

        # ── PostgreSQL ──────────────────────────────────────────────
        postgres_host: str = "localhost"
        postgres_port: int = 5432
        postgres_db: str = "ei_ads"
        postgres_user: str = "ei_ads_app"
        postgres_password: str = "changeme_local_only"
        postgres_ssl_mode: str = "disable"

        # ── Kafka ───────────────────────────────────────────────────
        kafka_bootstrap_servers: str = "localhost:9092"

        # ── Redis ───────────────────────────────────────────────────
        redis_url: str = "redis://localhost:6379"

        # ── GCKey OAuth 2.0 ─────────────────────────────────────────
        gckey_jwks_url: str = "https://auth.id.gc.ca/.well-known/jwks.json"
        gckey_client_id: str = "ei-ads-local"
        gckey_issuer: str = ""

        # ── SIN hashing ─────────────────────────────────────────────
        sin_hash_salt: str = "changeme_generate_a_real_64char_hex_salt_for_local_dev"

        @property
        def postgres_dsn(self) -> str:
            """asyncpg-compatible DSN built from the discrete PG settings."""
            return (
                f"postgresql://{self.postgres_user}:{self.postgres_password}"
                f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
            )

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local"}

else:  # pragma: no cover - executed only when pydantic-settings is absent

    import os

    class Settings:  # type: ignore[no-redef]
        """Minimal environment-backed settings fallback (no pydantic-settings)."""

        def __init__(self) -> None:
            self.app_env = os.environ.get("APP_ENV", "development")
            self.service_name = os.environ.get("SERVICE_NAME", "claim-intake")
            self.claim_intake_port = int(os.environ.get("CLAIM_INTAKE_PORT", "8001"))

            self.postgres_host = os.environ.get("POSTGRES_HOST", "localhost")
            self.postgres_port = int(os.environ.get("POSTGRES_PORT", "5432"))
            self.postgres_db = os.environ.get("POSTGRES_DB", "ei_ads")
            self.postgres_user = os.environ.get("POSTGRES_USER", "ei_ads_app")
            self.postgres_password = os.environ.get(
                "POSTGRES_PASSWORD", "changeme_local_only"
            )
            self.postgres_ssl_mode = os.environ.get("POSTGRES_SSL_MODE", "disable")

            self.kafka_bootstrap_servers = os.environ.get(
                "KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"
            )
            self.redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")

            self.gckey_jwks_url = os.environ.get(
                "GCKEY_JWKS_URL", "https://auth.id.gc.ca/.well-known/jwks.json"
            )
            self.gckey_client_id = os.environ.get("GCKEY_CLIENT_ID", "ei-ads-local")
            self.gckey_issuer = os.environ.get("GCKEY_ISSUER", "")
            self.sin_hash_salt = os.environ.get(
                "SIN_HASH_SALT",
                "changeme_generate_a_real_64char_hex_salt_for_local_dev",
            )

        @property
        def postgres_dsn(self) -> str:
            return (
                f"postgresql://{self.postgres_user}:{self.postgres_password}"
                f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
            )

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local"}


@lru_cache
def get_settings() -> "Settings":
    """Return a cached ``Settings`` instance."""
    return Settings()


__all__ = ["Settings", "get_settings"]
