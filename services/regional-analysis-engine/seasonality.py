"""
services/regional-analysis-engine/seasonality.py
================================================
Seasonality annotation and recurring-claim detection for the Regional Analysis
Engine (task 16.2). Pure, side-effect-free logic operating on values fetched
via injected, duck-typed query providers so unit tests can supply fakes with no
live database.

Two responsibilities live here (design §"Regional Analysis Engine",
Requirements 8.7 and 8.8):

  * :func:`is_seasonal_peak` / :func:`seasonal_annotation` — given the sector
    employment index for the claim's reference month and the 12-month rolling
    average for that sector, decide whether the claim falls in a seasonal peak.
    The rule (Requirement 8.7): the index STRICTLY exceeds the 12-month rolling
    average multiplied by 1.10 (i.e. more than 10% above the rolling average).
    Boundary: exactly ``avg * 1.10`` => NOT seasonal; ``avg * 1.10 + epsilon``
    => seasonal. When seasonal, EVERY Indicator produced by this engine is
    annotated with a ``SEASONAL`` flag (added to its ``payload``). This is an
    ANNOTATION, never a new Indicator.

  * :func:`matching_recurring_quarters` / :func:`check_recurring_claims` —
    inspect the claimant's prior claim history for the SAME calendar quarter in
    the three prior calendar years. When the claimant filed in that quarter in
    two or more of the three prior years, a MEDIUM Indicator is produced naming
    the matching years and the quarter (Requirement 8.8).

Exact :class:`~decimal.Decimal` arithmetic is used for the seasonal-index
comparison so the +10% boundary is exact and free of binary floating-point
error.

Requirements: 8.7, 8.8
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Iterable, Optional
from uuid import UUID

from shared.models import EngineType, Indicator, Severity


#: Seasonal-peak multiplier (Requirement 8.7): the sector employment index must
#: exceed the 12-month rolling average by more than 10%.
DEFAULT_SEASONAL_MULTIPLIER = Decimal("1.10")

#: Number of prior calendar years inspected for recurring claims (Req 8.8).
RECURRING_LOOKBACK_YEARS = 3

#: Minimum number of prior-year matches (of ``RECURRING_LOOKBACK_YEARS``) that
#: triggers the recurring-claim Indicator.
RECURRING_MATCH_THRESHOLD = 2

#: The ``payload`` key used to annotate Indicators when the claim falls in a
#: seasonal employment peak.
SEASONAL_FLAG_KEY = "seasonal"


# ---------------------------------------------------------------------------
# Seasonality annotation (Requirement 8.7)
# ---------------------------------------------------------------------------


def is_seasonal_peak(
    sector_index: Decimal,
    rolling_avg_12m: Decimal,
    *,
    multiplier: Decimal = DEFAULT_SEASONAL_MULTIPLIER,
) -> bool:
    """Return ``True`` when the sector employment index is in a seasonal peak.

    The peak condition (Requirement 8.7) is a STRICT inequality: the index must
    be greater than ``rolling_avg_12m * multiplier`` (default 1.10 => more than
    10% above the rolling average). Exactly at the threshold => not seasonal.

    A non-positive rolling average is treated as "no seasonal signal" (returns
    ``False``) because the ratio is undefined / meaningless.
    """
    if rolling_avg_12m <= 0:
        return False
    threshold = rolling_avg_12m * multiplier
    return sector_index > threshold


def seasonal_annotation(
    sector_index: Decimal,
    rolling_avg_12m: Decimal,
    *,
    multiplier: Decimal = DEFAULT_SEASONAL_MULTIPLIER,
) -> Optional[dict]:
    """Build the seasonal annotation ``dict`` when in a peak, else ``None``.

    The returned dict is merged into each Indicator's ``payload`` by the caller
    (see :func:`annotate_indicators`) so every Indicator carries the SEASONAL
    flag and the supporting evidence (index, rolling average, threshold).
    """
    if not is_seasonal_peak(sector_index, rolling_avg_12m, multiplier=multiplier):
        return None
    return {
        SEASONAL_FLAG_KEY: True,
        "seasonal_sector_index": str(sector_index),
        "seasonal_rolling_avg_12m": str(rolling_avg_12m),
        "seasonal_threshold": str(rolling_avg_12m * multiplier),
        "seasonal_multiplier": str(multiplier),
    }


def annotate_indicators(
    indicators: Iterable[Indicator], annotation: Optional[dict]
) -> list[Indicator]:
    """Merge *annotation* into every Indicator's ``payload`` (in place).

    When *annotation* is ``None`` the indicators are returned unchanged. The
    Indicator objects are mutated in place (their ``payload`` dicts are updated)
    and also returned as a list for convenience.
    """
    result = list(indicators)
    if annotation:
        for ind in result:
            ind.payload.update(annotation)
    return result


# ---------------------------------------------------------------------------
# Recurring-claim detection (Requirement 8.8)
# ---------------------------------------------------------------------------


def quarter_of(month: int) -> int:
    """Return the calendar quarter (1-4) for a 1-based month number."""
    if not 1 <= month <= 12:
        raise ValueError(f"month must be in 1..12, got {month!r}")
    return (month - 1) // 3 + 1


def matching_recurring_quarters(
    reference_date: date,
    prior_claim_dates: Iterable[date],
    *,
    lookback_years: int = RECURRING_LOOKBACK_YEARS,
) -> list[int]:
    """Return the prior calendar years whose SAME quarter had a claim.

    Args:
        reference_date: The claim's reference date (its calendar quarter and
            year anchor the comparison).
        prior_claim_dates: The claimant's prior claim start dates.
        lookback_years: How many prior calendar years to inspect (default 3).

    Returns:
        A sorted list of the prior years (e.g. ``[2021, 2023]``) in which the
        claimant filed a claim in the SAME calendar quarter as *reference_date*.
        Only the ``lookback_years`` years immediately preceding
        ``reference_date.year`` are considered; the reference year itself is
        excluded.
    """
    target_quarter = quarter_of(reference_date.month)
    ref_year = reference_date.year
    candidate_years = {ref_year - n for n in range(1, lookback_years + 1)}

    matches: set[int] = set()
    for d in prior_claim_dates:
        if d.year in candidate_years and quarter_of(d.month) == target_quarter:
            matches.add(d.year)
    return sorted(matches)


def check_recurring_claims(
    reference_date: date,
    prior_claim_dates: Iterable[date],
    *,
    claim_id: Optional[UUID] = None,
    lookback_years: int = RECURRING_LOOKBACK_YEARS,
    match_threshold: int = RECURRING_MATCH_THRESHOLD,
    produced_at: Optional[datetime] = None,
) -> list[Indicator]:
    """Produce a MEDIUM Indicator for a recurring seasonal claim cycle.

    An Indicator is produced iff the claimant filed a claim in the SAME
    calendar quarter as *reference_date* in AT LEAST ``match_threshold`` (default
    2) of the ``lookback_years`` (default 3) prior calendar years. The Indicator
    names the matching years and the quarter (Requirement 8.8).

    Returns:
        A list containing a single MEDIUM Indicator, or an empty list.
    """
    matching_years = matching_recurring_quarters(
        reference_date, prior_claim_dates, lookback_years=lookback_years
    )
    if len(matching_years) < match_threshold:
        return []

    ts = produced_at or datetime.now(timezone.utc)
    cid = claim_id or uuid.uuid4()
    quarter = quarter_of(reference_date.month)
    years_str = ", ".join(str(y) for y in matching_years)

    return [
        Indicator(
            indicator_id=uuid.uuid4(),
            claim_id=cid,
            engine=EngineType.REGIONAL_ANALYSIS,
            severity=Severity.MEDIUM,
            algorithm="recurring_seasonal_claim",
            title="Recurring seasonal claim cycle",
            explanation_en=(
                f"The claimant filed EI claims in Q{quarter} in "
                f"{len(matching_years)} of the {lookback_years} prior years "
                f"({years_str}), indicating a recurring seasonal claim cycle."
            ),
            explanation_fr=(
                f"Le prestataire a presente des demandes d'AE au T{quarter} "
                f"pendant {len(matching_years)} des {lookback_years} annees "
                f"precedentes ({years_str}), ce qui indique un cycle de "
                f"demandes saisonnieres recurrent."
            ),
            payload={
                "quarter": quarter,
                "matching_years": matching_years,
                "match_count": len(matching_years),
                "lookback_years": lookback_years,
                "match_threshold": match_threshold,
            },
            produced_at=ts,
        )
    ]


__all__ = [
    "DEFAULT_SEASONAL_MULTIPLIER",
    "RECURRING_LOOKBACK_YEARS",
    "RECURRING_MATCH_THRESHOLD",
    "SEASONAL_FLAG_KEY",
    "is_seasonal_peak",
    "seasonal_annotation",
    "annotate_indicators",
    "quarter_of",
    "matching_recurring_quarters",
    "check_recurring_claims",
]
