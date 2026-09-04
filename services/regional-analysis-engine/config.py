"""
services/regional-analysis-engine/config.py
============================================
Centralised configuration for the Regional Analysis Engine (task 16.1).

All runtime configuration is read from environment variables (see
``.env.example``) using ``pydantic-settings``. Connection settings for the
upstream adapters (Stats Canada, Job Bank) and the detection thresholds
(rate-deviation, active-posting count, wage ratio) live here so the pure
analysis functions in ``analysis.py`` and the FastAPI wiring in ``main.py`` can
share a single source of truth.

Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6
"""

from __future__ import annotations

import os
from decimal import Decimal
from functools import lru_cache

try:  # pydantic-settings is the canonical settings library for pydantic v2
    from pydantic_settings import BaseSettings, SettingsConfigDict

    _HAS_PYDANTIC_SETTINGS = True
except ImportError:  # pragma: no cover - fallback for minimal environments
    _HAS_PYDANTIC_SETTINGS = False


#: Default adapter base URLs (dev/compose defaults).
_DEFAULT_STATSCAN_URL = "http://localhost:8010"
_DEFAULT_JOB_BANK_URL = "http://localhost:8016"

#: Detection thresholds (design §"Regional Analysis Engine").
#: Rate deviation stricter than this (in percentage points) => MEDIUM Indicator.
_DEFAULT_RATE_DEVIATION_THRESHOLD_PP = "-1.5"
#: More than this many active postings => HIGH Indicator.
_DEFAULT_MAX_ACTIVE_POSTINGS = 200
#: median_wage strictly above hourly_earnings * this ratio => HIGH Indicator.
_DEFAULT_WAGE_RATIO_THRESHOLD = "1.15"
#: Sector employment index strictly above 12-month rolling avg * this => SEASONAL.
_DEFAULT_SEASONAL_MULTIPLIER = "1.10"
#: Overall engine wall-clock budget (analyse + publish), milliseconds (Req 8.9).
_DEFAULT_ENGINE_BUDGET_MS = 500
#: Kafka topic all produced Indicators are published to (task 16.2).
_DEFAULT_INDICATORS_TOPIC = "indicators.regional-analysis"


if _HAS_PYDANTIC_SETTINGS:

    class Settings(BaseSettings):
        """Environment-driven configuration for the Regional Analysis Engine."""

        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
            case_sensitive=False,
        )

        # ── Deployment ──────────────────────────────────────────────
        app_env: str = "development"
        service_name: str = "regional-analysis-engine"
        regional_analysis_engine_port: int = 8020

        # ── Upstream adapters ───────────────────────────────────────
        statscan_adapter_url: str = _DEFAULT_STATSCAN_URL
        job_bank_adapter_url: str = _DEFAULT_JOB_BANK_URL
        #: Total per-adapter request timeout, seconds (design: 5 s).
        adapter_timeout_seconds: float = 5.0

        # ── Detection thresholds ────────────────────────────────────
        #: Deviation (regional - national) strictly below this (pp) => MEDIUM.
        rate_deviation_threshold_pp: Decimal = Decimal(
            _DEFAULT_RATE_DEVIATION_THRESHOLD_PP
        )
        #: Active postings strictly above this count => HIGH.
        max_active_postings: int = _DEFAULT_MAX_ACTIVE_POSTINGS
        #: median_wage strictly above hourly_earnings * this ratio => HIGH.
        wage_ratio_threshold: Decimal = Decimal(_DEFAULT_WAGE_RATIO_THRESHOLD)
        #: Sector index strictly above 12-month rolling avg * this => SEASONAL.
        seasonal_multiplier: Decimal = Decimal(_DEFAULT_SEASONAL_MULTIPLIER)

        # ── Time budget & streaming ─────────────────────────────────
        #: Overall analyse+publish wall-clock budget in milliseconds (Req 8.9).
        engine_budget_ms: int = _DEFAULT_ENGINE_BUDGET_MS
        #: Kafka topic for produced Indicators.
        indicators_topic: str = _DEFAULT_INDICATORS_TOPIC
        #: Kafka bootstrap servers for the Indicator producer.
        kafka_bootstrap_servers: str = "localhost:9092"

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local", "test"}

else:  # pragma: no cover - executed only when pydantic-settings is absent

    class Settings:  # type: ignore[no-redef]
        """Minimal environment-backed settings fallback."""

        def __init__(self) -> None:
            self.app_env = os.environ.get("APP_ENV", "development")
            self.service_name = os.environ.get(
                "SERVICE_NAME", "regional-analysis-engine"
            )
            self.regional_analysis_engine_port = int(
                os.environ.get("REGIONAL_ANALYSIS_ENGINE_PORT", "8020")
            )

            self.statscan_adapter_url = os.environ.get(
                "STATSCAN_ADAPTER_URL", _DEFAULT_STATSCAN_URL
            )
            self.job_bank_adapter_url = os.environ.get(
                "JOB_BANK_ADAPTER_URL", _DEFAULT_JOB_BANK_URL
            )
            self.adapter_timeout_seconds = float(
                os.environ.get("ADAPTER_TIMEOUT_SECONDS", "5.0")
            )

            self.rate_deviation_threshold_pp = Decimal(
                os.environ.get(
                    "RATE_DEVIATION_THRESHOLD_PP",
                    _DEFAULT_RATE_DEVIATION_THRESHOLD_PP,
                )
            )
            self.max_active_postings = int(
                os.environ.get("MAX_ACTIVE_POSTINGS", str(_DEFAULT_MAX_ACTIVE_POSTINGS))
            )
            self.wage_ratio_threshold = Decimal(
                os.environ.get("WAGE_RATIO_THRESHOLD", _DEFAULT_WAGE_RATIO_THRESHOLD)
            )
            self.seasonal_multiplier = Decimal(
                os.environ.get("SEASONAL_MULTIPLIER", _DEFAULT_SEASONAL_MULTIPLIER)
            )

            self.engine_budget_ms = int(
                os.environ.get("ENGINE_BUDGET_MS", str(_DEFAULT_ENGINE_BUDGET_MS))
            )
            self.indicators_topic = os.environ.get(
                "INDICATORS_TOPIC", _DEFAULT_INDICATORS_TOPIC
            )
            self.kafka_bootstrap_servers = os.environ.get(
                "KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"
            )

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local", "test"}


@lru_cache
def get_settings() -> "Settings":
    """Return a cached ``Settings`` instance."""
    return Settings()


__all__ = ["Settings", "get_settings"]
