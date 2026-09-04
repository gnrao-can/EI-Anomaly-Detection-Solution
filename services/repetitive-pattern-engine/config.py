"""
services/repetitive-pattern-engine/config.py
=============================================
Centralised configuration for the Repetitive Pattern Engine.

The Repetitive Pattern Engine detects systematic benefit gaming through
sequential claim patterns and employer-repetition analysis over a claimant's
60-month rolling claim history (retrieved from PostgreSQL by ``sin_hash``).

All runtime configuration is read from environment variables (see
``.env.example``) via ``pydantic-settings`` so that the PostgreSQL DSN and the
analysis windows / tolerances stay out of source control and can be overridden
per environment.

Task 11.1 covers the 60-month history retrieval, the 36-month employer
repetition window, and the +/-14-day sequential interval tolerance.

Requirements: 3.1, 3.2, 3.3, 3.8
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Optional


def _parse_hours_map(raw: str) -> dict[str, float]:
    """Parse a JSON string into a ``{region_code: hours}`` float map.

    Returns an empty map on blank or malformed input so a bad env var never
    crashes the engine — it simply falls back to the default threshold.
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in data.items():
        try:
            out[str(key)] = float(value)
        except (ValueError, TypeError):
            continue
    return out

try:  # pydantic-settings is the canonical settings library for pydantic v2
    from pydantic_settings import BaseSettings, SettingsConfigDict

    _HAS_PYDANTIC_SETTINGS = True
except ImportError:  # pragma: no cover - fallback for minimal environments
    _HAS_PYDANTIC_SETTINGS = False


if _HAS_PYDANTIC_SETTINGS:

    class Settings(BaseSettings):
        """Environment-driven configuration for the Repetitive Pattern Engine."""

        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
            case_sensitive=False,
        )

        # ── Deployment ──────────────────────────────────────────────
        app_env: str = "development"
        service_name: str = "repetitive-pattern-engine"
        repetitive_pattern_engine_port: int = 8021

        # ── PostgreSQL claim-history store ──────────────────────────
        # Data-source name for the transactional claims database. In dev/test
        # this may be blank; the caller uses an in-memory fake repository and the
        # engine emits DATA_UNAVAILABLE when no repository can be reached.
        pg_dsn: str = ""

        # ── Analysis windows / tolerances ───────────────────────────
        # 60-month rolling window used to retrieve claim history (Req 3.1).
        history_window_months: int = 60
        # 36-month window used to flag >= 3 claims from the same employer BN
        # (Req 3.2).
        employer_repetition_window_months: int = 36
        # Minimum number of same-employer claims within the window that triggers
        # a HIGH employer-repetition Indicator (Req 3.2). Boundary: 2 -> no flag,
        # 3 -> flag.
        employer_repetition_threshold: int = 3
        # +/- tolerance (in days) applied to inter-claim intervals when detecting
        # a repeating sequential cadence (Req 3.3, Req 3.8 / Property 1).
        interval_tolerance_days: int = 14

        # ── Hours-clustering (Req 3.4) ──────────────────────────────
        # A claim's insurable hours are flagged when within this many hours of
        # the region's minimum qualifying threshold AND the same near-threshold
        # pattern appears in at least ``hours_cluster_min_prior`` prior claims.
        hours_cluster_tolerance: float = 5.0
        # Minimum number of PRIOR claims that must also sit within tolerance of
        # the threshold for the clustering Indicator to fire (Req 3.4: "two or
        # more prior claims").
        hours_cluster_min_prior: int = 2
        # Fallback regional minimum qualifying threshold (insurable hours) used
        # when the claim's economic region is not present in the ESDC map below.
        # 700 hours is the standard EI entrance requirement.
        default_min_qualifying_hours: float = 700.0
        # JSON map of ESDC economic region code -> minimum qualifying hours,
        # supplied via the RPE_MIN_QUALIFYING_HOURS env var. Empty by default;
        # the default threshold above is used for any unmapped region.
        min_qualifying_hours_json: str = ""

        # ── LLM narrative similarity (Req 3.5, 3.6) ─────────────────
        # Base URL of the LLM Service. The engine POSTs to
        # ``{llm_service_url}/llm/similarity``.
        llm_service_url: str = "http://llm-service:8030"
        # Hard timeout (seconds) for the narrative-similarity call. Design and
        # Req 3.6 mandate 5 s; on timeout a NARRATIVE_ANALYSIS_TIMEOUT flag is
        # recorded and the engine proceeds without the narrative result.
        llm_timeout_seconds: float = 5.0
        # Cosine-similarity threshold above which two narratives are treated as
        # verbatim-equivalent, producing a MEDIUM Indicator (Req 3.5).
        narrative_similarity_threshold: float = 0.92

        # ── Wall-clock budget (Req 3.7) ─────────────────────────────
        # Milliseconds the engine may spend on analysis + publish, EXCLUDING any
        # LLM wait, before it must return. Enforced via asyncio timeout.
        analysis_budget_ms: int = 400

        # ── Kafka Indicator publishing (Req 3.2, 3.7) ───────────────
        # Bootstrap servers for the Kafka bus. Blank in dev/test (the caller
        # injects a fake publisher and no broker is required).
        kafka_bootstrap_servers: str = ""
        # Topic all Repetitive Pattern Engine Indicators are published to.
        indicator_topic: str = "indicators.repetitive-pattern"

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local", "test"}

        @property
        def db_configured(self) -> bool:
            """True only when a PostgreSQL DSN is configured."""
            return bool(self.pg_dsn)

        @property
        def min_qualifying_hours_map(self) -> dict[str, float]:
            """Parsed ESDC region-code -> minimum qualifying hours map."""
            return _parse_hours_map(self.min_qualifying_hours_json)

        def min_qualifying_hours(self, region_code: Optional[str]) -> float:
            """Return the minimum qualifying hours for *region_code*.

            Falls back to :attr:`default_min_qualifying_hours` when the region is
            unknown or unmapped.
            """
            if region_code:
                mapped = self.min_qualifying_hours_map.get(region_code)
                if mapped is not None:
                    return mapped
            return self.default_min_qualifying_hours

else:  # pragma: no cover - executed only when pydantic-settings is absent

    class Settings:  # type: ignore[no-redef]
        """Minimal environment-backed settings fallback (no pydantic-settings)."""

        def __init__(self) -> None:
            self.app_env = os.environ.get("APP_ENV", "development")
            self.service_name = os.environ.get(
                "SERVICE_NAME", "repetitive-pattern-engine"
            )
            self.repetitive_pattern_engine_port = int(
                os.environ.get("REPETITIVE_PATTERN_ENGINE_PORT", "8021")
            )

            self.pg_dsn = os.environ.get("PG_DSN", "")

            self.history_window_months = int(
                os.environ.get("HISTORY_WINDOW_MONTHS", "60")
            )
            self.employer_repetition_window_months = int(
                os.environ.get("EMPLOYER_REPETITION_WINDOW_MONTHS", "36")
            )
            self.employer_repetition_threshold = int(
                os.environ.get("EMPLOYER_REPETITION_THRESHOLD", "3")
            )
            self.interval_tolerance_days = int(
                os.environ.get("INTERVAL_TOLERANCE_DAYS", "14")
            )

            self.hours_cluster_tolerance = float(
                os.environ.get("HOURS_CLUSTER_TOLERANCE", "5.0")
            )
            self.hours_cluster_min_prior = int(
                os.environ.get("HOURS_CLUSTER_MIN_PRIOR", "2")
            )
            self.default_min_qualifying_hours = float(
                os.environ.get("DEFAULT_MIN_QUALIFYING_HOURS", "700.0")
            )
            self.min_qualifying_hours_json = os.environ.get(
                "RPE_MIN_QUALIFYING_HOURS", ""
            )

            self.llm_service_url = os.environ.get(
                "LLM_SERVICE_URL", "http://llm-service:8030"
            )
            self.llm_timeout_seconds = float(
                os.environ.get("LLM_TIMEOUT_SECONDS", "5.0")
            )
            self.narrative_similarity_threshold = float(
                os.environ.get("NARRATIVE_SIMILARITY_THRESHOLD", "0.92")
            )

            self.analysis_budget_ms = int(
                os.environ.get("ANALYSIS_BUDGET_MS", "400")
            )

            self.kafka_bootstrap_servers = os.environ.get(
                "KAFKA_BOOTSTRAP_SERVERS", ""
            )
            self.indicator_topic = os.environ.get(
                "INDICATOR_TOPIC", "indicators.repetitive-pattern"
            )

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local", "test"}

        @property
        def db_configured(self) -> bool:
            return bool(self.pg_dsn)

        @property
        def min_qualifying_hours_map(self) -> dict[str, float]:
            return _parse_hours_map(self.min_qualifying_hours_json)

        def min_qualifying_hours(self, region_code: Optional[str]) -> float:
            if region_code:
                mapped = self.min_qualifying_hours_map.get(region_code)
                if mapped is not None:
                    return mapped
            return self.default_min_qualifying_hours


@lru_cache
def get_settings() -> "Settings":
    """Return a cached ``Settings`` instance."""
    return Settings()


__all__ = ["Settings", "get_settings"]
