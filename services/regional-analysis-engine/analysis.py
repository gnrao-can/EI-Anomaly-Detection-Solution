"""
services/regional-analysis-engine/analysis.py
=============================================
Pure, side-effect-free analysis functions for the Regional Analysis Engine
(task 16.1). These functions perform the rate-deviation and job-posting checks
using EXACT ``Decimal`` arithmetic and return canonical
:class:`shared.models.Indicator` instances (never persist or publish).

Design contract (design §"Regional Analysis Engine"):

  * ``rate_deviation(regional, national)`` == ``regional - national`` (Decimal).
  * ``check_rate_deviation`` emits a MEDIUM Indicator when the deviation is
    strictly below the -1.5 pp threshold. Boundary: exactly -1.5 pp => NO flag;
    -1.5001 pp => flag. Equal rates => deviation 0, no Indicator (P13, task
    16.3).
  * ``check_job_postings`` emits a HIGH Indicator when the active posting count
    is strictly greater than 200 (200 => no, 201 => yes) and a separate HIGH
    Indicator when ``median_wage`` is strictly greater than
    ``hourly_earnings * 1.15`` (exactly +15% => no, +15.01% => yes).

Every Indicator carries the regional/national rates and the deviation (or the
posting count / wage figures) in its ``payload`` as evidence, plus bilingual
explanations. ``DATA_UNAVAILABLE`` annotation on adapter failure is applied by
``main.py`` (this module is only reached with concrete numeric inputs).

Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from shared.models import EngineType, Indicator, Severity


#: Default detection thresholds (mirrors config.py defaults so the analysis
#: functions can be used standalone in tests without constructing Settings).
DEFAULT_RATE_DEVIATION_THRESHOLD_PP = Decimal("-1.5")
DEFAULT_MAX_ACTIVE_POSTINGS = 200
DEFAULT_WAGE_RATIO_THRESHOLD = Decimal("1.15")


# ---------------------------------------------------------------------------
# Rate deviation
# ---------------------------------------------------------------------------


def rate_deviation(regional: Decimal, national: Decimal) -> Decimal:
    """Return the exact deviation ``regional - national`` in percentage points.

    Uses :class:`~decimal.Decimal` throughout so that equal rates yield exactly
    ``Decimal("0")`` (P13) and no binary floating-point error is introduced.
    """
    return regional - national


def check_rate_deviation(
    regional_rate: Decimal,
    national_rate: Decimal,
    *,
    claim_id: Optional[UUID] = None,
    threshold_pp: Decimal = DEFAULT_RATE_DEVIATION_THRESHOLD_PP,
    produced_at: Optional[datetime] = None,
) -> list[Indicator]:
    """Check the regional-vs-national unemployment rate deviation.

    A MEDIUM Indicator is produced iff the deviation is STRICTLY below
    ``threshold_pp`` (default -1.5 pp). At exactly -1.5 pp no Indicator is
    produced; at -1.5001 pp one is. Equal rates give a deviation of exactly 0
    and therefore never flag (P13 invariant, task 16.3).

    Args:
        regional_rate: The region's unemployment rate (percentage points).
        national_rate: The national unemployment rate for the same month.
        claim_id: Optional claim UUID stamped onto the Indicator.
        threshold_pp: The (negative) deviation threshold in percentage points.
        produced_at: Optional timestamp; defaults to now (UTC).

    Returns:
        A list containing a single MEDIUM Indicator when the deviation is below
        the threshold, otherwise an empty list.
    """
    deviation = rate_deviation(regional_rate, national_rate)
    if not (deviation < threshold_pp):
        return []

    ts = produced_at or datetime.now(timezone.utc)
    cid = claim_id or uuid.uuid4()
    payload = {
        "regional_rate": str(regional_rate),
        "national_rate": str(national_rate),
        "deviation": str(deviation),
        "threshold_pp": str(threshold_pp),
    }
    explanation_en = (
        f"Regional unemployment rate ({regional_rate}%) is {abs(deviation)} "
        f"percentage points below the national rate ({national_rate}%), "
        f"exceeding the {abs(threshold_pp)} pp deviation threshold."
    )
    explanation_fr = (
        f"Le taux de chomage regional ({regional_rate} %) est inferieur de "
        f"{abs(deviation)} points de pourcentage au taux national "
        f"({national_rate} %), depassant le seuil de {abs(threshold_pp)} pp."
    )
    return [
        Indicator(
            indicator_id=uuid.uuid4(),
            claim_id=cid,
            engine=EngineType.REGIONAL_ANALYSIS,
            severity=Severity.MEDIUM,
            algorithm="regional_rate_deviation",
            title="Regional unemployment rate deviation",
            explanation_en=explanation_en,
            explanation_fr=explanation_fr,
            payload=payload,
            produced_at=ts,
        )
    ]


# ---------------------------------------------------------------------------
# Job postings
# ---------------------------------------------------------------------------


def check_job_postings(
    active_count: int,
    median_wage: Decimal,
    hourly_earnings: Decimal,
    *,
    claim_id: Optional[UUID] = None,
    max_active_postings: int = DEFAULT_MAX_ACTIVE_POSTINGS,
    wage_ratio_threshold: Decimal = DEFAULT_WAGE_RATIO_THRESHOLD,
    produced_at: Optional[datetime] = None,
) -> list[Indicator]:
    """Check active Job Bank postings and advertised wages for the claim NOC.

    Emits up to two independent HIGH Indicators:

      1. When ``active_count`` is STRICTLY greater than ``max_active_postings``
         (default 200). Exactly 200 => no flag; 201 => flag.
      2. When ``median_wage`` is STRICTLY greater than
         ``hourly_earnings * wage_ratio_threshold`` (default 1.15). Exactly +15%
         => no flag; +15.01% => flag. Exact ``Decimal`` multiplication is used
         so the boundary is exact.

    Args:
        active_count: Number of active postings matching NOC + region + period.
        median_wage: Median advertised hourly wage from Job Bank (Decimal).
        hourly_earnings: The claimant's insurable hourly earnings (Decimal).
        claim_id: Optional claim UUID stamped onto the Indicators.
        max_active_postings: Posting-count threshold (strictly-greater-than).
        wage_ratio_threshold: Wage ratio threshold (strictly-greater-than).
        produced_at: Optional timestamp; defaults to now (UTC).

    Returns:
        A list of zero, one, or two HIGH Indicators.
    """
    ts = produced_at or datetime.now(timezone.utc)
    cid = claim_id or uuid.uuid4()
    indicators: list[Indicator] = []

    if active_count > max_active_postings:
        indicators.append(
            Indicator(
                indicator_id=uuid.uuid4(),
                claim_id=cid,
                engine=EngineType.REGIONAL_ANALYSIS,
                severity=Severity.HIGH,
                algorithm="job_bank_active_postings",
                title="High volume of active job postings",
                explanation_en=(
                    f"There are {active_count} active job postings for the "
                    f"claimant's occupation in the region, above the threshold "
                    f"of {max_active_postings}."
                ),
                explanation_fr=(
                    f"Il y a {active_count} offres d'emploi actives pour la "
                    f"profession du prestataire dans la region, au-dessus du "
                    f"seuil de {max_active_postings}."
                ),
                payload={
                    "active_posting_count": active_count,
                    "threshold": max_active_postings,
                },
                produced_at=ts,
            )
        )

    wage_ceiling = hourly_earnings * wage_ratio_threshold
    if median_wage > wage_ceiling:
        indicators.append(
            Indicator(
                indicator_id=uuid.uuid4(),
                claim_id=cid,
                engine=EngineType.REGIONAL_ANALYSIS,
                severity=Severity.HIGH,
                algorithm="job_bank_median_wage",
                title="Advertised wage exceeds claimant earnings",
                explanation_en=(
                    f"Median advertised wage ({median_wage}) exceeds the "
                    f"claimant's hourly earnings ({hourly_earnings}) by more "
                    f"than {wage_ratio_threshold}x."
                ),
                explanation_fr=(
                    f"Le salaire median affiche ({median_wage}) depasse les "
                    f"gains horaires du prestataire ({hourly_earnings}) de plus "
                    f"de {wage_ratio_threshold}x."
                ),
                payload={
                    "median_wage": str(median_wage),
                    "hourly_earnings": str(hourly_earnings),
                    "wage_ratio_threshold": str(wage_ratio_threshold),
                    "wage_ceiling": str(wage_ceiling),
                },
                produced_at=ts,
            )
        )

    return indicators


__all__ = [
    "DEFAULT_RATE_DEVIATION_THRESHOLD_PP",
    "DEFAULT_MAX_ACTIVE_POSTINGS",
    "DEFAULT_WAGE_RATIO_THRESHOLD",
    "rate_deviation",
    "check_rate_deviation",
    "check_job_postings",
]
