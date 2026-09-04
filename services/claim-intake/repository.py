"""
services/claim-intake/repository.py
===================================
Persistence layer for the Claim Intake Service.

Provides:
  - ``ClaimRepository`` — inserts ``Claim`` records into the PostgreSQL
    ``claims`` table (see ``scripts/migrations/001_create_claims.sql``) using an
    ``asyncpg`` connection pool and parameterised queries. Claims are always
    inserted with status ``RECEIVED``.
  - ``FakeClaimRepository`` — an in-memory implementation with the same async
    interface, used for unit tests and local development where PostgreSQL is
    unavailable.

Both implementations expose:
  - ``async insert_claim(claim: Claim) -> UUID`` — persist a claim and return
    its ``claim_id``.
  - ``async delete_claim(claim_id: UUID) -> bool`` — remove a claim by id,
    used to roll back the PG write when the subsequent Kafka publish of
    ``claim.received`` fails (two-phase commit, task 4.3, Requirement 1.5).

The raw SIN is never accepted, stored, or logged here — only the already-hashed
``sin_hash`` carried on the ``Claim`` model is persisted (Requirement 1.11).

Requirements: 1.4, 1.5, 1.11, 19.1
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable
from uuid import UUID

from shared.models import Claim, ClaimStatus

# asyncpg is a runtime-only dependency. Guard the import so this module (and the
# unit tests that rely on the in-memory fake) load cleanly in environments where
# the compiled driver is unavailable.
try:  # pragma: no cover - exercised indirectly
    import asyncpg  # type: ignore

    _HAS_ASYNCPG = True
except ImportError:  # pragma: no cover
    asyncpg = None  # type: ignore
    _HAS_ASYNCPG = False


__all__ = [
    "AbstractClaimRepository",
    "ClaimRepository",
    "FakeClaimRepository",
]


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

# Insert only the columns the intake service is responsible for. All other
# columns (claim_id default, received_at/updated_at defaults, protected_b,
# language, status default) are handled by the table defaults, but we set
# status and claim_id explicitly so the returned id is deterministic.
_INSERT_CLAIM_SQL = """
INSERT INTO claims (
    claim_id,
    sin_hash,
    employer_bn,
    insurable_hours,
    insurable_earnings,
    separation_date,
    separation_reason,
    separation_reason_code,
    claim_start_date,
    economic_region_code,
    noc_code,
    residential_postal_code,
    residential_address_hash,
    employer_work_location,
    status,
    received_at
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16
)
RETURNING claim_id
"""

# Rollback query for the two-phase commit (task 4.3): if the Kafka publish of
# ``claim.received`` fails after the row has been inserted, the intake endpoint
# deletes the just-written row so no orphaned claim remains without a
# corresponding downstream event. Fully parameterised (no interpolation).
_DELETE_CLAIM_SQL = """
DELETE FROM claims
WHERE claim_id = $1
RETURNING claim_id
"""


@runtime_checkable
class AbstractClaimRepository(Protocol):
    """Structural interface shared by the real and fake repositories."""

    async def insert_claim(self, claim: Claim) -> UUID:  # pragma: no cover - protocol
        ...

    async def delete_claim(self, claim_id: UUID) -> bool:  # pragma: no cover - protocol
        ...


# ---------------------------------------------------------------------------
# asyncpg-backed repository
# ---------------------------------------------------------------------------


class ClaimRepository:
    """Persist ``Claim`` records to PostgreSQL via an ``asyncpg`` pool.

    Parameters
    ----------
    pool:
        An ``asyncpg`` connection pool (``asyncpg.Pool``). Injected by the
        application lifespan handler so the repository does not own connection
        lifecycle management.
    """

    def __init__(self, pool: "asyncpg.Pool") -> None:  # type: ignore[name-defined]
        if pool is None:
            raise ValueError("ClaimRepository requires a non-null asyncpg pool.")
        self._pool = pool

    async def insert_claim(self, claim: Claim) -> UUID:
        """Insert *claim* with status ``RECEIVED`` and return its ``claim_id``.

        Uses a fully parameterised query (no string interpolation of values) to
        prevent SQL injection. The claim is always persisted with status
        ``RECEIVED`` regardless of the status carried on the model.
        """
        row = await self._pool.fetchrow(
            _INSERT_CLAIM_SQL,
            claim.claim_id,
            claim.sin_hash,
            claim.employer_bn,
            claim.insurable_hours,
            claim.insurable_earnings,
            claim.separation_date,
            claim.separation_reason,
            claim.separation_reason_code or None,
            claim.claim_start_date,
            claim.economic_region_code or None,
            claim.noc_code or None,
            claim.residential_postal_code or None,
            claim.residential_address_hash or None,
            claim.employer_work_location or None,
            ClaimStatus.RECEIVED.value,
            claim.received_at,
        )
        return row["claim_id"]

    async def delete_claim(self, claim_id: UUID) -> bool:
        """Delete the claim row identified by *claim_id* (transactional rollback).

        Used by the two-phase commit in the intake endpoint: when the Kafka
        publish of ``claim.received`` fails after the row has been persisted,
        this removes the orphaned row so PG and the event stream stay consistent
        (Requirement 1.5). Returns ``True`` if a row was deleted, ``False`` if no
        matching row existed. Uses a fully parameterised query.
        """
        row = await self._pool.fetchrow(_DELETE_CLAIM_SQL, claim_id)
        return row is not None


# ---------------------------------------------------------------------------
# In-memory fake for unit tests / local dev
# ---------------------------------------------------------------------------


class FakeClaimRepository:
    """In-memory ``ClaimRepository`` stand-in for tests and local development.

    Stores inserted claims in a list and always records status ``RECEIVED``.
    Exposes the same ``async insert_claim`` signature as :class:`ClaimRepository`.
    """

    def __init__(self) -> None:
        self.claims: list[Claim] = []
        # Structured records mirroring what would be persisted, so tests can
        # assert on the exact stored representation (e.g. that no raw SIN leaks).
        self.records: list[dict[str, Any]] = []
        self._fail_next: bool = False

    def fail_next_insert(self) -> None:
        """Arm the repository to raise on the next ``insert_claim`` call.

        Useful for simulating a persistence failure in tests.
        """
        self._fail_next = True

    async def insert_claim(self, claim: Claim) -> UUID:
        """Record *claim* in memory (forcing status ``RECEIVED``) and return its id."""
        if self._fail_next:
            self._fail_next = False
            raise RuntimeError("Simulated persistence failure")

        # Force RECEIVED status to mirror the real repository's behaviour.
        stored = Claim(
            claim_id=claim.claim_id,
            sin_hash=claim.sin_hash,
            employer_bn=claim.employer_bn,
            insurable_hours=claim.insurable_hours,
            insurable_earnings=claim.insurable_earnings,
            separation_date=claim.separation_date,
            separation_reason=claim.separation_reason,
            separation_reason_code=claim.separation_reason_code,
            claim_start_date=claim.claim_start_date,
            economic_region_code=claim.economic_region_code,
            noc_code=claim.noc_code,
            residential_postal_code=claim.residential_postal_code,
            residential_address_hash=claim.residential_address_hash,
            employer_work_location=claim.employer_work_location,
            received_at=claim.received_at,
            status=ClaimStatus.RECEIVED,
            roe_document_id=claim.roe_document_id,
            protected_b=claim.protected_b,
        )
        self.claims.append(stored)
        self.records.append(stored.to_dict())
        return stored.claim_id

    async def delete_claim(self, claim_id: UUID) -> bool:
        """Remove the in-memory claim with *claim_id* (transactional rollback).

        Mirrors :meth:`ClaimRepository.delete_claim` so the rollback branch of
        the two-phase commit can be unit-tested without a database. Removes both
        the stored ``Claim`` and its serialised record. Returns ``True`` if a
        matching claim was removed, ``False`` otherwise.
        """
        removed = False
        remaining_claims: list[Claim] = []
        for claim in self.claims:
            if claim.claim_id == claim_id:
                removed = True
                continue
            remaining_claims.append(claim)
        self.claims = remaining_claims

        self.records = [
            record
            for record in self.records
            if record.get("claim_id") != str(claim_id)
        ]
        return removed

    def get(self, claim_id: UUID) -> Optional[Claim]:
        """Return the stored claim with *claim_id*, or ``None`` if absent."""
        for claim in self.claims:
            if claim.claim_id == claim_id:
                return claim
        return None
