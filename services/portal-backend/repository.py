"""
services/portal-backend/repository.py
=====================================
Read/write persistence layer for the Portal BFF.

Provides two interchangeable implementations of the same async interface:

  * :class:`PortalRepository`    — backed by an ``asyncpg`` pool, querying the
    ``claims`` / ``risk_assessments`` / ``indicators`` / ``shap_attributions``
    tables (see ``scripts/migrations``).
  * :class:`FakePortalRepository` — an in-memory stand-in for unit tests and
    local development when PostgreSQL is unavailable, mirroring the graceful
    degradation used by ``services/claim-intake``.

The shapes returned here are already in the **snake_case contract** the SPA
parsers read (``services/claimant-portal/src/api/*.ts``): ``claim_id``,
``risk_score``, ``risk_decision``, ``received_at``, ``employer_bn``,
``claimant_name``, ``claimant_sin``, ``address``, ``shap_base_value``,
``shap_attributions[{indicator_id, feature_name, contribution}]``,
``indicators[{indicator_id, engine, severity, title, explanation_en,
explanation_fr, shap_contribution}]``, ``explanation_en`` / ``explanation_fr``,
``disposition`` and ``case_notes``.

PII note
--------
The ``claims`` table intentionally does **not** store raw claimant name, raw
SIN, or full address (Protected B — only ``sin_hash`` and the postal FSA are
persisted; see ``001_create_claims.sql``). The real repository therefore derives
non-sensitive display surrogates for those fields from what is stored. Masking
for the REVIEWER role (Req 22.6) is applied server-side by
:func:`mask_flagged` / :func:`mask_detail`, never by the client.

Requirements: 19.7, 22.1, 22.2, 22.6
"""

from __future__ import annotations

import copy
from typing import Any, Optional

try:  # asyncpg is a runtime-only dependency; guard so the module imports.
    import asyncpg  # type: ignore

    _HAS_ASYNCPG = True
except ImportError:  # pragma: no cover
    asyncpg = None  # type: ignore
    _HAS_ASYNCPG = False


__all__ = [
    "FLAGGED_DECISIONS",
    "PortalRepository",
    "FakePortalRepository",
    "map_db_status_to_view",
    "mask_flagged",
    "mask_detail",
    "mask_sin",
]

# Risk_Decisions surfaced in the investigator queue (Req 22.1). The SPA rejects
# any other value, so the queue query filters to exactly these.
FLAGGED_DECISIONS = ("FLAG_FOR_REVIEW", "INVESTIGATE")

# Map the persisted claims.status lifecycle (RECEIVED|PROCESSING|SCORED|
# INVESTIGATE|APPROVED|REJECTED) onto the ClaimStatus the SPA understands
# (RECEIVED|PROCESSING|DECIDED|UNDER_REVIEW|CLOSED). The SPA discards unknown
# statuses, so this mapping is part of the contract.
_DB_STATUS_TO_VIEW = {
    "RECEIVED": "RECEIVED",
    "PROCESSING": "PROCESSING",
    "SCORED": "DECIDED",
    "INVESTIGATE": "UNDER_REVIEW",
    "APPROVED": "CLOSED",
    "REJECTED": "CLOSED",
}


def map_db_status_to_view(db_status: Optional[str]) -> str:
    """Translate a persisted claim status to the SPA-facing ClaimStatus."""
    return _DB_STATUS_TO_VIEW.get((db_status or "").upper(), "PROCESSING")


# ---------------------------------------------------------------------------
# PII masking (server-side, authoritative — Req 22.6)
# ---------------------------------------------------------------------------


def mask_sin(sin: Optional[str]) -> Optional[str]:
    """Mask a SIN to its last three digits (e.g. ``***-**-321``)."""
    if not sin:
        return sin
    digits = [c for c in sin if c.isdigit()]
    if len(digits) < 3:
        return "***"
    return "***-**-" + "".join(digits[-3:])


def mask_flagged(row: dict[str, Any], *, unmasked: bool) -> dict[str, Any]:
    """Return a flagged-claim row with PII masked unless *unmasked*.

    Masks ``claimant_name``, ``claimant_sin`` and ``address`` for REVIEWER-role
    callers while leaving risk data intact for triage (Req 22.6).
    """
    out = dict(row)
    if unmasked:
        return out
    out["claimant_name"] = None
    out["claimant_sin"] = mask_sin(out.get("claimant_sin"))
    out["address"] = None
    return out


def mask_detail(detail: dict[str, Any], *, unmasked: bool) -> dict[str, Any]:
    """Return a claim-detail dict with PII masked unless *unmasked* (Req 22.6)."""
    out = dict(detail)
    if unmasked:
        return out
    out["claimant_name"] = None
    out["claimant_sin"] = mask_sin(out.get("claimant_sin"))
    return out


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

# Claimant status view: latest assessment (if any) joined to the claim.
_CLAIM_STATUS_SQL = """
SELECT
    c.claim_id,
    c.status              AS db_status,
    c.sin_hash,
    c.received_at,
    ra.risk_score,
    ra.risk_decision,
    ra.explanation_en,
    ra.explanation_fr,
    ra.scored_at
FROM claims c
LEFT JOIN LATERAL (
    SELECT risk_score, risk_decision, explanation_en, explanation_fr, scored_at
    FROM risk_assessments
    WHERE claim_id = c.claim_id
    ORDER BY scored_at DESC
    LIMIT 1
) ra ON TRUE
WHERE c.claim_id = $1
"""

# Investigator queue: every claim whose latest decision is flagged.
_FLAGGED_QUEUE_SQL = """
SELECT
    c.claim_id,
    c.employer_bn,
    c.received_at,
    c.sin_hash,
    ra.risk_score,
    ra.risk_decision
FROM claims c
JOIN LATERAL (
    SELECT risk_score, risk_decision
    FROM risk_assessments
    WHERE claim_id = c.claim_id
    ORDER BY scored_at DESC
    LIMIT 1
) ra ON TRUE
WHERE ra.risk_decision = ANY($1::text[])
ORDER BY ra.risk_score DESC, c.received_at DESC
"""

_CLAIM_DETAIL_SQL = """
SELECT
    c.claim_id,
    c.employer_bn,
    c.sin_hash,
    c.received_at,
    ra.assessment_id,
    ra.risk_score,
    ra.risk_decision,
    ra.shap_base_value,
    ra.explanation_en,
    ra.explanation_fr,
    cd.disposition,
    cd.case_notes
FROM claims c
JOIN LATERAL (
    SELECT assessment_id, risk_score, risk_decision, shap_base_value,
           explanation_en, explanation_fr
    FROM risk_assessments
    WHERE claim_id = c.claim_id
    ORDER BY scored_at DESC
    LIMIT 1
) ra ON TRUE
LEFT JOIN LATERAL (
    SELECT disposition, case_notes
    FROM claim_dispositions
    WHERE claim_id = c.claim_id
    ORDER BY created_at DESC
    LIMIT 1
) cd ON TRUE
WHERE c.claim_id = $1
"""

_INDICATORS_SQL = """
SELECT indicator_id, engine_type, severity, title,
       explanation_en, explanation_fr, shap_contribution
FROM indicators
WHERE claim_id = $1
ORDER BY produced_at ASC
"""

_SHAP_SQL = """
SELECT indicator_id, feature_name, contribution
FROM shap_attributions
WHERE assessment_id = $1
"""

# Persist a freshly submitted claim (dev-mode direct-persist path). Every value
# is bound as a parameter — no user value is interpolated into the SQL string.
# postal_code is the FSA only (residential_postal_code VARCHAR(3)); it and the
# ROE document id are optional. status defaults to 'RECEIVED', language to 'en'.
_INSERT_CLAIM_SQL = """
INSERT INTO claims (
    claim_id,
    sin_hash,
    employer_bn,
    insurable_hours,
    insurable_earnings,
    separation_date,
    separation_reason,
    residential_postal_code,
    roe_document_id,
    status,
    language
) VALUES (
    $1, $2, $3, $4, $5, $6::date, $7, $8, $9, $10, $11
)
RETURNING claim_id, status
"""


# Record a disposition + case notes for a claim. Dispositions are stored in the
# dedicated ``claim_dispositions`` table (migration 004) — an append-only case
# record keyed by claim_id, disposition, case_notes and the investigator's GCKey
# subject (investigator_id). The "current" disposition is the latest by
# created_at (see the detail read's LEFT JOIN LATERAL). All values are bound as
# parameters — no user value is interpolated into the SQL string.
_INSERT_DISPOSITION_SQL = """
INSERT INTO claim_dispositions (claim_id, disposition, case_notes, investigator_id)
VALUES ($1, $2, $3, $4)
RETURNING claim_id
"""


# ---------------------------------------------------------------------------
# Display surrogates for PII the DB does not store
# ---------------------------------------------------------------------------


def _display_name_from_hash(sin_hash: Optional[str]) -> str:
    """Deterministic non-sensitive display name derived from a sin_hash.

    The claims table stores only ``sin_hash`` (never the raw name), so for the
    investigator surfaces we synthesise a stable, opaque label. Real deployments
    join a separate identity store; this keeps the contract shape intact without
    inventing Protected B data.
    """
    if not sin_hash:
        return "Claimant"
    return f"Claimant {sin_hash[:8].upper()}"


def _pseudo_sin_from_hash(sin_hash: Optional[str]) -> Optional[str]:
    """Derive a display-only pseudo-SIN (last 3 digits) from a sin_hash."""
    if not sin_hash:
        return None
    digits = [c for c in sin_hash if c.isdigit()][-3:]
    if len(digits) < 3:
        digits = (["0"] * (3 - len(digits))) + digits
    return "***-**-" + "".join(digits)


# ---------------------------------------------------------------------------
# asyncpg-backed repository
# ---------------------------------------------------------------------------


class PortalRepository:
    """Read/write access to the portal data in PostgreSQL via an asyncpg pool."""

    def __init__(self, pool: "asyncpg.Pool") -> None:  # type: ignore[name-defined]
        if pool is None:
            raise ValueError("PortalRepository requires a non-null asyncpg pool.")
        self._pool = pool

    async def get_claim_status(self, claim_id: str) -> Optional[dict[str, Any]]:
        """Return the claimant status view for *claim_id*, or ``None`` if absent."""
        row = await self._pool.fetchrow(_CLAIM_STATUS_SQL, claim_id)
        if row is None:
            return None
        return {
            "claim_id": str(row["claim_id"]),
            "sin_hash": row["sin_hash"],
            "status": map_db_status_to_view(row["db_status"]),
            "risk_decision": row["risk_decision"],
            "risk_score": row["risk_score"],
            "explanation_en": row["explanation_en"],
            "explanation_fr": row["explanation_fr"],
            "submitted_at": _iso(row["received_at"]),
            "decided_at": _iso(row["scored_at"]),
        }

    async def list_flagged(self) -> list[dict[str, Any]]:
        """Return the flagged-claims queue, sorted by risk_score desc (Req 22.1)."""
        rows = await self._pool.fetch(_FLAGGED_QUEUE_SQL, list(FLAGGED_DECISIONS))
        result: list[dict[str, Any]] = []
        for row in rows:
            sin_hash = row["sin_hash"]
            result.append(
                {
                    "claim_id": str(row["claim_id"]),
                    "risk_score": row["risk_score"],
                    "risk_decision": row["risk_decision"],
                    "received_at": _iso(row["received_at"]),
                    "employer_bn": row["employer_bn"],
                    "claimant_name": _display_name_from_hash(sin_hash),
                    "claimant_sin": _pseudo_sin_from_hash(sin_hash),
                    "address": None,
                }
            )
        return result

    async def get_claim_detail(self, claim_id: str) -> Optional[dict[str, Any]]:
        """Return the full investigator detail for *claim_id*, or ``None``."""
        row = await self._pool.fetchrow(_CLAIM_DETAIL_SQL, claim_id)
        if row is None:
            return None

        indicators = await self._pool.fetch(_INDICATORS_SQL, claim_id)
        shap = await self._pool.fetch(_SHAP_SQL, row["assessment_id"])
        sin_hash = row["sin_hash"]

        return {
            "claim_id": str(row["claim_id"]),
            "risk_score": row["risk_score"],
            "risk_decision": row["risk_decision"],
            "shap_base_value": row["shap_base_value"],
            "shap_attributions": [
                {
                    "indicator_id": str(a["indicator_id"]),
                    "feature_name": a["feature_name"],
                    "contribution": a["contribution"],
                }
                for a in shap
            ],
            "indicators": [
                {
                    "indicator_id": str(i["indicator_id"]),
                    "engine": i["engine_type"],
                    "severity": i["severity"],
                    "title": i["title"],
                    "explanation_en": i["explanation_en"],
                    "explanation_fr": i["explanation_fr"],
                    "shap_contribution": i["shap_contribution"],
                }
                for i in indicators
            ],
            "explanation_en": row["explanation_en"],
            "explanation_fr": row["explanation_fr"],
            "claimant_name": _display_name_from_hash(sin_hash),
            "claimant_sin": _pseudo_sin_from_hash(sin_hash),
            "employer_bn": row["employer_bn"],
            "disposition": row["disposition"],
            "case_notes": row["case_notes"],
        }

    async def record_disposition(
        self,
        claim_id: str,
        disposition: str,
        case_notes: str,
        *,
        investigator_id: str,
    ) -> bool:
        """Record *disposition* + *case_notes* in ``claim_dispositions`` (Req 22.3).

        Appends a case record attributed to *investigator_id* (the recorder's
        GCKey subject). Returns ``True`` when the row was inserted. Raises on a
        real database error (e.g. an invalid ``disposition`` violating the CHECK
        constraint, or a missing claim FK) so callers surface genuine failures
        rather than silently dropping the audit-relevant case outcome.
        """
        row = await self._pool.fetchrow(
            _INSERT_DISPOSITION_SQL,
            claim_id,
            disposition,
            case_notes,
            investigator_id,
        )
        return row is not None

    async def create_claim(
        self,
        *,
        claim_id: str,
        sin_hash: str,
        employer_bn: str,
        insurable_hours: float,
        insurable_earnings: float,
        separation_date: str,
        separation_reason: str,
        postal_code: Optional[str] = None,
        roe_document_id: Optional[str] = None,
        language: str = "en",
    ) -> dict[str, Any]:
        """Insert a freshly submitted claim (status RECEIVED) and return it.

        Persists directly to the same ``claims`` table the investigator/read
        side queries (dev-mode direct-persist path). The raw SIN is never
        accepted here — only its ``sin_hash`` digest. All values are bound as
        query parameters (no string interpolation of user input). The new claim
        has no risk assessment yet, so it correctly does not appear in the
        flagged investigator queue.
        """
        row = await self._pool.fetchrow(
            _INSERT_CLAIM_SQL,
            claim_id,
            sin_hash,
            employer_bn,
            insurable_hours,
            insurable_earnings,
            separation_date,
            separation_reason,
            postal_code,
            roe_document_id,
            "RECEIVED",
            language,
        )
        return {"claim_id": str(row["claim_id"]), "status": row["status"]}


def _iso(value: Any) -> Optional[str]:
    """Serialise a datetime-like value to ISO-8601, passing ``None`` through."""
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# In-memory fake for unit tests / local dev
# ---------------------------------------------------------------------------


class FakePortalRepository:
    """In-memory :class:`PortalRepository` stand-in for tests and local dev.

    Seed data via :meth:`add_claim`. Every accessor returns the same snake_case
    contract shape as the real repository so tests exercise the true wire format.
    """

    def __init__(self) -> None:
        # claim_id -> claim record (unmasked, contract-shaped)
        self._claims: dict[str, dict[str, Any]] = {}

    # -- seeding -----------------------------------------------------------

    def add_claim(
        self,
        claim_id: str,
        *,
        sin_hash: str = "hash",
        db_status: str = "SCORED",
        risk_score: Optional[int] = None,
        risk_decision: Optional[str] = None,
        explanation_en: Optional[str] = None,
        explanation_fr: Optional[str] = None,
        received_at: str = "2024-01-01T00:00:00+00:00",
        scored_at: Optional[str] = "2024-01-02T00:00:00+00:00",
        employer_bn: str = "123456789RT0001",
        claimant_name: str = "Jane Doe",
        claimant_sin: str = "123-456-789",
        address: str = "123 Main St, Ottawa ON",
        shap_base_value: float = 50.0,
        shap_attributions: Optional[list[dict[str, Any]]] = None,
        indicators: Optional[list[dict[str, Any]]] = None,
        disposition: Optional[str] = None,
        case_notes: Optional[str] = None,
    ) -> None:
        """Seed a claim (with optional assessment / indicators) for tests."""
        self._claims[claim_id] = {
            "claim_id": claim_id,
            "sin_hash": sin_hash,
            "db_status": db_status,
            "risk_score": risk_score,
            "risk_decision": risk_decision,
            "explanation_en": explanation_en,
            "explanation_fr": explanation_fr,
            "received_at": received_at,
            "scored_at": scored_at,
            "employer_bn": employer_bn,
            "claimant_name": claimant_name,
            "claimant_sin": claimant_sin,
            "address": address,
            "shap_base_value": shap_base_value,
            "shap_attributions": shap_attributions or [],
            "indicators": indicators or [],
            "disposition": disposition,
            "case_notes": case_notes,
        }

    # -- reads -------------------------------------------------------------

    async def get_claim_status(self, claim_id: str) -> Optional[dict[str, Any]]:
        claim = self._claims.get(claim_id)
        if claim is None:
            return None
        return {
            "claim_id": claim["claim_id"],
            "sin_hash": claim["sin_hash"],
            "status": map_db_status_to_view(claim["db_status"]),
            "risk_decision": claim["risk_decision"],
            "risk_score": claim["risk_score"],
            "explanation_en": claim["explanation_en"],
            "explanation_fr": claim["explanation_fr"],
            "submitted_at": claim["received_at"],
            "decided_at": claim["scored_at"],
        }

    async def list_flagged(self) -> list[dict[str, Any]]:
        rows = [
            {
                "claim_id": c["claim_id"],
                "risk_score": c["risk_score"],
                "risk_decision": c["risk_decision"],
                "received_at": c["received_at"],
                "employer_bn": c["employer_bn"],
                "claimant_name": c["claimant_name"],
                "claimant_sin": c["claimant_sin"],
                "address": c["address"],
            }
            for c in self._claims.values()
            if c["risk_decision"] in FLAGGED_DECISIONS
        ]
        rows.sort(
            key=lambda r: (r["risk_score"] or 0, r["received_at"]), reverse=True
        )
        return rows

    async def get_claim_detail(self, claim_id: str) -> Optional[dict[str, Any]]:
        claim = self._claims.get(claim_id)
        if claim is None or claim["risk_decision"] is None:
            return None
        return {
            "claim_id": claim["claim_id"],
            "risk_score": claim["risk_score"],
            "risk_decision": claim["risk_decision"],
            "shap_base_value": claim["shap_base_value"],
            "shap_attributions": copy.deepcopy(claim["shap_attributions"]),
            "indicators": copy.deepcopy(claim["indicators"]),
            "explanation_en": claim["explanation_en"],
            "explanation_fr": claim["explanation_fr"],
            "claimant_name": claim["claimant_name"],
            "claimant_sin": claim["claimant_sin"],
            "employer_bn": claim["employer_bn"],
            "disposition": claim["disposition"],
            "case_notes": claim["case_notes"],
        }

    async def record_disposition(
        self,
        claim_id: str,
        disposition: str,
        case_notes: str,
        *,
        investigator_id: str = "",
    ) -> bool:
        claim = self._claims.get(claim_id)
        if claim is None:
            return False
        claim["disposition"] = disposition
        claim["case_notes"] = case_notes
        claim["investigator_id"] = investigator_id
        return True

    async def create_claim(
        self,
        *,
        claim_id: str,
        sin_hash: str,
        employer_bn: str,
        insurable_hours: float,
        insurable_earnings: float,
        separation_date: str,
        separation_reason: str,
        postal_code: Optional[str] = None,
        roe_document_id: Optional[str] = None,
        language: str = "en",
    ) -> dict[str, Any]:
        """In-memory stand-in for :meth:`PortalRepository.create_claim`.

        Stores the claim with ``db_status='RECEIVED'`` and no risk assessment
        (``risk_decision=None``), so it is retrievable via
        :meth:`get_claim_status` (own-data-only keyed on ``sin_hash``) yet is
        correctly absent from :meth:`list_flagged` / :meth:`get_claim_detail`.
        """
        self._claims[claim_id] = {
            "claim_id": claim_id,
            "sin_hash": sin_hash,
            "db_status": "RECEIVED",
            "risk_score": None,
            "risk_decision": None,
            "explanation_en": None,
            "explanation_fr": None,
            "received_at": "2024-01-01T00:00:00+00:00",
            "scored_at": None,
            "employer_bn": employer_bn,
            "claimant_name": None,
            "claimant_sin": None,
            "address": None,
            "shap_base_value": None,
            "shap_attributions": [],
            "indicators": [],
            "disposition": None,
            "case_notes": None,
            # Non-contract extras retained for local inspection / tests.
            "insurable_hours": insurable_hours,
            "insurable_earnings": insurable_earnings,
            "separation_date": separation_date,
            "separation_reason": separation_reason,
            "residential_postal_code": postal_code,
            "roe_document_id": roe_document_id,
            "language": language,
        }
        return {"claim_id": claim_id, "status": "RECEIVED"}
