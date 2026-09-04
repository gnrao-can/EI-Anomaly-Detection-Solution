"""
services/repetitive-pattern-engine/patterns.py
===============================================
Pure detection functions for the Repetitive Pattern Engine (task 11.1).

Two independent, dependency-light, deterministic detectors operate over a
claimant's prior claims:

1. ``check_employer_repetition(claims, ...)``
   Flags systematic re-claiming against a single employer: if >= 3 claims share
   the same employer Business Number within ANY rolling 36-month window, a HIGH
   Indicator is produced (Requirement 3.2).
     - Boundary: exactly 2 same-employer claims -> no flag; exactly 3 -> flag.
     - "any 36-month window" is evaluated with a sliding window over the
       claims sorted by separation_date, so a trio spanning MORE than 36 months
       does not flag unless some 3 of them fit inside a single 36-month span.

2. ``sequential_pattern_mining(claim_dates, ...)``
   A PrefixSpan-style detection over the ordered claim-date sequence. Rather
   than pulling in a heavy sequential-pattern-mining library, we implement the
   pattern directly: we look for a repeating *cadence* in the consecutive
   inter-claim intervals. Concretely, for N >= 3 claims we take the N-1
   consecutive gaps (in days) and test whether they cluster within +/-14 days of
   a common cadence. If they do, a HIGH pattern Indicator is produced
   (Requirements 3.3, 3.8 / Property 1).

Approach note (PrefixSpan analogue)
-----------------------------------
Classic PrefixSpan mines frequent subsequences by projecting a database of
sequences on frequent prefixes. Here the "database" is a single claimant's
date sequence and the pattern of interest is a *regular temporal cadence*
(equally-spaced-within-tolerance claims). The frequent-pattern question
therefore reduces to: "is there a length->=3 run whose consecutive gaps are all
within +/-tolerance of each other?" We detect that by scanning the consecutive
gaps and growing the longest run of gaps that stay within ``2 * tolerance`` of
each other (any two gaps that are each within +/-tolerance of a shared cadence
differ by at most ``2 * tolerance``). A run of >= 2 qualifying gaps corresponds
to >= 3 claims forming the repeating pattern. This is O(N), deterministic, and
has no third-party dependency.

Property 1 (completeness): for any N >= 3 claims whose *consecutive*
inter-claim intervals are all within +/-14 days of each other, at least one
pattern Indicator is produced. Because all N-1 gaps are then mutually within
``2 * 14`` days, the whole sequence forms a single qualifying run and this
function returns a pattern Indicator — satisfying P1.

All functions are pure (no I/O) and operate on already-retrieved data.

Requirements: 3.2, 3.3, 3.8
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Iterable, Optional

from shared.models import EngineType, Indicator, Severity

__all__ = [
    "DEFAULT_EMPLOYER_WINDOW_MONTHS",
    "DEFAULT_EMPLOYER_THRESHOLD",
    "DEFAULT_INTERVAL_TOLERANCE_DAYS",
    "check_employer_repetition",
    "sequential_pattern_mining",
]

DEFAULT_EMPLOYER_WINDOW_MONTHS = 36
DEFAULT_EMPLOYER_THRESHOLD = 3
DEFAULT_INTERVAL_TOLERANCE_DAYS = 14

# Approximate days per month for the rolling employer-repetition window. Using a
# fixed 30.4375-day month (365.25 / 12) keeps the "36-month window" test free of
# calendar-arithmetic edge cases while remaining accurate to within a day or so.
_DAYS_PER_MONTH = 365.25 / 12.0


def _new_indicator(
    *,
    claim_id: Optional[str],
    algorithm: str,
    title: str,
    explanation_en: str,
    explanation_fr: str,
    payload: dict,
) -> Indicator:
    """Build a HIGH REPETITIVE_PATTERN Indicator with a fresh id + timestamp."""
    return Indicator(
        indicator_id=uuid.uuid4(),
        claim_id=_coerce_claim_uuid(claim_id),
        engine=EngineType.REPETITIVE_PATTERN,
        severity=Severity.HIGH,
        algorithm=algorithm,
        title=title[:100],
        explanation_en=explanation_en[:500],
        explanation_fr=explanation_fr[:500],
        payload=payload,
        produced_at=datetime.now(timezone.utc),
    )


def _coerce_claim_uuid(claim_id: Optional[str]) -> uuid.UUID:
    """Return a UUID for the Indicator's ``claim_id`` field.

    The Indicator model types ``claim_id`` as a UUID. When the caller passes a
    non-UUID string (or ``None``), we substitute a deterministic-enough UUID so
    the model stays well-typed without raising.
    """
    if isinstance(claim_id, uuid.UUID):
        return claim_id
    if claim_id:
        try:
            return uuid.UUID(str(claim_id))
        except (ValueError, AttributeError, TypeError):
            return uuid.uuid5(uuid.NAMESPACE_OID, str(claim_id))
    return uuid.uuid4()


# ---------------------------------------------------------------------------
# 1. Employer repetition — >= 3 claims from the same employer BN in any
#    36-month window (Req 3.2)
# ---------------------------------------------------------------------------


def check_employer_repetition(
    claims: Iterable,
    *,
    window_months: int = DEFAULT_EMPLOYER_WINDOW_MONTHS,
    threshold: int = DEFAULT_EMPLOYER_THRESHOLD,
    claim_id: Optional[str] = None,
) -> list[Indicator]:
    """Flag >= ``threshold`` claims from the same employer BN in any window.

    Parameters
    ----------
    claims:
        Iterable of objects exposing ``employer_bn`` (str) and
        ``separation_date`` (``datetime.date``) attributes — e.g.
        ``ClaimHistoryRecord``.
    window_months:
        Rolling window size in months (default 36, Req 3.2).
    threshold:
        Minimum same-employer claim count within a single window that triggers a
        HIGH Indicator (default 3). Boundary: 2 -> no flag, 3 -> flag.
    claim_id:
        Optional id of the claim under analysis, attached to the Indicator.

    Returns
    -------
    list[Indicator]
        One HIGH Indicator per employer BN that meets the threshold within some
        rolling window; empty when none qualify.
    """
    window_days = window_months * _DAYS_PER_MONTH

    # Group claims by employer BN.
    by_employer: dict[str, list[date]] = {}
    for c in claims:
        by_employer.setdefault(c.employer_bn, []).append(c.separation_date)

    indicators: list[Indicator] = []
    for employer_bn, dates in by_employer.items():
        if len(dates) < threshold:
            continue

        ordered = sorted(dates)
        max_in_window = _max_count_in_window(ordered, window_days)
        if max_in_window >= threshold:
            indicators.append(
                _new_indicator(
                    claim_id=claim_id,
                    algorithm="employer_repetition",
                    title=(
                        f"{max_in_window} claims against employer {employer_bn} "
                        f"within {window_months} months"
                    ),
                    explanation_en=(
                        f"Detected {max_in_window} Employment Insurance claims filed "
                        f"against the same employer (BN {employer_bn}) within a "
                        f"{window_months}-month period, at or above the "
                        f"repetition threshold of {threshold}."
                    ),
                    explanation_fr=(
                        f"Detection de {max_in_window} demandes d'assurance-emploi "
                        f"deposees contre le meme employeur (NE {employer_bn}) sur "
                        f"une periode de {window_months} mois, atteignant ou "
                        f"depassant le seuil de repetition de {threshold}."
                    ),
                    payload={
                        "employer_bn": employer_bn,
                        "claim_count": max_in_window,
                        "window_months": window_months,
                        "threshold": threshold,
                        "separation_dates": [d.isoformat() for d in ordered],
                    },
                )
            )

    # Deterministic ordering of Indicators by employer BN for stable output.
    indicators.sort(key=lambda i: i.payload.get("employer_bn", ""))
    return indicators


def _max_count_in_window(sorted_dates: list[date], window_days: float) -> int:
    """Max number of dates that fit inside any window of ``window_days`` days.

    Sliding window over the ascending-sorted dates: for each right endpoint,
    advance the left endpoint until the span is within ``window_days``. The
    window is inclusive of both endpoints (a span of exactly ``window_days``
    still counts).
    """
    if not sorted_dates:
        return 0

    left = 0
    best = 1
    for right in range(len(sorted_dates)):
        while (sorted_dates[right] - sorted_dates[left]).days > window_days:
            left += 1
        best = max(best, right - left + 1)
    return best


# ---------------------------------------------------------------------------
# 2. Sequential pattern mining — repeating inter-claim cadence within +/-14
#    days (Req 3.3, 3.8 / Property 1)
# ---------------------------------------------------------------------------


def sequential_pattern_mining(
    claim_dates: Iterable[date],
    *,
    tolerance_days: int = DEFAULT_INTERVAL_TOLERANCE_DAYS,
    min_claims: int = 3,
    claim_id: Optional[str] = None,
) -> list[Indicator]:
    """Detect a repeating temporal cadence in a claimant's claim-date sequence.

    A PrefixSpan-style detection (see the module docstring for the approach):
    the consecutive inter-claim gaps are scanned for the longest run whose gaps
    all stay within ``2 * tolerance_days`` of one another. A run of >= 2
    qualifying gaps corresponds to >= ``min_claims`` (3) claims forming the
    repeating pattern and yields a single HIGH pattern Indicator.

    Parameters
    ----------
    claim_dates:
        Iterable of ``datetime.date`` claim dates (order-independent; sorted
        internally).
    tolerance_days:
        Half-width of the interval tolerance band (default +/-14 days).
    min_claims:
        Minimum number of claims that must form the pattern (default 3).
    claim_id:
        Optional id of the claim under analysis, attached to the Indicator.

    Returns
    -------
    list[Indicator]
        A single-element list with the HIGH pattern Indicator when a qualifying
        cadence is found; otherwise an empty list.
    """
    dates = sorted(set(claim_dates))
    if len(dates) < min_claims:
        return []

    # Consecutive inter-claim gaps in days.
    gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]

    # Two gaps each within +/-tolerance of a shared cadence differ by at most
    # 2 * tolerance. Grow the longest run of consecutive gaps whose spread
    # (max - min within the run) stays within that band.
    band = 2 * tolerance_days
    best_start, best_len = 0, 1
    run_start = 0
    run_min = run_max = gaps[0]
    for i in range(1, len(gaps)):
        candidate_min = min(run_min, gaps[i])
        candidate_max = max(run_max, gaps[i])
        if candidate_max - candidate_min <= band:
            run_min, run_max = candidate_min, candidate_max
        else:
            # Restart the run at the current gap.
            run_start = i
            run_min = run_max = gaps[i]
        run_len = i - run_start + 1
        if run_len > best_len:
            best_len, best_start = run_len, run_start

    # A run of >= (min_claims - 1) gaps => a pattern over >= min_claims claims.
    if best_len < (min_claims - 1):
        return []

    run_gaps = gaps[best_start : best_start + best_len]
    # The pattern spans best_len + 1 claims starting at date index best_start.
    pattern_dates = dates[best_start : best_start + best_len + 1]
    cadence = sum(run_gaps) / len(run_gaps)

    return [
        _new_indicator(
            claim_id=claim_id,
            algorithm="sequential_pattern_mining",
            title=(
                f"Repeating claim cadence over {len(pattern_dates)} claims "
                f"(~{cadence:.0f}-day interval)"
            ),
            explanation_en=(
                f"Detected {len(pattern_dates)} claims filed on a repeating "
                f"cadence of approximately {cadence:.0f} days, with all "
                f"inter-claim intervals within +/-{tolerance_days} days of one "
                f"another — a systematic sequential pattern."
            ),
            explanation_fr=(
                f"Detection de {len(pattern_dates)} demandes deposees selon une "
                f"cadence repetitive d'environ {cadence:.0f} jours, tous les "
                f"intervalles entre demandes se situant a +/-{tolerance_days} "
                f"jours les uns des autres — un schema sequentiel systematique."
            ),
            payload={
                "pattern_length": len(pattern_dates),
                "intervals_days": run_gaps,
                "tolerance_days": tolerance_days,
                "approx_cadence_days": round(cadence, 2),
                "claim_dates": [d.isoformat() for d in pattern_dates],
            },
        )
    ]
