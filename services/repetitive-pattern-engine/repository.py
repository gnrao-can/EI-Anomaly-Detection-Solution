"""
services/repetitive-pattern-engine/repository.py
=================================================
Claim-history persistence layer for the Repetitive Pattern Engine.

Provides:
  - ``ClaimHistoryRepository`` — retrieves a claimant's prior claims from the
    PostgreSQL ``claims`` table (see ``scripts/migrations/001_create_claims.sql``)
    by ``sin_hash`` over a 60-month rolling window, using an ``asyncpg``
    connection pool and fully parameterised queries (no string interpolation of
    values, so no SQL-injection surface).
  - ``FakeClaimHistoryRepository`` — an in-memory implementation with the same
    async interface, used for unit tests and local development where PostgreSQL
    is unavailable. It can also be armed to raise, so callers can exercise the
    DATA_UNAVAILABLE path.

Both implementations expose:
  - ``async fetch_history(sin_hash, as_of=None) -> list[ClaimHistoryRecord]``
    returning the claimant's claims within the configured rolling window,
    ordered by ``separation_date`` ascending.

On database unavailability the *caller* (see ``main.py`` /
``ClaimHistoryUnavailable``) emits a single DATA_UNAVAILABLE Indicator and
returns without further analysis (Requirement 3.1 fallback).

The raw SIN is never accepted here — only the already-hashed ``sin_hash`` is
used to key the lookup (Requirement 1.11).

Requirements: 3.1, 1.11, 19.1
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional, Protocol, runtime_checkable

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
    "ClaimHistoryRecord",
    "ClaimHistoryUnavailable",
    "AbstractClaimHistoryRepository",
    "ClaimHistoryRepository",
    "FakeClaimHistoryRepository",
    "window_start",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ClaimHistoryUnavailable(Exception):
    """Raised when the claim-history store cannot be reached.

    The engine translates this into a single DATA_UNAVAILABLE Indicator and
    returns without further analysis (Requirement 3.1 fallback).
    """


# ---------------------------------------------------------------------------
# Lightweight record used by the pattern functions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimHistoryRecord:
    """A single historical claim, reduced to the fields the engine needs.

    Attributes
    ----------
    claim_id:
        The claim's UUID rendered as a string (opaque identifier).
    employer_bn:
        15-character employer Business Number the claim was filed against.
    separation_date:
        Date on which the employment ended — the sequence key used for both
        employer-repetition windows and sequential interval mining.
    insurable_hours:
        Total insurable hours (float; used by hours-clustering in task 11.2 —
        retained here so the record is complete).
    """

    claim_id: str
    employer_bn: str
    separation_date: date
    insurable_hours: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "employer_bn": self.employer_bn,
            "separation_date": self.separation_date.isoformat(),
            "insurable_hours": self.insurable_hours,
        }


# ---------------------------------------------------------------------------
# Rolling-window helper
# ---------------------------------------------------------------------------


def window_start(as_of: date, months: int) -> date:
    """Return the first date included in a rolling window of *months* months.

    Computed by subtracting whole months from *as_of* using calendar arithmetic
    (no external ``dateutil`` dependency). Clamps the day-of-month when the
    target month is shorter (e.g. 60 months before 2024-03-31).
    """
    total_months = (as_of.year * 12 + (as_of.month - 1)) - months
    year, month0 = divmod(total_months, 12)
    month = month0 + 1
    # Clamp day to the last valid day of the target month.
    day = min(as_of.day, _days_in_month(year, month))
    return date(year, month, day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    return (nxt - date(year, month, 1)).days


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------


@runtime_checkable
class AbstractClaimHistoryRepository(Protocol):
    """Structural interface shared by the real and fake repositories."""

    async def fetch_history(
        self, sin_hash: str, as_of: Optional[date] = None
    ) -> list[ClaimHistoryRecord]:  # pragma: no cover - protocol
        ...


# ---------------------------------------------------------------------------
# asyncpg-backed repository
# ---------------------------------------------------------------------------

# Parameterised query: select all claims for the given sin_hash whose
# separation_date falls within the rolling window [$2, $3]. Ordered ascending so
# the sequence functions receive a chronologically sorted list.
_SELECT_HISTORY_SQL = """
SELECT claim_id,
       employer_bn,
       separation_date,
       insurable_hours
FROM claims
WHERE sin_hash = $1
  AND separation_date >= $2
  AND separation_date <= $3
ORDER BY separation_date ASC
"""


class ClaimHistoryRepository:
    """Retrieve claim history from PostgreSQL via an ``asyncpg`` pool.

    Parameters
    ----------
    pool:
        An ``asyncpg`` connection pool (``asyncpg.Pool``). Injected by the
        application lifespan handler so the repository does not own connection
        lifecycle management.
    window_months:
        Size of the rolling retrieval window in months (default 60, Req 3.1).
    """

    def __init__(self, pool: "asyncpg.Pool", window_months: int = 60) -> None:  # type: ignore[name-defined]
        if pool is None:
            raise ValueError("ClaimHistoryRepository requires a non-null asyncpg pool.")
        self._pool = pool
        self._window_months = window_months

    async def fetch_history(
        self, sin_hash: str, as_of: Optional[date] = None
    ) -> list[ClaimHistoryRecord]:
        """Return the claimant's claims within the rolling window.

        Raises
        ------
        ClaimHistoryUnavailable
            If the database cannot be queried (connection/pool error). The
            caller converts this into a DATA_UNAVAILABLE Indicator.
        """
        as_of = as_of or datetime.now(timezone.utc).date()
        start = window_start(as_of, self._window_months)
        try:
            rows = await self._pool.fetch(_SELECT_HISTORY_SQL, sin_hash, start, as_of)
        except Exception as exc:  # noqa: BLE001 - any DB error => unavailable
            raise ClaimHistoryUnavailable(
                "claim history store unavailable"
            ) from exc

        return [
            ClaimHistoryRecord(
                claim_id=str(row["claim_id"]),
                employer_bn=row["employer_bn"],
                separation_date=row["separation_date"],
                insurable_hours=float(row["insurable_hours"] or 0.0),
            )
            for row in rows
        ]


# ---------------------------------------------------------------------------
# In-memory fake for unit tests / local dev
# ---------------------------------------------------------------------------


class FakeClaimHistoryRepository:
    """In-memory ``ClaimHistoryRepository`` stand-in for tests and local dev.

    Stores records keyed by ``sin_hash`` and applies the same rolling-window
    filter and ascending ``separation_date`` ordering as the real repository.
    Can be armed via :meth:`fail_next` to raise ``ClaimHistoryUnavailable`` so
    the DATA_UNAVAILABLE path can be unit-tested without a database.
    """

    def __init__(self, window_months: int = 60) -> None:
        self._by_sin: dict[str, list[ClaimHistoryRecord]] = {}
        self._window_months = window_months
        self._fail: bool = False

    def fail_next(self) -> None:
        """Arm the repository to raise ``ClaimHistoryUnavailable`` on next call."""
        self._fail = True

    def add(self, sin_hash: str, record: ClaimHistoryRecord) -> None:
        """Add a single history record for *sin_hash*."""
        self._by_sin.setdefault(sin_hash, []).append(record)

    def add_many(self, sin_hash: str, records: list[ClaimHistoryRecord]) -> None:
        """Add several history records for *sin_hash*."""
        self._by_sin.setdefault(sin_hash, []).extend(records)

    async def fetch_history(
        self, sin_hash: str, as_of: Optional[date] = None
    ) -> list[ClaimHistoryRecord]:
        if self._fail:
            self._fail = False
            raise ClaimHistoryUnavailable("simulated claim history store failure")

        as_of = as_of or datetime.now(timezone.utc).date()
        start = window_start(as_of, self._window_months)
        records = [
            r
            for r in self._by_sin.get(sin_hash, [])
            if start <= r.separation_date <= as_of
        ]
        return sorted(records, key=lambda r: r.separation_date)
