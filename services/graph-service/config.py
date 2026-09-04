"""
services/graph-service/config.py
================================
Configuration for the Graph Service.

Uses ``pydantic-settings`` (falling back to ``pydantic.BaseSettings`` when the
standalone package is unavailable) so that all runtime configuration is sourced
from environment variables with sensible development defaults.

Connection settings for Neo4j are read from the environment variables
``NEO4J_URI``, ``NEO4J_USER`` and ``NEO4J_PASSWORD``.

Requirements: 5.1
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

from pydantic import Field


class Settings(BaseSettings):
    """Runtime settings for the Graph Service.

    Service-scoped values may be overridden with the ``GRAPH_SERVICE_`` prefix
    (e.g. ``GRAPH_SERVICE_ENVIRONMENT``).  The Neo4j connection settings use the
    conventional un-prefixed ``NEO4J_URI`` / ``NEO4J_USER`` / ``NEO4J_PASSWORD``
    environment variables (via ``validation_alias``) so they can be shared with
    other tooling and the ``podman-compose`` stack.
    """

    # --- Service identity ---
    service_name: str = "graph-service"
    environment: str = "dev"  # dev | test | staging | prod

    # --- Neo4j connection ---
    neo4j_uri: str = Field(default="bolt://localhost:7687", validation_alias="NEO4J_URI")
    neo4j_user: str = Field(default="neo4j", validation_alias="NEO4J_USER")
    neo4j_password: str = Field(default="neo4j", validation_alias="NEO4J_PASSWORD")
    neo4j_database: str = Field(default="neo4j", validation_alias="NEO4J_DATABASE")

    # --- Behaviour ---
    # When True (default outside prod), the repository runs in "test mode" using
    # an in-memory fake driver rather than opening a real Bolt connection. This
    # keeps unit tests and local development from requiring a live Neo4j.
    neo4j_test_mode: bool = Field(default=False, validation_alias="NEO4J_TEST_MODE")

    if _HAS_SETTINGS_PKG:
        model_config = SettingsConfigDict(  # type: ignore[misc]
            env_prefix="GRAPH_SERVICE_",
            env_file=".env",
            extra="ignore",
            populate_by_name=True,
        )
    else:  # pragma: no cover - pydantic v1 style config

        class Config:
            env_prefix = "GRAPH_SERVICE_"
            env_file = ".env"


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance."""
    return Settings()
