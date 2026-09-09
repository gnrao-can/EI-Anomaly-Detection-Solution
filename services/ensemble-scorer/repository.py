"""
services/ensemble-scorer/repository.py
======================================
Persistence for :class:`~shared.models.RiskAssessment` records (task 7.2).

``RiskAssessmentRepository`` writes each assessment to two data stores, matching
the design (design.md — "PostgreSQL Schema Overview" and "ClickHouse Schema
Overview"):

  - **PostgreSQL** (transactional): the ``risk_assessments`` row plus one
    ``shap_attributions`` row per attribution, via ``asyncpg``.
  - **ClickHouse** (analytics / 7-year audit): the ``risk_assessments_analytics``
    row plus the ``shap_attributions`` rows, via ``clickhouse-connect``.

All SQL uses parameterised queries (never string interpolation of values), so
the persistence path is safe against injection.

Because ``asyncpg`` and ``clickhouse-connect`` are not always present or
connectable in the CI sandbox, this module also provides
:class:`FakeRiskAssessmentRepository` — an in-memory test double implementing
the same ``async persist`` contract — so unit tests can exercise the wiring
without live databases.

Requirements: 9.3
"""

from __future__ import annotations

import os
from typing import Any, Optional, Protocol

try:  # package/relative import tolerance
    from shared.models import RiskAssessment
except ImportError:  # pragma: no cover
    from models import RiskAssessment  # type: ignore


# ---------------------------------------------------------------------------
# SQL / statement templates
# ---------------------------------------------------------------------------

# PostgreSQL uses $1, $2, ... positional placeholders (asyncpg convention).
_PG_INSERT_ASSESSMENT = """
INSERT INTO risk_assessments (
    assessment_id, claim_id, risk_score, risk_decision,
    shap_base_value, explanation_en, explanation_fr,
    model_version, scored_at
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
ON CONFLICT (assessment_id) DO NOTHING
"""

# Columns match the shap_attributions schema (migration 002): the table links
# to the assessment via assessment_id and has no claim_id / scored_at columns.
_PG_INSERT_ATTRIBUTION = """
INSERT INTO shap_attributions (
    assessment_id, indicator_id, feature_name,
    contribution, base_value
) VALUES ($1, $2, $3, $4, $5)
"""


class RiskAssessmentRepositoryProtocol(Protocol):
    """Structural contract implemented by both the real and fake repositories."""

    async def persist(self, assessment: RiskAssessment) -> None: ...


class RiskAssessmentRepository:
    """Persist ``RiskAssessment`` records to PostgreSQL and ClickHouse.

    Parameters
    ----------
    pg_pool:
        An ``asyncpg`` connection pool (or any object exposing an async
        ``acquire()`` context manager yielding a connection with ``execute`` /
        ``executemany``). When ``None`` the PostgreSQL write is skipped.
    clickhouse_client:
        A ``clickhouse-connect`` client (exposing ``insert``). When ``None`` the
        ClickHouse write is skipped.

    Notes
    -----
    Construct via :meth:`create` to build both clients from environment /
    connection settings. Direct construction is convenient for injecting
    pre-built or faked clients in tests.
    """

    def __init__(
        self,
        pg_pool: Optional[Any] = None,
        clickhouse_client: Optional[Any] = None,
    ) -> None:
        self._pg_pool = pg_pool
        self._ch = clickhouse_client

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    async def create(
        cls,
        pg_dsn: Optional[str] = None,
        clickhouse_settings: Optional[dict[str, Any]] = None,
    ) -> "RiskAssessmentRepository":
        """Build a repository with live asyncpg + clickhouse-connect clients.

        Any client whose settings are missing (or whose driver is unavailable)
        is left as ``None`` and its write is skipped. This lets the service run
        in degraded/dev environments without a full data tier.
        """
        pg_pool = await cls._build_pg_pool(pg_dsn)
        ch_client = cls._build_clickhouse_client(clickhouse_settings)
        return cls(pg_pool=pg_pool, clickhouse_client=ch_client)

    @staticmethod
    async def _build_pg_pool(pg_dsn: Optional[str]) -> Optional[Any]:
        dsn = pg_dsn or os.environ.get("EI_ADS_PG_DSN")
        if not dsn:
            return None
        try:
            import asyncpg  # local import

            return await asyncpg.create_pool(dsn=dsn)
        except Exception:  # noqa: BLE001 - degrade gracefully
            return None

    @staticmethod
    def _build_clickhouse_client(
        clickhouse_settings: Optional[dict[str, Any]],
    ) -> Optional[Any]:
        settings = clickhouse_settings
        if settings is None:
            host = os.environ.get("EI_ADS_CLICKHOUSE_HOST")
            if not host:
                return None
            settings = {
                "host": host,
                "port": int(os.environ.get("EI_ADS_CLICKHOUSE_PORT", "8123")),
                "username": os.environ.get("EI_ADS_CLICKHOUSE_USER", "default"),
                "password": os.environ.get("EI_ADS_CLICKHOUSE_PASSWORD", ""),
                "database": os.environ.get("EI_ADS_CLICKHOUSE_DB", "default"),
            }
        try:
            import clickhouse_connect  # local import

            return clickhouse_connect.get_client(**settings)
        except Exception:  # noqa: BLE001 - degrade gracefully
            return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def persist(self, assessment: RiskAssessment) -> None:
        """Persist a RiskAssessment to PostgreSQL and ClickHouse.

        Writes are best-effort per store: a configured store that fails raises,
        while an unconfigured store (``None`` client) is simply skipped so the
        service can run without a full data tier in development.
        """
        await self._persist_postgres(assessment)
        self._persist_clickhouse(assessment)

    # ------------------------------------------------------------------
    # PostgreSQL
    # ------------------------------------------------------------------

    async def _persist_postgres(self, assessment: RiskAssessment) -> None:
        if self._pg_pool is None:
            return
        async with self._pg_pool.acquire() as conn:
            # Prefer a transaction so the assessment and its attributions are
            # written atomically.
            tx = getattr(conn, "transaction", None)
            if callable(tx):
                async with conn.transaction():
                    await self._pg_write(conn, assessment)
            else:  # pragma: no cover - fakes without transaction support
                await self._pg_write(conn, assessment)

    async def _pg_write(self, conn: Any, assessment: RiskAssessment) -> None:
        await conn.execute(
            _PG_INSERT_ASSESSMENT,
            assessment.assessment_id,
            assessment.claim_id,
            int(assessment.risk_score),
            assessment.risk_decision.value,
            float(assessment.shap_base_value),
            assessment.explanation_en,
            assessment.explanation_fr,
            assessment.model_version,
            assessment.scored_at,
        )
        rows = [
            (
                assessment.assessment_id,
                a.indicator_id,
                a.feature_name,
                float(a.contribution),
                float(a.base_value),
            )
            for a in assessment.shap_attributions
        ]
        if rows:
            await conn.executemany(_PG_INSERT_ATTRIBUTION, rows)

    # ------------------------------------------------------------------
    # ClickHouse
    # ------------------------------------------------------------------

    _CH_ASSESSMENT_COLUMNS = [
        "assessment_id",
        "claim_id",
        "risk_score",
        "risk_decision",
        "shap_base_value",
        "explanation_en",
        "explanation_fr",
        "model_version",
        "scored_at",
    ]

    _CH_ATTRIBUTION_COLUMNS = [
        "assessment_id",
        "claim_id",
        "indicator_id",
        "feature_name",
        "contribution",
        "base_value",
        "scored_at",
    ]

    def _persist_clickhouse(self, assessment: RiskAssessment) -> None:
        if self._ch is None:
            return
        self._ch.insert(
            "risk_assessments_analytics",
            [
                [
                    str(assessment.assessment_id),
                    str(assessment.claim_id),
                    int(assessment.risk_score),
                    assessment.risk_decision.value,
                    float(assessment.shap_base_value),
                    assessment.explanation_en,
                    assessment.explanation_fr,
                    assessment.model_version,
                    assessment.scored_at,
                ]
            ],
            column_names=self._CH_ASSESSMENT_COLUMNS,
        )
        attribution_rows = [
            [
                str(assessment.assessment_id),
                str(assessment.claim_id),
                str(a.indicator_id),
                a.feature_name,
                float(a.contribution),
                float(a.base_value),
                assessment.scored_at,
            ]
            for a in assessment.shap_attributions
        ]
        if attribution_rows:
            self._ch.insert(
                "shap_attributions",
                attribution_rows,
                column_names=self._CH_ATTRIBUTION_COLUMNS,
            )


class FakeRiskAssessmentRepository:
    """In-memory test double implementing the ``persist`` contract.

    Captures every persisted :class:`RiskAssessment` in :attr:`saved` so unit
    tests can assert on what would have been written, without a live database.
    """

    def __init__(self) -> None:
        self.saved: list[RiskAssessment] = []

    async def persist(self, assessment: RiskAssessment) -> None:
        self.saved.append(assessment)

    # Convenience accessors for tests -----------------------------------

    @property
    def count(self) -> int:
        return len(self.saved)

    def last(self) -> Optional[RiskAssessment]:
        return self.saved[-1] if self.saved else None


__all__ = [
    "RiskAssessmentRepository",
    "FakeRiskAssessmentRepository",
    "RiskAssessmentRepositoryProtocol",
]
