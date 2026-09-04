"""
services/regional-analysis-engine/providers.py
==============================================
Query providers for the seasonal employment pattern database and the claimant
claim-history store used by the Regional Analysis Engine (task 16.2).

These providers are intentionally thin and duck-typed so ``main.py`` can inject
real implementations in production while unit tests supply in-memory fakes with
no live database. Each provider exposes a single ``async`` query method:

  * :class:`SeasonalPatternProvider.get_sector_index` — returns the sector
    employment index for the claim's reference month together with the 12-month
    rolling average for that sector (Requirement 8.7). Returns ``None`` when no
    seasonal data exists for the sector/month (the engine then simply skips the
    seasonal annotation).

  * :class:`ClaimHistoryProvider.get_prior_claim_dates` — returns the claimant's
    prior claim start dates (as :class:`datetime.date`), used to detect
    recurring seasonal claim cycles (Requirement 8.8).

The real, database-backed implementations are out of scope for task 16.2's unit
surface; the in-memory fakes below stand in for them in tests and local dev and
document the exact contract the production providers must honour.

Requirements: 8.7, 8.8
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class SectorIndex:
    """Sector employment index for a reference month plus its rolling average."""

    sector_index: Decimal
    rolling_avg_12m: Decimal


@runtime_checkable
class SeasonalPatternProvider(Protocol):
    """Async query interface onto the EI-ADS seasonal employment pattern DB."""

    async def get_sector_index(
        self, sector_code: str, reference_month: str
    ) -> Optional[SectorIndex]:
        """Return the sector index + 12-month rolling average, or ``None``.

        Args:
            sector_code: The claimant's industry sector code.
            reference_month: Reference month as ``YYYY-MM``.
        """
        ...


@runtime_checkable
class ClaimHistoryProvider(Protocol):
    """Async query interface onto the claimant claim-history store."""

    async def get_prior_claim_dates(self, claim_key: str) -> list[date]:
        """Return the claimant's prior claim start dates (may be empty).

        Args:
            claim_key: A stable claimant key (e.g. ``sin_hash``) used to look up
                the claimant's history without exposing the raw SIN.
        """
        ...


# ---------------------------------------------------------------------------
# In-memory fakes (tests / local dev)
# ---------------------------------------------------------------------------


class InMemorySeasonalPatternProvider:
    """In-memory :class:`SeasonalPatternProvider` keyed by (sector, month)."""

    def __init__(
        self, data: Optional[dict[tuple[str, str], SectorIndex]] = None
    ) -> None:
        self._data: dict[tuple[str, str], SectorIndex] = dict(data or {})

    def set(
        self,
        sector_code: str,
        reference_month: str,
        sector_index: Decimal,
        rolling_avg_12m: Decimal,
    ) -> None:
        self._data[(sector_code, reference_month)] = SectorIndex(
            sector_index=sector_index, rolling_avg_12m=rolling_avg_12m
        )

    async def get_sector_index(
        self, sector_code: str, reference_month: str
    ) -> Optional[SectorIndex]:
        return self._data.get((sector_code, reference_month))


class InMemoryClaimHistoryProvider:
    """In-memory :class:`ClaimHistoryProvider` keyed by claimant key."""

    def __init__(self, data: Optional[dict[str, list[date]]] = None) -> None:
        self._data: dict[str, list[date]] = {k: list(v) for k, v in (data or {}).items()}

    def set(self, claim_key: str, dates: list[date]) -> None:
        self._data[claim_key] = list(dates)

    async def get_prior_claim_dates(self, claim_key: str) -> list[date]:
        return list(self._data.get(claim_key, []))


__all__ = [
    "SectorIndex",
    "SeasonalPatternProvider",
    "ClaimHistoryProvider",
    "InMemorySeasonalPatternProvider",
    "InMemoryClaimHistoryProvider",
]
