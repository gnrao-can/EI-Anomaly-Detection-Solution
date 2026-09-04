"""
services/cbsa-adapter/absence.py
================================
Pure, side-effect-free logic for the CBSA Adapter.

This module defines the canonical CBSA travel-record model (``CrossingRecord``),
the absence-period result model (``AbsencePeriod``), and the
``detect_absence_periods()`` algorithm exactly as specified in the design:

    Sort crossing records by ``crossing_date`` and identify consecutive
    EXIT -> ENTRY pairs whose gap is **>= 10 days**. Each qualifying pair is
    reported as an ``AbsencePeriod`` covering the time the claimant was outside
    Canada.

The boundary is inclusive on 10 days: a 9-day gap is NOT an absence; a 10-day
gap IS an absence. This function is intentionally free of I/O, logging, and
global state so it can be exhaustively unit- and property-tested.

Requirements: 13.3
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

# The minimum consecutive days outside Canada that constitutes an absence
# period (inclusive lower bound). Exactly 10 days qualifies; 9 days does not.
MIN_ABSENCE_DAYS = 10

# Canonical crossing directions.
DIRECTION_EXIT = "EXIT"
DIRECTION_ENTRY = "ENTRY"


@dataclass(frozen=True)
class CrossingRecord:
    """A single CBSA border-crossing event for a claimant.

    Attributes
    ----------
    direction:
        ``"EXIT"`` (departing Canada) or ``"ENTRY"`` (returning to Canada).
    crossing_date:
        Calendar date of the crossing.
    port_of_entry:
        Port/point at which the crossing was recorded (e.g. ``"YYZ"``).

    Notes
    -----
    Instances are treated as Protected B travel data. Their contents must NOT
    be written to any general-purpose application log (Requirement 13.4).
    """

    direction: str
    crossing_date: date
    port_of_entry: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable ``dict`` representation."""
        return {
            "direction": self.direction,
            "crossing_date": self.crossing_date.isoformat(),
            "port_of_entry": self.port_of_entry,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CrossingRecord":
        """Restore a ``CrossingRecord`` from a ``dict`` (fixture or API row)."""
        raw_date = data["crossing_date"]
        parsed = raw_date if isinstance(raw_date, date) else date.fromisoformat(raw_date)
        return cls(
            direction=str(data["direction"]).upper(),
            crossing_date=parsed,
            port_of_entry=data.get("port_of_entry", ""),
        )


@dataclass(frozen=True)
class AbsencePeriod:
    """A period during which the claimant was outside Canada.

    Attributes
    ----------
    start:
        Date the claimant exited Canada (the EXIT crossing date).
    end:
        Date the claimant re-entered Canada (the matching ENTRY crossing date).
    days:
        Number of days outside Canada (``(end - start).days``); always
        ``>= MIN_ABSENCE_DAYS`` for a reported period.
    """

    start: date
    end: date
    days: int

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable ``dict`` representation."""
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "days": self.days,
        }


def detect_absence_periods(
    records: Iterable[CrossingRecord],
) -> list[AbsencePeriod]:
    """Detect absence periods (consecutive EXIT -> ENTRY gaps >= 10 days).

    The records are sorted by ``crossing_date`` (a stable sort, so records that
    share a date keep their original relative order). The scan then pairs each
    EXIT with the next ENTRY that follows it:

      * On an EXIT, remember the exit date (the most recent EXIT wins if two
        EXITs occur with no ENTRY between them).
      * On an ENTRY, if there is a pending EXIT, compute the gap in days. When
        the gap is ``>= MIN_ABSENCE_DAYS`` (10), record an ``AbsencePeriod``.
        Either way, the pending EXIT is consumed.
      * An ENTRY with no preceding EXIT is ignored (no period opened).
      * A trailing EXIT with no matching ENTRY produces no period (the claimant
        has not been observed returning).

    The function is pure: it does not mutate its input, perform I/O, or log.

    Parameters
    ----------
    records:
        An iterable of ``CrossingRecord`` instances (any order).

    Returns
    -------
    list[AbsencePeriod]
        Absence periods in chronological order (by exit date).
    """
    # Stable sort by crossing date; do not mutate the caller's sequence.
    sorted_records = sorted(records, key=lambda r: r.crossing_date)

    absences: list[AbsencePeriod] = []
    exit_date: date | None = None

    for record in sorted_records:
        direction = (record.direction or "").upper()
        if direction == DIRECTION_EXIT:
            # A new (or replacement) exit; the most recent EXIT is the one that
            # pairs with the next ENTRY.
            exit_date = record.crossing_date
        elif direction == DIRECTION_ENTRY and exit_date is not None:
            gap = (record.crossing_date - exit_date).days
            if gap >= MIN_ABSENCE_DAYS:
                absences.append(
                    AbsencePeriod(
                        start=exit_date,
                        end=record.crossing_date,
                        days=gap,
                    )
                )
            # The pending EXIT is consumed whether or not it qualified.
            exit_date = None
        # ENTRY with no pending EXIT, or any unknown direction, is ignored.

    return absences


__all__ = [
    "MIN_ABSENCE_DAYS",
    "DIRECTION_EXIT",
    "DIRECTION_ENTRY",
    "CrossingRecord",
    "AbsencePeriod",
    "detect_absence_periods",
]
