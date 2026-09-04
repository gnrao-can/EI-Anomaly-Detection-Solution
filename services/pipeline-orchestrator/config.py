"""
services/pipeline-orchestrator/config.py
========================================
Configuration for the Detection Pipeline Orchestrator service.

Uses ``pydantic-settings`` (falling back to ``pydantic.BaseSettings`` when the
standalone package is unavailable) so that all runtime configuration is sourced
from environment variables with sensible development defaults.

The orchestrator subscribes to both ``claim.received`` and ``roe.received``
events, correlates them by ``claim_id`` using Redis as the pairing store, and —
once both have arrived (or the correlation window elapses) — triggers parallel
detection-engine dispatch. Two time budgets from the design's "Time Budget
Allocation" table are captured here:

  - ``correlation_timeout_seconds`` = 60 s — the window during which the first
    event of a pair waits for its complement (design: "configurable correlation
    timeout of 60 s"). This equals ``ORCHESTRATOR_PAIRING_TTL_SECONDS`` in
    ``shared/cache_keys.py``.
  - ``dispatch_budget_ms`` = 100 ms — after a pairing is detected, engine
    dispatch must be triggered within 100 ms of the later event's timestamp
    (design: "Pipeline initiation after both events | 100 ms").

Requirements: 2.1
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
    """Runtime settings for the Pipeline Orchestrator.

    All values may be overridden via environment variables prefixed with
    ``ORCHESTRATOR_`` (e.g. ``ORCHESTRATOR_KAFKA_BOOTSTRAP_SERVERS``).
    """

    # --- Service identity ---
    service_name: str = "pipeline-orchestrator"
    environment: str = "dev"  # dev | test | staging | prod

    # --- Kafka ---
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_claim_topic: str = "claim.received"
    kafka_roe_topic: str = "roe.received"
    kafka_consumer_group: str = "pipeline-orchestrator"
    # When True (default in dev), a failure to connect/consume from Kafka is
    # logged and degraded gracefully instead of raising. Set False in prod.
    kafka_degrade_gracefully: bool = True

    # --- Redis (event-pairing store) ---
    redis_url: str = "redis://localhost:6379"
    redis_cluster_mode: bool = False
    redis_password: str | None = None

    # --- Correlation / dispatch budgets ---
    # 60-second correlation window (design + ORCHESTRATOR_PAIRING_TTL_SECONDS).
    correlation_timeout_seconds: int = 60
    # 100 ms budget to trigger dispatch after the later event's timestamp.
    dispatch_budget_ms: int = 100

    if _HAS_SETTINGS_PKG:
        model_config = SettingsConfigDict(  # type: ignore[misc]
            env_prefix="ORCHESTRATOR_",
            env_file=".env",
            extra="ignore",
        )
    else:  # pragma: no cover - pydantic v1 style config

        class Config:
            env_prefix = "ORCHESTRATOR_"
            env_file = ".env"


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance."""
    return Settings()


__all__ = ["Settings", "get_settings"]
