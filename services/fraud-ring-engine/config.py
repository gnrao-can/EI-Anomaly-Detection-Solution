"""
services/fraud-ring-engine/config.py
=====================================
Centralised configuration for the Fraud Ring Engine (task 13.1).

The Fraud Ring Engine detects shell employers, coordinated mass layoffs, and
claimant community clusters using graph analytics. Task 13.1 covers the
shell-employer detection (via the Business Registry Adapter) and the
coordinated-layoff detection (over the 90-day separation cohort).

All runtime configuration is read from environment variables (see
``.env.example``) via ``pydantic-settings`` so that the Business Registry
Adapter URL, the Graph Service URL (used by task 13.2), the detection
thresholds, and the development flag stay out of source control and can be
overridden per environment.

Requirements: 5.2, 5.3, 5.4
"""

from __future__ import annotations

import os
from functools import lru_cache

try:  # pydantic-settings is the canonical settings library for pydantic v2
    from pydantic_settings import BaseSettings, SettingsConfigDict

    _HAS_PYDANTIC_SETTINGS = True
except ImportError:  # pragma: no cover - fallback for minimal environments
    _HAS_PYDANTIC_SETTINGS = False


if _HAS_PYDANTIC_SETTINGS:

    class Settings(BaseSettings):
        """Environment-driven configuration for the Fraud Ring Engine."""

        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
            case_sensitive=False,
        )

        # ── Deployment ──────────────────────────────────────────────
        app_env: str = "development"
        service_name: str = "fraud-ring-engine"
        fraud_ring_engine_port: int = 8023

        # ── Business Registry Adapter (shell-employer detection) ────
        business_registry_adapter_url: str = "http://business-registry-adapter:8012"
        # ── Graph Service (community detection — task 13.2) ─────────
        # Port 8006 matches the graph-service Containerfile's uvicorn port.
        graph_service_url: str = "http://graph-service:8006"

        # ── Internal service auth (Req 19.7) ────────────────────────
        # Service Bearer token presented on the internal Business Registry
        # Adapter + Graph Service calls. Every EI-ADS endpoint validates a GCKey
        # Bearer (Req 19.7 / design "Token validation"), so the engine
        # authenticates to each collaborator with its own service credential —
        # mirroring the orchestrator's ORCHESTRATOR_SERVICE_TOKEN and the
        # History/Regional/Geographic engines' SERVICE_TOKEN pattern. Empty in
        # unit tests (no header sent).
        service_token: str = ""

        # ── Timeouts ────────────────────────────────────────────────
        registry_timeout_seconds: float = 5.0
        graph_timeout_seconds: float = 5.0
        # Overall engine wall-clock budget (design: 800 ms).
        engine_budget_ms: int = 800

        # ── Shell-employer thresholds (Req 5.2) ─────────────────────
        # CRITICAL when the employer was registered within this many months
        # BEFORE the separation date AND the registered address is residential.
        shell_registration_window_months: int = 12

        # ── Coordinated-layoff thresholds (Req 5.3, 5.4) ────────────
        # Cohort is loaded over this many days BEFORE the separation date.
        coordinated_layoff_window_days: int = 90
        # CRITICAL when at least this fraction of the cohort share the same
        # separation calendar date. Boundary: 79% -> no flag, 80% -> flag.
        coordinated_layoff_same_date_fraction: float = 0.80
        # AND all their ROE submissions fall within any rolling window of this
        # many hours.
        coordinated_layoff_roe_window_hours: int = 24

        # ── Community-cluster + hours-band thresholds (Req 5.5–5.7) ─
        # CRITICAL when at least this fraction of the employer cohort share a
        # postal FSA or surname_hash. Boundary: 39% -> no flag, 40% -> flag.
        community_shared_attribute_fraction: float = 0.40
        # HIGH when all cohort insurable hours fall within this band width AND
        # at or above the minimum qualifying threshold.
        hours_band_width: float = 10.0
        # Cohorts smaller than this are NOT_EVALUATED for the hours-band check.
        hours_band_min_cohort: int = 3
        # Minimum qualifying insurable-hours threshold for the active benefit
        # period configuration (ESDC config; overridable per environment).
        min_qualifying_hours: float = 700.0
        # Cohort window (months) used by the Graph Service community detection.
        community_cohort_window_months: int = 24

        # ── Kafka (Indicator publishing — Req 5.8) ──────────────────
        kafka_bootstrap_servers: str = "kafka:9092"
        fraud_ring_indicators_topic: str = "indicators.fraud-ring"

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local", "test"}

else:  # pragma: no cover - executed only when pydantic-settings is absent

    class Settings:  # type: ignore[no-redef]
        """Minimal environment-backed settings fallback (no pydantic-settings)."""

        def __init__(self) -> None:
            self.app_env = os.environ.get("APP_ENV", "development")
            self.service_name = os.environ.get("SERVICE_NAME", "fraud-ring-engine")
            self.fraud_ring_engine_port = int(
                os.environ.get("FRAUD_RING_ENGINE_PORT", "8023")
            )
            self.business_registry_adapter_url = os.environ.get(
                "BUSINESS_REGISTRY_ADAPTER_URL",
                "http://business-registry-adapter:8012",
            )
            self.graph_service_url = os.environ.get(
                "GRAPH_SERVICE_URL", "http://graph-service:8006"
            )
            # Internal service Bearer token (Req 19.7); empty in unit tests.
            self.service_token = os.environ.get("SERVICE_TOKEN", "")
            self.registry_timeout_seconds = float(
                os.environ.get("REGISTRY_TIMEOUT_SECONDS", "5.0")
            )
            self.graph_timeout_seconds = float(
                os.environ.get("GRAPH_TIMEOUT_SECONDS", "5.0")
            )
            self.engine_budget_ms = int(os.environ.get("ENGINE_BUDGET_MS", "800"))
            self.shell_registration_window_months = int(
                os.environ.get("SHELL_REGISTRATION_WINDOW_MONTHS", "12")
            )
            self.coordinated_layoff_window_days = int(
                os.environ.get("COORDINATED_LAYOFF_WINDOW_DAYS", "90")
            )
            self.coordinated_layoff_same_date_fraction = float(
                os.environ.get("COORDINATED_LAYOFF_SAME_DATE_FRACTION", "0.80")
            )
            self.coordinated_layoff_roe_window_hours = int(
                os.environ.get("COORDINATED_LAYOFF_ROE_WINDOW_HOURS", "24")
            )
            self.community_shared_attribute_fraction = float(
                os.environ.get("COMMUNITY_SHARED_ATTRIBUTE_FRACTION", "0.40")
            )
            self.hours_band_width = float(
                os.environ.get("HOURS_BAND_WIDTH", "10.0")
            )
            self.hours_band_min_cohort = int(
                os.environ.get("HOURS_BAND_MIN_COHORT", "3")
            )
            self.min_qualifying_hours = float(
                os.environ.get("MIN_QUALIFYING_HOURS", "700.0")
            )
            self.community_cohort_window_months = int(
                os.environ.get("COMMUNITY_COHORT_WINDOW_MONTHS", "24")
            )
            self.kafka_bootstrap_servers = os.environ.get(
                "KAFKA_BOOTSTRAP_SERVERS", "kafka:9092"
            )
            self.fraud_ring_indicators_topic = os.environ.get(
                "FRAUD_RING_INDICATORS_TOPIC", "indicators.fraud-ring"
            )

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local", "test"}


@lru_cache
def get_settings() -> "Settings":
    """Return a cached ``Settings`` instance."""
    return Settings()


__all__ = ["Settings", "get_settings"]
