"""
services/job-bank-adapter/config.py
===================================
Centralised configuration for the Job Bank Adapter.

The Job Bank Adapter queries the Job Bank Canada REST API for the number of
active job postings and the median advertised wage for a claimant's NOC code
within an ESDC economic region. All runtime configuration is read from
environment variables (see ``.env.example``) using ``pydantic-settings`` so that
the API key, base URL, and snapshot location stay out of source control and can
be overridden per environment.

Failure / degradation strategy:
  * In development and test, the adapter ALWAYS serves from the Job Bank open
    data snapshot in ``mock-data/job-bank/`` (Requirement 16.4) — no live API.
  * In production, on API unavailability the adapter serves from the snapshot as
    long as the snapshot is no older than 7 days, flagging the response
    ``DATA_STALE`` (Requirement 16.3).
  * Successful API responses are cached in Redis under
    ``job_bank:postings:{noc}:{region}:{ym}`` with a 7-day TTL (Requirement
    16.2).

Requirements: 16.1, 16.2, 16.3, 16.4
"""

from __future__ import annotations

import os
from functools import lru_cache

try:  # pydantic-settings is the canonical settings library for pydantic v2
    from pydantic_settings import BaseSettings, SettingsConfigDict

    _HAS_PYDANTIC_SETTINGS = True
except ImportError:  # pragma: no cover - fallback for minimal environments
    _HAS_PYDANTIC_SETTINGS = False


#: Default Job Bank Canada REST API base URL.
_DEFAULT_API_BASE_URL = "https://www.jobbank.gc.ca/api"

#: Project-root-relative default location of the Job Bank open data snapshot.
_DEFAULT_SNAPSHOT_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "mock-data", "job-bank")
)

#: Maximum snapshot age (days) before a snapshot-served response is considered
#: unusable (Requirement 16.3).
_DEFAULT_SNAPSHOT_MAX_AGE_DAYS = 7


if _HAS_PYDANTIC_SETTINGS:

    class Settings(BaseSettings):
        """Environment-driven configuration for the Job Bank Adapter."""

        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
            case_sensitive=False,
        )

        # ── Deployment ──────────────────────────────────────────────
        app_env: str = "development"
        service_name: str = "job-bank-adapter"
        job_bank_adapter_port: int = 8016

        # ── Upstream Job Bank API ───────────────────────────────────
        job_bank_api_base_url: str = _DEFAULT_API_BASE_URL
        job_bank_api_key: str = ""
        #: Total request timeout, seconds. The Regional Analysis Engine allots a
        #: 5-second budget to the Job Bank lookup (design).
        job_bank_timeout: float = 5.0

        # ── Redis cache ─────────────────────────────────────────────
        redis_url: str = "redis://localhost:6379"
        redis_cluster_mode: bool = False
        redis_password: str | None = None

        # ── Snapshot / mock data ────────────────────────────────────
        job_bank_snapshot_dir: str = _DEFAULT_SNAPSHOT_DIR
        job_bank_snapshot_max_age_days: int = _DEFAULT_SNAPSHOT_MAX_AGE_DAYS

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local", "test"}

        @property
        def serve_from_snapshot(self) -> bool:
            """Always serve from the snapshot in dev/test (Requirement 16.4)."""
            return self.is_development

else:  # pragma: no cover - executed only when pydantic-settings is absent

    class Settings:  # type: ignore[no-redef]
        """Minimal environment-backed settings fallback."""

        def __init__(self) -> None:
            self.app_env = os.environ.get("APP_ENV", "development")
            self.service_name = os.environ.get("SERVICE_NAME", "job-bank-adapter")
            self.job_bank_adapter_port = int(
                os.environ.get("JOB_BANK_ADAPTER_PORT", "8016")
            )

            self.job_bank_api_base_url = os.environ.get(
                "JOB_BANK_API_BASE_URL", _DEFAULT_API_BASE_URL
            )
            self.job_bank_api_key = os.environ.get("JOB_BANK_API_KEY", "")
            self.job_bank_timeout = float(os.environ.get("JOB_BANK_TIMEOUT", "5"))

            self.redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
            self.redis_cluster_mode = (
                os.environ.get("REDIS_CLUSTER_MODE", "false").lower() == "true"
            )
            self.redis_password = os.environ.get("REDIS_PASSWORD") or None

            self.job_bank_snapshot_dir = os.environ.get(
                "JOB_BANK_SNAPSHOT_DIR", _DEFAULT_SNAPSHOT_DIR
            )
            self.job_bank_snapshot_max_age_days = int(
                os.environ.get(
                    "JOB_BANK_SNAPSHOT_MAX_AGE_DAYS",
                    str(_DEFAULT_SNAPSHOT_MAX_AGE_DAYS),
                )
            )

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local", "test"}

        @property
        def serve_from_snapshot(self) -> bool:
            return self.is_development


@lru_cache
def get_settings() -> "Settings":
    """Return a cached ``Settings`` instance."""
    return Settings()


__all__ = ["Settings", "get_settings"]
