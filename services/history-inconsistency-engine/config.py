"""
services/history-inconsistency-engine/config.py
===============================================
Centralised configuration for the Employment History Inconsistency Engine
(task 14.1).

The engine cross-references ROE declarations against CRA T4 records. This module
holds the connection settings for the CRA Adapter, the 5-second CRA lookup
timeout (design "History Inconsistency Engine"), the $2,000 earnings-discrepancy
threshold (Requirement 6.4), and a development flag.

Configuration is read from environment variables (see ``.env.example``) via
``pydantic-settings`` so connection settings and thresholds stay out of source
control and can be overridden per environment.

Requirements: 6.1, 6.2, 6.3, 6.4, 6.10
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


if _HAS_PYDANTIC_SETTINGS:

    class Settings(BaseSettings):
        """Environment-driven configuration for the History Inconsistency Engine."""

        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
            case_sensitive=False,
        )

        # ── Deployment ──────────────────────────────────────────────
        app_env: str = "development"
        service_name: str = "history-inconsistency-engine"
        history_inconsistency_engine_port: int = 8010

        # ── CRA Adapter (T4 retrieval) ──────────────────────────────
        # Base URL of the CRA Adapter service. The engine calls
        # ``/cra/t4/{sin_hash}/{tax_year}/{employer_bn}`` on this host.
        cra_adapter_url: str = "http://cra-adapter:8011"
        # Total timeout (seconds) for the CRA T4 lookup. Design mandates 5 s.
        cra_timeout_seconds: float = 5.0

        # ── Earnings discrepancy detection ──────────────────────────
        # Absolute ROE-vs-T4 difference (CAD) above which a CRITICAL Indicator is
        # produced. Strict comparison: exactly this value -> NO flag; a cent more
        # -> flag (Requirement 6.4). Stored as a string and exposed as Decimal.
        discrepancy_threshold_cad: str = "2000"

        # ── LLM narrative contradiction (task 14.2) ─────────────────
        # Base URL of the LLM Service used for narrative contradiction analysis.
        llm_service_url: str = "http://llm-service:8020"
        # LLM narrative-classification timeout (seconds). Design mandates 10 s
        # (Requirement 6.7). This wait is EXCLUDED from the 500 ms budget (6.9).
        llm_timeout_seconds: float = 10.0

        # ── Analysis budget & Kafka (task 14.2) ─────────────────────
        # Wall-clock budget (ms) for the core (non-LLM) analysis and Indicator
        # publication (Requirement 6.9).
        analysis_budget_ms: int = 500
        # Kafka bootstrap servers and the Indicators topic for this engine.
        kafka_bootstrap_servers: str = "localhost:9092"
        indicators_topic: str = "indicators.history-inconsistency"

        @property
        def discrepancy_threshold(self) -> Decimal:
            """The discrepancy threshold as an exact ``Decimal``."""
            return Decimal(str(self.discrepancy_threshold_cad))

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local", "test"}

else:  # pragma: no cover - executed only when pydantic-settings is absent

    class Settings:  # type: ignore[no-redef]
        """Minimal environment-backed settings fallback (no pydantic-settings)."""

        def __init__(self) -> None:
            self.app_env = os.environ.get("APP_ENV", "development")
            self.service_name = os.environ.get(
                "SERVICE_NAME", "history-inconsistency-engine"
            )
            self.history_inconsistency_engine_port = int(
                os.environ.get("HISTORY_INCONSISTENCY_ENGINE_PORT", "8010")
            )

            self.cra_adapter_url = os.environ.get(
                "CRA_ADAPTER_URL", "http://cra-adapter:8011"
            )
            self.cra_timeout_seconds = float(
                os.environ.get("CRA_TIMEOUT_SECONDS", "5.0")
            )

            self.discrepancy_threshold_cad = os.environ.get(
                "DISCREPANCY_THRESHOLD_CAD", "2000"
            )

            self.llm_service_url = os.environ.get(
                "LLM_SERVICE_URL", "http://llm-service:8020"
            )
            self.llm_timeout_seconds = float(
                os.environ.get("LLM_TIMEOUT_SECONDS", "10.0")
            )
            self.analysis_budget_ms = int(
                os.environ.get("ANALYSIS_BUDGET_MS", "500")
            )
            self.kafka_bootstrap_servers = os.environ.get(
                "KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"
            )
            self.indicators_topic = os.environ.get(
                "INDICATORS_TOPIC", "indicators.history-inconsistency"
            )

        @property
        def discrepancy_threshold(self) -> Decimal:
            return Decimal(str(self.discrepancy_threshold_cad))

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local", "test"}


@lru_cache
def get_settings() -> "Settings":
    """Return a cached ``Settings`` instance."""
    return Settings()


__all__ = ["Settings", "get_settings"]
