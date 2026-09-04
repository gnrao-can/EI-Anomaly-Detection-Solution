"""
services/claim-intake/privacy.py
================================
Privacy Act / PIPEDA compliance layer for the Claim Intake Service.

Implements the persistence + orchestration logic behind two claimant-facing
Privacy Act endpoints (wired into the FastAPI app in ``main.py``, task 28.1):

  - ``GET  /api/v1/privacy/access`` — return, in a machine-readable format, all
    records held for a given claimant (keyed by ``sin_hash``) across PostgreSQL
    (``claims`` / ``indicators`` / ``risk_assessments``) and the ClickHouse
    analytics/audit store, satisfying the 30-day Privacy Act *access* SLA
    (Requirement 19.4).
  - ``DELETE /api/v1/privacy/delete`` — anonymise the identifying fields of the
    records tied to a ``sin_hash`` (in ``claims`` / ``indicators`` /
    ``risk_assessments``) while *preserving* the aggregate / statistical rows
    required for audit and the 7-year retention schedule, satisfying the 30-day
    Privacy Act *erasure* SLA (Requirement 19.5).

Design notes
------------
The raw SIN is never persisted anywhere (Requirement 1.11); a claimant is keyed
by ``sin_hash`` = SHA-256(normalised SIN + per-environment salt). The privacy
endpoints therefore operate purely on ``sin_hash``:

  * For a **CLAIMANT** the ``sin_hash`` is derived from the *own* SIN carried in
    their validated GCKey token — a claimant may only ever access or erase their
    own data (own-data-only).
  * For an **ADMINISTRATOR** the target ``sin_hash`` may be supplied directly (or
    a raw SIN which is hashed here), so an operator can service a Privacy Act
    request on a claimant's behalf.

"Anonymisation, not deletion": erasure never removes rows (that would break the
7-year audit retention and the aggregate/statistical records). Instead it nulls
/ tombstones the *identifying* columns — ``sin_hash``, ``employer_bn``,
residential postal / address fields, and free-text explanation / payload fields
that could carry PII — while leaving the numeric, coded, and timestamp columns
(insurable hours/earnings, economic region, NOC code, risk score, severity,
engine, dates) intact for statistical use.

Both a real (asyncpg + ClickHouse) and an in-memory fake implementation are
provided so the endpoints can be unit-tested without any live database.

Requirements: 19.4, 19.5, 1.11, 19.1
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Anonymisation sentinel
# ---------------------------------------------------------------------------

# Value written into identifying string columns on erasure. Chosen so it is
# obviously a tombstone (never a real hash / BN) and is stable across records so
# anonymised rows can be recognised in audits.
ANONYMISED_TOKEN = "ANONYMISED"

# The set of PostgreSQL tables an access/erasure request touches, and the
# identifying columns anonymised in each. Numeric / coded / timestamp columns are
# deliberately excluded so the aggregate/statistical value of the row survives.
_CLAIM_IDENTIFYING_COLUMNS = (
    "sin_hash",
    "employer_bn",
    "residential_postal_code",
    "residential_address_hash",
    "employer_work_location",
    "separation_reason",
)
_INDICATOR_IDENTIFYING_COLUMNS = (
    "title",
    "explanation_en",
    "explanation_fr",
    "payload",
)
_RISK_ASSESSMENT_IDENTIFYING_COLUMNS = (
    "explanation_en",
    "explanation_fr",
)


__all__ = [
    "ANONYMISED_TOKEN",
    "AbstractPrivacyRepository",
    "PrivacyRepository",
    "FakePrivacyRepository",
    "AccessReport",
    "DeletionReceipt",
]


# ---------------------------------------------------------------------------
# Result value objects
# ---------------------------------------------------------------------------


class AccessReport(dict):
    """Machine-readable Privacy Act access report.

    A plain ``dict`` subclass (so it serialises directly to JSON) with a fixed
    top-level shape::

        {
          "sin_hash": "...",
          "generated_at": "<ISO-8601 UTC>",
          "postgres": {"claims": [...], "indicators": [...],
                       "risk_assessments": [...]},
          "clickhouse": {"claims_analytics": [...], "indicators_analytics": [...],
                         "risk_assessments_analytics": [...]},
          "record_count": <int>
        }
    """


class DeletionReceipt(dict):
    """Machine-readable receipt describing what an erasure anonymised.

    Shape::

        {
          "sin_hash": "...",
          "completed_at": "<ISO-8601 UTC>",
          "anonymised": {"claims": <int>, "indicators": <int>,
                         "risk_assessments": <int>},
          "preserved_aggregates": true
        }
    """


# ---------------------------------------------------------------------------
# Repository protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AbstractPrivacyRepository(Protocol):
    """Structural interface shared by the real and fake privacy repositories."""

    async def access_records(self, sin_hash: str) -> AccessReport:  # pragma: no cover
        ...

    async def anonymise_records(
        self, sin_hash: str
    ) -> DeletionReceipt:  # pragma: no cover
        ...


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# asyncpg + ClickHouse backed repository
# ---------------------------------------------------------------------------

# All queries are fully parameterised (no string interpolation of values) to
# prevent SQL injection. ClickHouse tables key on claim_id, so analytics rows are
# resolved via the claim_ids discovered in PostgreSQL for the sin_hash.

_SELECT_CLAIMS_SQL = "SELECT * FROM claims WHERE sin_hash = $1 ORDER BY received_at"
_SELECT_INDICATORS_SQL = (
    "SELECT i.* FROM indicators i "
    "JOIN claims c ON c.claim_id = i.claim_id "
    "WHERE c.sin_hash = $1 ORDER BY i.produced_at"
)
_SELECT_RISK_ASSESSMENTS_SQL = (
    "SELECT r.* FROM risk_assessments r "
    "JOIN claims c ON c.claim_id = r.claim_id "
    "WHERE c.sin_hash = $1 ORDER BY r.scored_at"
)

# Anonymisation updates. Only identifying columns are overwritten; numeric /
# coded / timestamp columns are untouched so aggregate records are preserved.
_ANON_CLAIMS_SQL = """
UPDATE claims
SET sin_hash = $2,
    employer_bn = $2,
    residential_postal_code = $2,
    residential_address_hash = $2,
    employer_work_location = $2,
    separation_reason = $2
WHERE sin_hash = $1
RETURNING claim_id
"""
_ANON_INDICATORS_SQL = """
UPDATE indicators i
SET title = $2,
    explanation_en = $2,
    explanation_fr = $2,
    payload = '{}'::jsonb
FROM claims c
WHERE i.claim_id = c.claim_id AND c.claim_id = ANY($1::uuid[])
RETURNING i.indicator_id
"""
_ANON_RISK_ASSESSMENTS_SQL = """
UPDATE risk_assessments
SET explanation_en = $2,
    explanation_fr = $2
WHERE claim_id = ANY($1::uuid[])
RETURNING assessment_id
"""


class PrivacyRepository:
    """PostgreSQL (+ optional ClickHouse) backed Privacy Act repository.

    Parameters
    ----------
    pool:
        An ``asyncpg`` connection pool. Required.
    clickhouse_client:
        Optional async ClickHouse client exposing ``async fetch(query, params)``.
        When ``None`` the ClickHouse portion of an access report is returned
        empty (PostgreSQL remains the system of record for the response).
    """

    def __init__(
        self,
        pool: Any,
        clickhouse_client: Optional[Any] = None,
    ) -> None:
        if pool is None:
            raise ValueError("PrivacyRepository requires a non-null asyncpg pool.")
        self._pool = pool
        self._ch = clickhouse_client

    async def access_records(self, sin_hash: str) -> AccessReport:
        """Return every PostgreSQL + ClickHouse record held for *sin_hash*."""
        claims = await self._pool.fetch(_SELECT_CLAIMS_SQL, sin_hash)
        indicators = await self._pool.fetch(_SELECT_INDICATORS_SQL, sin_hash)
        risk_assessments = await self._pool.fetch(
            _SELECT_RISK_ASSESSMENTS_SQL, sin_hash
        )

        pg = {
            "claims": [dict(r) for r in claims],
            "indicators": [dict(r) for r in indicators],
            "risk_assessments": [dict(r) for r in risk_assessments],
        }
        claim_ids = [r["claim_id"] for r in claims]
        clickhouse = await self._fetch_clickhouse(sin_hash, claim_ids)

        record_count = (
            len(pg["claims"])
            + len(pg["indicators"])
            + len(pg["risk_assessments"])
            + sum(len(v) for v in clickhouse.values())
        )
        return AccessReport(
            sin_hash=sin_hash,
            generated_at=_now_iso(),
            postgres=pg,
            clickhouse=clickhouse,
            record_count=record_count,
        )

    async def _fetch_clickhouse(
        self, sin_hash: str, claim_ids: list[Any]
    ) -> dict[str, list[dict[str, Any]]]:
        """Best-effort fetch of ClickHouse analytics rows for the claim ids."""
        empty = {
            "claims_analytics": [],
            "indicators_analytics": [],
            "risk_assessments_analytics": [],
        }
        if self._ch is None or not claim_ids:
            return empty
        ids = [str(cid) for cid in claim_ids]
        try:
            claims_a = await self._ch.fetch(
                "SELECT * FROM claims_analytics WHERE claim_id IN %(ids)s",
                {"ids": ids},
            )
            indicators_a = await self._ch.fetch(
                "SELECT * FROM indicators_analytics WHERE claim_id IN %(ids)s",
                {"ids": ids},
            )
            risk_a = await self._ch.fetch(
                "SELECT * FROM risk_assessments_analytics WHERE claim_id IN %(ids)s",
                {"ids": ids},
            )
        except Exception:  # noqa: BLE001 - ClickHouse is best-effort for access
            return empty
        return {
            "claims_analytics": [dict(r) for r in claims_a],
            "indicators_analytics": [dict(r) for r in indicators_a],
            "risk_assessments_analytics": [dict(r) for r in risk_a],
        }

    async def anonymise_records(self, sin_hash: str) -> DeletionReceipt:
        """Anonymise identifying fields for *sin_hash*; preserve aggregate rows.

        Runs inside a single transaction so the claims / indicators /
        risk_assessments updates are atomic. Rows are never deleted — only their
        identifying columns are tombstoned — so the statistical / aggregate
        records required for audit survive.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                claim_rows = await conn.fetch(
                    _ANON_CLAIMS_SQL, sin_hash, ANONYMISED_TOKEN
                )
                # Capture claim_ids BEFORE the sin_hash is overwritten so the
                # child tables can still be resolved. RETURNING gives us those.
                claim_ids = [r["claim_id"] for r in claim_rows]

                indicator_rows: list[Any] = []
                risk_rows: list[Any] = []
                if claim_ids:
                    indicator_rows = await conn.fetch(
                        _ANON_INDICATORS_SQL, claim_ids, ANONYMISED_TOKEN
                    )
                    risk_rows = await conn.fetch(
                        _ANON_RISK_ASSESSMENTS_SQL, claim_ids, ANONYMISED_TOKEN
                    )

        return DeletionReceipt(
            sin_hash=sin_hash,
            completed_at=_now_iso(),
            anonymised={
                "claims": len(claim_rows),
                "indicators": len(indicator_rows),
                "risk_assessments": len(risk_rows),
            },
            preserved_aggregates=True,
        )


# ---------------------------------------------------------------------------
# In-memory fake for unit tests / local dev
# ---------------------------------------------------------------------------


class FakePrivacyRepository:
    """In-memory Privacy Act repository stand-in for tests and local dev.

    Stores ``claims``, ``indicators`` and ``risk_assessments`` as lists of plain
    dicts. ``claims`` rows are keyed by ``sin_hash`` and carry a ``claim_id``;
    ``indicators`` / ``risk_assessments`` rows carry a ``claim_id`` foreign key.
    ClickHouse analytics rows may be seeded per table under ``clickhouse``.
    """

    def __init__(self) -> None:
        self.claims: list[dict[str, Any]] = []
        self.indicators: list[dict[str, Any]] = []
        self.risk_assessments: list[dict[str, Any]] = []
        self.clickhouse: dict[str, list[dict[str, Any]]] = {
            "claims_analytics": [],
            "indicators_analytics": [],
            "risk_assessments_analytics": [],
        }

    # -- seeding helpers -------------------------------------------------

    def add_claim(self, claim: dict[str, Any]) -> None:
        self.claims.append(dict(claim))

    def add_indicator(self, indicator: dict[str, Any]) -> None:
        self.indicators.append(dict(indicator))

    def add_risk_assessment(self, assessment: dict[str, Any]) -> None:
        self.risk_assessments.append(dict(assessment))

    def _claim_ids_for(self, sin_hash: str) -> set[Any]:
        return {c["claim_id"] for c in self.claims if c.get("sin_hash") == sin_hash}

    # -- repository interface -------------------------------------------

    async def access_records(self, sin_hash: str) -> AccessReport:
        claim_ids = self._claim_ids_for(sin_hash)
        pg_claims = [dict(c) for c in self.claims if c.get("sin_hash") == sin_hash]
        pg_indicators = [
            dict(i) for i in self.indicators if i.get("claim_id") in claim_ids
        ]
        pg_risk = [
            dict(r) for r in self.risk_assessments if r.get("claim_id") in claim_ids
        ]

        ch = {
            table: [dict(r) for r in rows if r.get("claim_id") in claim_ids]
            for table, rows in self.clickhouse.items()
        }

        record_count = (
            len(pg_claims)
            + len(pg_indicators)
            + len(pg_risk)
            + sum(len(v) for v in ch.values())
        )
        return AccessReport(
            sin_hash=sin_hash,
            generated_at=_now_iso(),
            postgres={
                "claims": pg_claims,
                "indicators": pg_indicators,
                "risk_assessments": pg_risk,
            },
            clickhouse=ch,
            record_count=record_count,
        )

    async def anonymise_records(self, sin_hash: str) -> DeletionReceipt:
        claim_ids = self._claim_ids_for(sin_hash)

        claims_anon = 0
        for claim in self.claims:
            if claim.get("sin_hash") == sin_hash:
                for col in _CLAIM_IDENTIFYING_COLUMNS:
                    if col == "payload":
                        continue
                    if col in claim:
                        claim[col] = ANONYMISED_TOKEN
                claims_anon += 1

        indicators_anon = 0
        for ind in self.indicators:
            if ind.get("claim_id") in claim_ids:
                for col in _INDICATOR_IDENTIFYING_COLUMNS:
                    if col == "payload":
                        ind[col] = {}
                    elif col in ind:
                        ind[col] = ANONYMISED_TOKEN
                indicators_anon += 1

        risk_anon = 0
        for ra in self.risk_assessments:
            if ra.get("claim_id") in claim_ids:
                for col in _RISK_ASSESSMENT_IDENTIFYING_COLUMNS:
                    if col in ra:
                        ra[col] = ANONYMISED_TOKEN
                risk_anon += 1

        return DeletionReceipt(
            sin_hash=sin_hash,
            completed_at=_now_iso(),
            anonymised={
                "claims": claims_anon,
                "indicators": indicators_anon,
                "risk_assessments": risk_anon,
            },
            preserved_aggregates=True,
        )
