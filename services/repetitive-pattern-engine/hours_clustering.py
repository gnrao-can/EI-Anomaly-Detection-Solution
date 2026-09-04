"""
services/repetitive-pattern-engine/hours_clustering.py
======================================================
Hours-clustering detector for the Repetitive Pattern Engine (task 11.2).

Requirement 3.4:

    WHEN a claim's insurable hours are within 5 hours of the minimum qualifying
    threshold anchored to the minimum qualifying threshold for the claimant's
    economic region AND the same pattern appears in two or more prior claims
    from the same SIN, THE Repetitive_Pattern_Engine SHALL generate a
    HIGH-severity Indicator describing the hours-clustering pattern.

``check_hours_clustering`` is a pure, dependency-light, deterministic function.
It compares the current claim's insurable hours to the region's minimum
qualifying threshold and, when the current claim sits within ``tolerance`` hours
of that threshold AND at least ``min_prior`` prior claims from the same SIN also
sit within ``tolerance`` of the threshold, it produces a single HIGH Indicator.

Design mapping (design "Repetitive Pattern Engine" pipeline step 4):
    Retrieve regional minimum qualifying threshold from ESDC config
    Flag if |insurable_hours - threshold| <= 5 AND same pattern in >= 2 prior claims

Boundary semantics
------------------
* "within 5 hours" is inclusive: a claim with ``|hours - threshold| == 5.0``
  sits within tolerance; a claim at ``5.01`` does not.
* "two or more prior claims" counts PRIOR claims only (the current claim is not
  counted toward the ``min_prior`` requirement).

The function is pure (no I/O) and operates on already-retrieved data.

Requirements: 3.4
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Iterable, Optional

from shared.models import EngineType, Indicator, Severity

__all__ = [
    "DEFAULT_HOURS_TOLERANCE",
    "DEFAULT_MIN_PRIOR_CLAIMS",
    "check_hours_clustering",
]

DEFAULT_HOURS_TOLERANCE = 5.0
DEFAULT_MIN_PRIOR_CLAIMS = 2


def _coerce_claim_uuid(claim_id: Optional[str]) -> uuid.UUID:
    """Return a UUID for the Indicator's ``claim_id`` field.

    Mirrors the coercion used by the other detectors: pass a real UUID through,
    derive a stable UUID5 from a non-UUID string, or mint a fresh UUID for
    ``None``.
    """
    if isinstance(claim_id, uuid.UUID):
        return claim_id
    if claim_id:
        try:
            return uuid.UUID(str(claim_id))
        except (ValueError, AttributeError, TypeError):
            return uuid.uuid5(uuid.NAMESPACE_OID, str(claim_id))
    return uuid.uuid4()


def _within_tolerance(hours: float, threshold: float, tolerance: float) -> bool:
    """True when ``hours`` is within ``tolerance`` (inclusive) of ``threshold``."""
    return abs(float(hours) - float(threshold)) <= tolerance


def check_hours_clustering(
    current_hours: float,
    prior_claims: Iterable,
    *,
    threshold: float,
    tolerance: float = DEFAULT_HOURS_TOLERANCE,
    min_prior: int = DEFAULT_MIN_PRIOR_CLAIMS,
    claim_id: Optional[str] = None,
    region_code: Optional[str] = None,
) -> list[Indicator]:
    """Flag insurable hours clustering near the regional minimum threshold.

    Parameters
    ----------
    current_hours:
        Insurable hours declared on the claim under analysis.
    prior_claims:
        Iterable of prior-claim objects exposing an ``insurable_hours`` attribute
        (e.g. ``ClaimHistoryRecord``). Only prior claims are considered when
        counting the recurring near-threshold pattern.
    threshold:
        The region's minimum qualifying insurable-hours threshold.
    tolerance:
        Half-width (in hours) of the "near threshold" band (default 5.0, Req
        3.4). Inclusive: exactly ``tolerance`` away still counts.
    min_prior:
        Minimum number of PRIOR claims that must also sit within tolerance for
        the Indicator to fire (default 2, Req 3.4 "two or more prior claims").
    claim_id:
        Optional id of the claim under analysis, attached to the Indicator.
    region_code:
        Optional ESDC economic region code, recorded in the Indicator payload.

    Returns
    -------
    list[Indicator]
        A single-element list with the HIGH clustering Indicator when the
        current claim AND at least ``min_prior`` prior claims cluster near the
        threshold; otherwise an empty list.
    """
    # The current claim must itself sit within the near-threshold band.
    if not _within_tolerance(current_hours, threshold, tolerance):
        return []

    prior_near = [
        float(c.insurable_hours)
        for c in prior_claims
        if _within_tolerance(getattr(c, "insurable_hours", 0.0), threshold, tolerance)
    ]

    if len(prior_near) < min_prior:
        return []

    total_near = len(prior_near) + 1  # + the current claim
    lower = threshold - tolerance
    upper = threshold + tolerance

    return [
        Indicator(
            indicator_id=uuid.uuid4(),
            claim_id=_coerce_claim_uuid(claim_id),
            engine=EngineType.REPETITIVE_PATTERN,
            severity=Severity.HIGH,
            algorithm="hours_clustering",
            title=(
                f"Insurable hours cluster within {tolerance:.0f}h of the "
                f"{threshold:.0f}h regional minimum across {total_near} claims"
            )[:100],
            explanation_en=(
                f"The claim's {current_hours:.0f} insurable hours fall within "
                f"{tolerance:.0f} hours of the regional minimum qualifying "
                f"threshold of {threshold:.0f} hours, and {len(prior_near)} prior "
                f"claims from the same claimant also cluster in the "
                f"{lower:.0f}-{upper:.0f} hour band — a systematic near-threshold "
                f"hours pattern."
            )[:500],
            explanation_fr=(
                f"Les {current_hours:.0f} heures assurables de la demande se "
                f"situent a moins de {tolerance:.0f} heures du seuil regional "
                f"minimal d'admissibilite de {threshold:.0f} heures, et "
                f"{len(prior_near)} demandes anterieures du meme prestataire se "
                f"regroupent aussi dans la plage de {lower:.0f} a {upper:.0f} "
                f"heures — un schema systematique proche du seuil."
            )[:500],
            payload={
                "current_insurable_hours": float(current_hours),
                "threshold_hours": float(threshold),
                "tolerance_hours": float(tolerance),
                "band_lower": float(lower),
                "band_upper": float(upper),
                "prior_near_threshold_count": len(prior_near),
                "clustered_claim_count": total_near,
                "prior_near_threshold_hours": prior_near,
                "economic_region_code": region_code,
            },
            produced_at=datetime.now(timezone.utc),
        )
    ]
