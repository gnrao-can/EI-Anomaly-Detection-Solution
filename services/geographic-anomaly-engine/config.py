"""
services/geographic-anomaly-engine/config.py
=============================================
Centralised configuration for the Geographic Anomaly Engine.

The Geographic Anomaly Engine detects ghost employment via geospatial
implausibility. Task 12.1 establishes the resolution + distance foundation: it
resolves the claimant's residential postal code and the employer work-location
to coordinates via the GeoNames Adapter, then computes the Haversine geodesic
distance (rounded to 4 dp).

All runtime configuration is read from environment variables (see
``.env.example``) via ``pydantic-settings`` so the GeoNames Adapter URL and the
HTTP timeouts stay out of source control and can be overridden per environment.

Requirements: 4.1, 4.2, 4.10
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
        """Environment-driven configuration for the Geographic Anomaly Engine."""

        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
            case_sensitive=False,
        )

        # ── Deployment ──────────────────────────────────────────────
        app_env: str = "development"
        service_name: str = "geographic-anomaly-engine"
        geographic_anomaly_engine_port: int = 8022

        # ── GeoNames Adapter (coordinate resolution) ────────────────
        # Base URL of the GeoNames Adapter service. The engine calls
        # ``GET {url}/geonames/resolve/{postal_code}`` for both the residential
        # postal code and the employer work-location.
        geonames_adapter_url: str = "http://geonames-adapter:8014"
        # Per-request HTTP timeouts (seconds) for the adapter calls.
        geonames_connect_timeout_seconds: float = 2.0
        geonames_read_timeout_seconds: float = 3.0

        # ── MVR Adapter (vehicle ownership) ─────────────────────────
        # Queried only when distance > 100 km (task 12.2 / Req 4.3).
        mvr_adapter_url: str = "http://mvr-adapter:8023"
        mvr_connect_timeout_seconds: float = 1.0
        mvr_read_timeout_seconds: float = 3.0

        # ── CBSA Adapter (cross-border absence) ─────────────────────
        # Queried for absences during the employment period (Req 4.9).
        cbsa_adapter_url: str = "http://cbsa-adapter:8021"
        cbsa_connect_timeout_seconds: float = 1.0
        cbsa_read_timeout_seconds: float = 5.0

        # ── Internal service auth (Req 19.7) ────────────────────────
        # Service Bearer token presented on the internal GeoNames / MVR / CBSA
        # Adapter calls. Every EI-ADS endpoint validates a GCKey Bearer (Req
        # 19.7 / design "Token validation"), so the engine authenticates to
        # each adapter with its own service credential — mirroring the
        # orchestrator's ORCHESTRATOR_SERVICE_TOKEN and the History/Regional
        # engines' SERVICE_TOKEN pattern. Empty in unit tests (no header sent).
        service_token: str = ""

        # ── Wall-clock budget (Req 4.9) ─────────────────────────────
        # Total budget in milliseconds from claim.received to the final
        # Indicator being published. On budget exhaustion the engine returns
        # whatever Indicators it has already produced.
        analysis_budget_ms: int = 600

        # ── Kafka (Indicator publication) ───────────────────────────
        kafka_bootstrap_servers: str = "kafka:9092"
        indicators_topic: str = "indicators.geographic-anomaly"

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local", "test"}

else:  # pragma: no cover - executed only when pydantic-settings is absent

    class Settings:  # type: ignore[no-redef]
        """Minimal environment-backed settings fallback (no pydantic-settings)."""

        def __init__(self) -> None:
            self.app_env = os.environ.get("APP_ENV", "development")
            self.service_name = os.environ.get(
                "SERVICE_NAME", "geographic-anomaly-engine"
            )
            self.geographic_anomaly_engine_port = int(
                os.environ.get("GEOGRAPHIC_ANOMALY_ENGINE_PORT", "8022")
            )

            self.geonames_adapter_url = os.environ.get(
                "GEONAMES_ADAPTER_URL", "http://geonames-adapter:8014"
            )
            self.geonames_connect_timeout_seconds = float(
                os.environ.get("GEONAMES_CONNECT_TIMEOUT_SECONDS", "2.0")
            )
            self.geonames_read_timeout_seconds = float(
                os.environ.get("GEONAMES_READ_TIMEOUT_SECONDS", "3.0")
            )

            self.mvr_adapter_url = os.environ.get(
                "MVR_ADAPTER_URL", "http://mvr-adapter:8023"
            )
            self.mvr_connect_timeout_seconds = float(
                os.environ.get("MVR_CONNECT_TIMEOUT_SECONDS", "1.0")
            )
            self.mvr_read_timeout_seconds = float(
                os.environ.get("MVR_READ_TIMEOUT_SECONDS", "3.0")
            )

            self.cbsa_adapter_url = os.environ.get(
                "CBSA_ADAPTER_URL", "http://cbsa-adapter:8021"
            )
            self.cbsa_connect_timeout_seconds = float(
                os.environ.get("CBSA_CONNECT_TIMEOUT_SECONDS", "1.0")
            )
            self.cbsa_read_timeout_seconds = float(
                os.environ.get("CBSA_READ_TIMEOUT_SECONDS", "5.0")
            )

            # Internal service Bearer token (Req 19.7); empty in unit tests.
            self.service_token = os.environ.get("SERVICE_TOKEN", "")

            self.analysis_budget_ms = int(
                os.environ.get("ANALYSIS_BUDGET_MS", "600")
            )

            self.kafka_bootstrap_servers = os.environ.get(
                "KAFKA_BOOTSTRAP_SERVERS", "kafka:9092"
            )
            self.indicators_topic = os.environ.get(
                "INDICATORS_TOPIC", "indicators.geographic-anomaly"
            )

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local", "test"}


@lru_cache
def get_settings() -> "Settings":
    """Return a cached ``Settings`` instance."""
    return Settings()


__all__ = ["Settings", "get_settings"]
