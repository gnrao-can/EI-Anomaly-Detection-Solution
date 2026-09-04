"""
services/history-inconsistency-engine/discrepancy.py
====================================================
Earnings discrepancy detection for the Employment History Inconsistency Engine
(task 14.1).

The engine cross-references the earnings a claimant declared on their Record of
Employment (ROE) against the employment income reported to the Canada Revenue
Agency on their T4. A material discrepancy is a strong fraud signal.

Two pieces live here:

  * :func:`compute_discrepancy` — the exact ``Decimal`` absolute-difference
    calculation. Using ``Decimal`` arithmetic (never ``float``) guarantees that
    two numerically identical amounts produce a discrepancy of *exactly*
    ``Decimal("0")`` with no floating-point rounding error (Requirement 6.10 /
    Property 4).

  * :func:`check_earnings_discrepancy` — turns the computed discrepancy into an
    Indicator list. A CRITICAL Indicator is produced only when the absolute
    difference *exceeds* the $2,000 threshold. The boundary is strict:
      - exactly $2,000.00  -> NO flag
      - $2,000.01          -> CRITICAL flag
    When the two amounts are equal the discrepancy is ``Decimal("0")`` and no
    Indicator is produced, which is exactly the P4 invariant that the property
    test (task 14.3) exercises.

Requirements: 6.1, 6.2, 6.3, 6.4, 6.10
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from shared.models import EngineType, Indicator, Severity

#: The discrepancy threshold in CAD. A discrepancy is flagged only when the
#: absolute difference is *strictly greater* than this value (Requirement 6.4).
DEFAULT_DISCREPANCY_THRESHOLD: Decimal = Decimal("2000")


def compute_discrepancy(roe_earnings: Decimal, t4_earnings: Decimal) -> Decimal:
    """Return the exact absolute difference between ROE and T4 earnings.

    The arithmetic is performed with :class:`decimal.Decimal` so the result is
    exact: when ``roe_earnings == t4_earnings`` the returned value is exactly
    ``Decimal("0")`` (Requirement 6.10 / Property 4), with no floating-point
    drift regardless of the number of decimal places.

    Inputs are coerced to ``Decimal`` via their string representation so that
    callers passing ``int`` / ``str`` still get exact arithmetic; passing a
    ``float`` is coerced through ``str`` as well to avoid binary-float noise.
    """
    roe = _as_decimal(roe_earnings)
    t4 = _as_decimal(t4_earnings)
    return abs(roe - t4)


def check_earnings_discrepancy(
    roe_earnings: Decimal,
    t4_earnings: Decimal,
    *,
    claim_id: Optional[str] = None,
    threshold: Decimal = DEFAULT_DISCREPANCY_THRESHOLD,
) -> list[Indicator]:
    """Return Indicators for an ROE-vs-T4 earnings discrepancy.

    A single CRITICAL Indicator is produced when
    ``|roe_earnings - t4_earnings| > threshold`` (default $2,000). The comparison
    is strict, so a difference of exactly ``threshold`` produces NO Indicator and
    a difference of ``threshold + 0.01`` does. When the two amounts are equal the
    discrepancy is ``Decimal("0")`` and the returned list is empty (P4).

    The CRITICAL Indicator's payload carries the exact CAD amounts (ROE, T4, the
    discrepancy, and the threshold) as strings so the audit trail preserves them
    without float rounding.
    """
    roe = _as_decimal(roe_earnings)
    t4 = _as_decimal(t4_earnings)
    discrepancy = abs(roe - t4)

    threshold = _as_decimal(threshold)
    if discrepancy <= threshold:
        return []

    indicator = Indicator(
        indicator_id=uuid.uuid4(),
        claim_id=_coerce_uuid(claim_id),
        engine=EngineType.HISTORY_INCONSISTENCY,
        severity=Severity.CRITICAL,
        algorithm="earnings_discrepancy",
        title="ROE and T4 earnings discrepancy exceeds threshold",
        explanation_en=(
            f"Declared ROE earnings (CAD {roe}) differ from CRA T4 employment "
            f"income (CAD {t4}) by CAD {discrepancy}, exceeding the "
            f"CAD {threshold} threshold."
        ),
        explanation_fr=(
            f"Les gains declares sur le releve d'emploi (CAD {roe}) different du "
            f"revenu d'emploi du T4 de l'ARC (CAD {t4}) de CAD {discrepancy}, "
            f"depassant le seuil de CAD {threshold}."
        ),
        payload={
            "roe_earnings_cad": str(roe),
            "t4_earnings_cad": str(t4),
            "discrepancy_cad": str(discrepancy),
            "threshold_cad": str(threshold),
        },
        produced_at=datetime.now(timezone.utc),
    )
    return [indicator]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_decimal(value: Decimal | int | float | str) -> Decimal:
    """Coerce a numeric value to ``Decimal`` via its string form (exact)."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _coerce_uuid(claim_id: Optional[str]) -> uuid.UUID:
    """Coerce a claim id string to a UUID, tolerating missing / non-UUID ids."""
    if isinstance(claim_id, uuid.UUID):
        return claim_id
    if claim_id:
        try:
            return uuid.UUID(str(claim_id))
        except (ValueError, AttributeError, TypeError):
            return uuid.uuid5(uuid.NAMESPACE_OID, str(claim_id))
    return uuid.uuid4()


__all__ = [
    "DEFAULT_DISCREPANCY_THRESHOLD",
    "compute_discrepancy",
    "check_earnings_discrepancy",
]
