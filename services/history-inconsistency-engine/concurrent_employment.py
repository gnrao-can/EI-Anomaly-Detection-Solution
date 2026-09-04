"""
services/history-inconsistency-engine/concurrent_employment.py
==============================================================
Concurrent employment detection for the Employment History Inconsistency Engine
(task 14.2).

Design ("Employment History Inconsistency Engine", step 5):

    5. Concurrent employment detection:
       - Query CRA for all BNs active for the SIN during claim period
       - If any second employer BN active during any portion of claim period
         -> CRITICAL Indicator

Requirement 6.8 requires the Indicator to identify the overlapping employer and
period expressed as a start date, an end date, the overlap length in days, and
the overlapping employer's Business Number.

Model
-----
The CRA (or provincial employment records) return, for a claimant SIN and claim
period, a list of *employment spans* — each a Business Number with a start date
and an (optional, open-ended) end date. This module:

  * filters out the claim's own employer BN (a claimant is expected to have an
    employment span with the very employer they filed the ROE for),
  * computes the calendar-day overlap between each remaining span and the claim
    period, and
  * emits one CRITICAL Indicator per overlapping *second* employer
    (Requirement 6.8), each carrying the overlapping BN and the overlap
    start/end dates and day count.

Overlap arithmetic uses inclusive calendar dates: two spans that touch on a
single shared day overlap by 1 day. An open-ended employment span (no end date)
is treated as ongoing through the end of the claim period.

Requirements: 6.8
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Optional

from shared.models import EngineType, Indicator, Severity


@dataclass(frozen=True)
class EmploymentSpan:
    """An employment span for a SIN at one employer, from CRA/provincial records.

    Attributes
    ----------
    employer_bn:
        The employer's Business Number.
    start_date:
        First day of the employment span (inclusive).
    end_date:
        Last day of the employment span (inclusive). ``None`` means the span is
        open-ended / ongoing.
    """

    employer_bn: str
    start_date: date
    end_date: Optional[date] = None

    @classmethod
    def from_dict(cls, data: dict) -> "EmploymentSpan":
        """Build a span from a plain dict (as returned by the CRA client)."""
        return cls(
            employer_bn=str(data["employer_bn"]),
            start_date=_coerce_date(data["start_date"]),
            end_date=_coerce_date(data.get("end_date")),
        )


@dataclass(frozen=True)
class Overlap:
    """The inclusive-day overlap between an employment span and the claim period."""

    employer_bn: str
    start_date: date
    end_date: date

    @property
    def days(self) -> int:
        """Number of overlapping calendar days (inclusive of both endpoints)."""
        return (self.end_date - self.start_date).days + 1


def compute_overlap(
    span: EmploymentSpan,
    claim_start: date,
    claim_end: date,
) -> Optional[Overlap]:
    """Return the inclusive overlap between *span* and ``[claim_start, claim_end]``.

    Returns ``None`` when the span does not overlap the claim period at all. An
    open-ended span (``span.end_date is None``) is treated as ongoing through
    ``claim_end``.
    """
    span_end = span.end_date if span.end_date is not None else claim_end
    latest_start = max(span.start_date, claim_start)
    earliest_end = min(span_end, claim_end)
    if latest_start > earliest_end:
        return None
    return Overlap(
        employer_bn=span.employer_bn,
        start_date=latest_start,
        end_date=earliest_end,
    )


def detect_concurrent_employment(
    spans: Iterable[EmploymentSpan],
    claim_start: date,
    claim_end: date,
    *,
    claim_employer_bn: Optional[str] = None,
    claim_id: Optional[str] = None,
) -> list[Indicator]:
    """Return CRITICAL Indicators for second-employer overlaps in the claim period.

    Each employment span whose Business Number differs from ``claim_employer_bn``
    and which overlaps ``[claim_start, claim_end]`` by at least one calendar day
    produces one CRITICAL Indicator (Requirement 6.8). The Indicator payload
    carries the overlapping BN, the overlap start/end dates (ISO-8601), and the
    overlap length in days. Spans for the claim's own employer are ignored, as
    are spans that do not overlap the claim period.
    """
    indicators: list[Indicator] = []
    for span in spans:
        if claim_employer_bn is not None and span.employer_bn == claim_employer_bn:
            continue
        overlap = compute_overlap(span, claim_start, claim_end)
        if overlap is None:
            continue
        indicators.append(
            _concurrent_indicator(overlap, claim_start, claim_end, claim_id)
        )
    return indicators


def _concurrent_indicator(
    overlap: Overlap,
    claim_start: date,
    claim_end: date,
    claim_id: Optional[str],
) -> Indicator:
    return Indicator(
        indicator_id=uuid.uuid4(),
        claim_id=_coerce_uuid(claim_id),
        engine=EngineType.HISTORY_INCONSISTENCY,
        severity=Severity.CRITICAL,
        algorithm="concurrent_employment",
        title="Concurrent employment at a second employer during the claim period",
        explanation_en=(
            f"CRA / provincial employment records show concurrent employment at "
            f"a second employer (BN {overlap.employer_bn}) during the claim "
            f"period, overlapping from {overlap.start_date.isoformat()} to "
            f"{overlap.end_date.isoformat()} ({overlap.days} day(s))."
        ),
        explanation_fr=(
            f"Les dossiers d'emploi de l'ARC / provinciaux indiquent un emploi "
            f"concurrent chez un second employeur (NE {overlap.employer_bn}) "
            f"pendant la periode de demande, chevauchant du "
            f"{overlap.start_date.isoformat()} au {overlap.end_date.isoformat()} "
            f"({overlap.days} jour(s))."
        ),
        payload={
            "overlapping_employer_bn": overlap.employer_bn,
            "overlap_start_date": overlap.start_date.isoformat(),
            "overlap_end_date": overlap.end_date.isoformat(),
            "overlap_days": overlap.days,
            "claim_start_date": claim_start.isoformat(),
            "claim_end_date": claim_end.isoformat(),
        },
        produced_at=datetime.now(timezone.utc),
    )


def _coerce_date(value) -> Optional[date]:
    """Coerce an ISO date string / ``date`` / ``datetime`` to a ``date``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _coerce_uuid(claim_id: Optional[str]) -> uuid.UUID:
    if isinstance(claim_id, uuid.UUID):
        return claim_id
    if claim_id:
        try:
            return uuid.UUID(str(claim_id))
        except (ValueError, AttributeError, TypeError):
            return uuid.uuid5(uuid.NAMESPACE_OID, str(claim_id))
    return uuid.uuid4()


__all__ = [
    "EmploymentSpan",
    "Overlap",
    "compute_overlap",
    "detect_concurrent_employment",
]
