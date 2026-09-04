"""
services/document-forgery-engine/consistency.py
===============================================
Internal consistency checks for the Document Forgery Engine (task 15.1).

These are the pure, dependency-free functions implementing design step 2
("Internal consistency checks") of the Document Forgery Engine:

  a. ``hours * hourly_rate`` vs declared earnings — a fractional deviation
     *strictly greater* than 2% produces a CRITICAL Indicator (Requirement 7.2).
  b. ``last_day_paid < first_day_worked`` — an impossible date order produces a
     CRITICAL Indicator (Requirement 7.2).

Plus the low-confidence flagging helper re-exported from :mod:`ocr`.

All money and hours arithmetic uses :class:`decimal.Decimal` so comparisons are
exact and boundary behaviour is deterministic:

  * hours*rate vs earnings: deviation == 2.00% -> NO flag; 2.01% -> flag.
  * date order: ``last_day_paid == first_day_worked`` -> NO flag;
    ``last_day_paid < first_day_worked`` -> flag.

Requirements: 7.1, 7.2
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from shared.models import EngineType, Indicator, Severity

# Re-export the low-confidence flagging helper so callers can import the whole
# consistency surface from one module (the task groups it under consistency.py).
from ocr import (  # type: ignore  # noqa: F401  (bare import within service dir)
    DEFAULT_CONFIDENCE_THRESHOLD,
    ExtractedFields,
    low_confidence_indicators,
)

#: Default fractional tolerance between ``hours * hourly_rate`` and declared
#: earnings. 0.02 == 2%. A deviation strictly greater than this fraction is
#: flagged CRITICAL (Requirement 7.2).
DEFAULT_DEVIATION_THRESHOLD: Decimal = Decimal("0.02")


def check_hours_rate_earnings(
    hours: Decimal,
    hourly_rate: Decimal,
    earnings: Decimal,
    *,
    claim_id: Optional[str] = None,
    threshold: Decimal = DEFAULT_DEVIATION_THRESHOLD,
) -> list[Indicator]:
    """Flag an internal earnings inconsistency on the ROE.

    Computes ``expected = hours * hourly_rate`` and compares it to the declared
    ``earnings``. The relative deviation is::

        deviation = |expected - earnings| / earnings

    A single CRITICAL Indicator is returned when ``deviation > threshold``
    (default 2%). The comparison is strict and exact (``Decimal``), so:

      - a deviation of exactly 2% (e.g. earnings 1000.00, expected 1020.00)
        produces NO Indicator;
      - a deviation of 2.01% produces one CRITICAL Indicator.

    When ``earnings`` is zero the relative deviation is undefined; a non-zero
    expected value against zero declared earnings is treated as an inconsistency
    (CRITICAL), while ``expected == earnings == 0`` produces no Indicator.
    """
    hours_d = _as_decimal(hours)
    rate_d = _as_decimal(hourly_rate)
    earnings_d = _as_decimal(earnings)
    threshold_d = _as_decimal(threshold)

    expected = hours_d * rate_d
    absolute_deviation = abs(expected - earnings_d)

    if earnings_d == 0:
        # Undefined relative deviation. Zero-vs-zero is consistent; any non-zero
        # expected earnings against a zero declaration is a hard inconsistency.
        if expected == 0:
            return []
        deviation_display = "undefined (declared earnings are zero)"
        flagged = True
        deviation_value: Optional[Decimal] = None
    else:
        deviation_value = absolute_deviation / earnings_d
        flagged = deviation_value > threshold_d
        deviation_display = f"{deviation_value}"

    if not flagged:
        return []

    payload = {
        "hours": str(hours_d),
        "hourly_rate": str(rate_d),
        "expected_earnings_cad": str(expected),
        "declared_earnings_cad": str(earnings_d),
        "absolute_deviation_cad": str(absolute_deviation),
        "relative_deviation": None if deviation_value is None else str(deviation_value),
        "threshold": str(threshold_d),
    }
    indicator = Indicator(
        indicator_id=uuid.uuid4(),
        claim_id=_coerce_uuid(claim_id),
        engine=EngineType.DOCUMENT_FORGERY,
        severity=Severity.CRITICAL,
        algorithm="hours_rate_earnings_consistency",
        title="ROE earnings inconsistent with hours x hourly rate",
        explanation_en=(
            f"Declared earnings (CAD {earnings_d}) deviate from hours x hourly "
            f"rate ({hours_d} x CAD {rate_d} = CAD {expected}) by "
            f"{deviation_display}, exceeding the "
            f"{threshold_d * 100}% tolerance."
        ),
        explanation_fr=(
            f"Les gains declares (CAD {earnings_d}) s'ecartent du produit "
            f"heures x taux horaire ({hours_d} x CAD {rate_d} = CAD {expected}) "
            f"de {deviation_display}, depassant la tolerance de "
            f"{threshold_d * 100}%."
        ),
        payload=payload,
        produced_at=datetime.now(timezone.utc),
    )
    return [indicator]


def check_date_order(
    last_day_paid: date,
    first_day_worked: date,
    *,
    claim_id: Optional[str] = None,
) -> list[Indicator]:
    """Flag an impossible ROE date order.

    Returns a single CRITICAL Indicator when ``last_day_paid < first_day_worked``
    (the last day for which the employee was paid cannot precede the first day
    they worked). The comparison is strict:

      - ``last_day_paid == first_day_worked`` -> NO Indicator (same-day is valid)
      - ``last_day_paid > first_day_worked``  -> NO Indicator
      - ``last_day_paid < first_day_worked``  -> one CRITICAL Indicator
    """
    if last_day_paid >= first_day_worked:
        return []

    indicator = Indicator(
        indicator_id=uuid.uuid4(),
        claim_id=_coerce_uuid(claim_id),
        engine=EngineType.DOCUMENT_FORGERY,
        severity=Severity.CRITICAL,
        algorithm="date_order_consistency",
        title="ROE last day paid precedes first day worked",
        explanation_en=(
            f"The last day paid ({last_day_paid.isoformat()}) precedes the "
            f"first day worked ({first_day_worked.isoformat()}), an impossible "
            f"date order on the Record of Employment."
        ),
        explanation_fr=(
            f"Le dernier jour paye ({last_day_paid.isoformat()}) precede le "
            f"premier jour travaille ({first_day_worked.isoformat()}), un ordre "
            f"de dates impossible sur le releve d'emploi."
        ),
        payload={
            "last_day_paid": last_day_paid.isoformat(),
            "first_day_worked": first_day_worked.isoformat(),
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
    "DEFAULT_DEVIATION_THRESHOLD",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "check_hours_rate_earnings",
    "check_date_order",
    "low_confidence_indicators",
]
