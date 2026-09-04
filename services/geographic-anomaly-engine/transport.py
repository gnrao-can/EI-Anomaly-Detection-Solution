"""
services/geographic-anomaly-engine/transport.py
================================================
Pure, side-effect-free domain logic for the Geographic Anomaly Engine's
transport-implausibility, economic-cost, and prior-employer analyses (task
12.2).

This module holds only pure functions and small data models so they can be
exhaustively unit- and property-tested without any I/O, network, or FastAPI
plumbing. The HTTP-facing glue (calling the MVR / CBSA adapters, enforcing the
600 ms wall-clock budget, publishing to Kafka) lives in ``main.py``; the
adapter clients live in ``mvr_client.py`` and ``cbsa_client.py``.

What lives here
---------------
  * ``DISTANCE_THRESHOLD_KM`` — the 100 km boundary (strictly greater than 100
    triggers distance-dependent analysis, per Req 4.3/4.4).
  * ``PriorEmployer`` — a light record for a prior employer's location + dates.
  * ``weekly_transport_cost_exceeds_benefit()`` — exact ``Decimal`` comparison
    of the lowest-cost weekly transport cost against the weekly EI benefit rate,
    returning the CAD amounts (2 dp) and the cost-to-benefit ratio for the
    CRITICAL Indicator (Req 4.5).
  * ``prior_employers_over_threshold()`` — selects the claimant's two most
    recent prior employers and reports those located > 100 km from residence,
    with names, distances, and employment period dates (Req 4.7, 4.8).

Requirements: 4.3, 4.4, 4.5, 4.6, 4.7, 4.8
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Distance boundary in kilometres. The distance-dependent checks (MVR, transit,
#: economic cost, prior-employer pattern) run only when the computed distance is
#: STRICTLY greater than this value: exactly 100 km does NOT trigger (Req 4.3).
DISTANCE_THRESHOLD_KM: float = 100.0

#: Two decimal places for all CAD money amounts.
_CENTS = Decimal("0.01")

#: Four decimal places for the cost-to-benefit ratio.
_RATIO_QUANT = Decimal("0.0001")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def exceeds_distance_threshold(distance_km: float) -> bool:
    """Return ``True`` iff ``distance_km`` is strictly greater than 100 km.

    The boundary is exclusive: 100.0 km returns ``False``; 100.001 km returns
    ``True`` (Requirement 4.3 boundary).
    """
    return distance_km > DISTANCE_THRESHOLD_KM


def _to_decimal(value: Any) -> Decimal:
    """Coerce a numeric-ish value to ``Decimal`` via the string path (no float loss)."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _money(value: Any) -> Decimal:
    """Quantise a value to 2 dp CAD using half-up rounding."""
    return _to_decimal(value).quantize(_CENTS, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Economic implausibility: weekly transport cost vs weekly EI benefit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransportCostResult:
    """Result of the weekly transport-cost vs weekly-benefit comparison.

    Attributes
    ----------
    exceeds:
        ``True`` when the weekly transport cost is strictly greater than the
        weekly EI benefit rate (Req 4.5 CRITICAL trigger).
    weekly_transport_cost:
        Lowest-cost weekly transport cost in CAD, quantised to 2 dp.
    weekly_benefit_rate:
        Claimant weekly EI benefit rate in CAD, quantised to 2 dp.
    ratio:
        Cost-to-benefit ratio (cost / benefit) quantised to 4 dp; ``None`` when
        the benefit rate is zero (ratio undefined).
    lowest_cost_mode:
        The transport mode that produced the lowest cost (for the payload).
    """

    exceeds: bool
    weekly_transport_cost: Decimal
    weekly_benefit_rate: Decimal
    ratio: Optional[Decimal]
    lowest_cost_mode: Optional[str] = None

    def payload(self) -> dict[str, Any]:
        """Return the Indicator payload with CAD amounts as 2-dp strings."""
        return {
            "weekly_transport_cost_cad": f"{self.weekly_transport_cost:.2f}",
            "weekly_benefit_rate_cad": f"{self.weekly_benefit_rate:.2f}",
            "cost_to_benefit_ratio": (
                f"{self.ratio:.4f}" if self.ratio is not None else None
            ),
            "lowest_cost_mode": self.lowest_cost_mode,
        }


def lowest_weekly_transport_cost(
    mode_costs: dict[str, Any],
) -> tuple[Optional[Decimal], Optional[str]]:
    """Return the lowest weekly transport cost and its mode from ``mode_costs``.

    ``mode_costs`` maps a mode label (e.g. ``"flight"``, ``"bus"``) to that
    mode's estimated weekly cost in CAD. Entries with a ``None`` cost are
    ignored (that mode is unavailable). Returns ``(None, None)`` when no mode has
    a cost (Req 4.6: transportation cost data unavailable from all sources).
    """
    best_cost: Optional[Decimal] = None
    best_mode: Optional[str] = None
    for mode, raw in mode_costs.items():
        if raw is None:
            continue
        cost = _money(raw)
        if best_cost is None or cost < best_cost:
            best_cost = cost
            best_mode = mode
    return best_cost, best_mode


def weekly_transport_cost_exceeds_benefit(
    mode_costs: dict[str, Any],
    weekly_benefit_rate: Any,
) -> Optional[TransportCostResult]:
    """Compare the lowest weekly transport cost against the weekly EI benefit.

    Uses exact ``Decimal`` arithmetic. The CRITICAL Indicator (Req 4.5) is
    triggered when the lowest weekly transport cost is STRICTLY greater than the
    weekly benefit rate.

    Parameters
    ----------
    mode_costs:
        Mapping of transport mode -> weekly cost in CAD (a ``None`` value means
        that mode is unavailable).
    weekly_benefit_rate:
        The claimant's weekly EI benefit rate in CAD.

    Returns
    -------
    Optional[TransportCostResult]
        A populated result when at least one transport cost is available;
        ``None`` when transportation cost data is unavailable from all sources
        (the caller then emits the MEDIUM data-unavailability Indicator per
        Req 4.6 and does NOT emit the CRITICAL economic Indicator).
    """
    lowest, mode = lowest_weekly_transport_cost(mode_costs)
    if lowest is None:
        return None

    benefit = _money(weekly_benefit_rate)
    exceeds = lowest > benefit

    ratio: Optional[Decimal]
    if benefit > 0:
        ratio = (lowest / benefit).quantize(_RATIO_QUANT, rounding=ROUND_HALF_UP)
    else:
        ratio = None

    return TransportCostResult(
        exceeds=exceeds,
        weekly_transport_cost=lowest,
        weekly_benefit_rate=benefit,
        ratio=ratio,
        lowest_cost_mode=mode,
    )


# ---------------------------------------------------------------------------
# Prior-employer distance pattern
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PriorEmployer:
    """A prior employer's location and employment window for pattern analysis.

    Attributes
    ----------
    employer_name:
        Human-readable employer name (included in the Indicator payload).
    distance_km:
        Distance in km from the claimant's residence to this employer's work
        location (already resolved/computed by the caller).
    employment_start:
        First day of employment with this employer.
    employment_end:
        Last day of employment with this employer.
    """

    employer_name: str
    distance_km: float
    employment_start: Optional[date] = None
    employment_end: Optional[date] = None

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-serialisable payload row for this prior employer."""
        return {
            "employer_name": self.employer_name,
            "distance_km": round(float(self.distance_km), 4),
            "employment_start": (
                self.employment_start.isoformat() if self.employment_start else None
            ),
            "employment_end": (
                self.employment_end.isoformat() if self.employment_end else None
            ),
        }


@dataclass(frozen=True)
class PriorEmployerPattern:
    """Outcome of the two-most-recent prior-employer distance analysis.

    Attributes
    ----------
    evaluated:
        ``False`` when fewer than 2 prior employers exist — no Indicator content
        is produced (Req 4.8); ``True`` otherwise.
    both_over_threshold:
        ``True`` when BOTH of the two most recent prior employers are located
        strictly more than 100 km from the claimant's residence (Req 4.7).
    employers:
        The two most recent prior employers considered (when evaluated).
    """

    evaluated: bool
    both_over_threshold: bool
    employers: tuple[PriorEmployer, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        """Return the payload fragment describing the prior-employer pattern."""
        return {
            "prior_employers_evaluated": self.evaluated,
            "prior_employers_both_over_100km": self.both_over_threshold,
            "prior_employers": [e.to_payload() for e in self.employers],
        }


def prior_employers_over_threshold(
    prior_employers: list[PriorEmployer],
) -> PriorEmployerPattern:
    """Analyse the claimant's two most recent prior employers by distance.

    Selects the two most recent prior employers (assumed to be provided most
    recent first) and determines whether BOTH are located strictly more than
    100 km from the claimant's residence.

    If fewer than 2 prior employers exist, the analysis is omitted entirely and
    no Indicator content is produced (Requirement 4.8).

    Parameters
    ----------
    prior_employers:
        The claimant's prior employers ordered most-recent-first.

    Returns
    -------
    PriorEmployerPattern
        ``evaluated=False`` when fewer than 2 prior employers exist; otherwise a
        result describing whether both recent employers exceed the 100 km
        threshold, carrying their names, distances, and dates.
    """
    if len(prior_employers) < 2:
        return PriorEmployerPattern(evaluated=False, both_over_threshold=False)

    two_recent = tuple(prior_employers[:2])
    both = all(exceeds_distance_threshold(e.distance_km) for e in two_recent)
    return PriorEmployerPattern(
        evaluated=True,
        both_over_threshold=both,
        employers=two_recent,
    )


__all__ = [
    "DISTANCE_THRESHOLD_KM",
    "exceeds_distance_threshold",
    "TransportCostResult",
    "lowest_weekly_transport_cost",
    "weekly_transport_cost_exceeds_benefit",
    "PriorEmployer",
    "PriorEmployerPattern",
    "prior_employers_over_threshold",
]
