"""
services/fraud-ring-engine/community_analysis.py
=================================================
Pure analysis functions for the community-cluster and hours-band checks of the
Fraud Ring Engine (task 13.2).

Two independent, dependency-light, deterministic analyses operate over the
employer's claimant *cohort* (the claimants linked to the employer BN within the
prior 24-month window, as returned by the Graph Service community-detection
step). Each function is pure (no I/O); the caller (``main.py``) is responsible
for fetching the cohort from the Graph Service.

1. ``check_shared_attribute_cluster(cohort, ...)`` (Requirement 5.5)
   CRITICAL when >= 40% of the cohort share the first 3 characters of their
   postal code (FSA) OR share a ``surname_hash``. The Indicator identifies the
   matched attribute type ("postal_fsa" | "surname_hash"), the matched value,
   and the list of claimant identifiers in the connected cluster.
   Boundary: 39% -> no flag, 40% -> flag.

2. ``check_hours_band(cohort, min_qualifying_hours, ...)``
   (Requirements 5.6, 5.7)
     - When cohort < 3 -> NOT_EVALUATED disposition (no Indicator).
     - When cohort >= 3 AND every claimant's insurable hours fall within a
       10-hour band whose lower edge is at or above the minimum qualifying
       threshold -> HIGH Indicator stating the hour-band range and the
       threshold value.

Both functions return a ``CheckResult`` carrying the produced Indicators plus a
``disposition`` string ("EVALUATED" | "NOT_EVALUATED") so the caller can record
the NOT_EVALUATED disposition required by Requirement 5.7.

Requirements: 5.5, 5.6, 5.7
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional, Sequence

from shared.models import EngineType, Indicator, Severity

__all__ = [
    "DEFAULT_SHARED_ATTRIBUTE_FRACTION",
    "DEFAULT_HOURS_BAND_WIDTH",
    "MIN_COHORT_FOR_HOURS_BAND",
    "CohortClaimant",
    "CheckResult",
    "check_shared_attribute_cluster",
    "check_hours_band",
]

# Design thresholds (Requirements 5.5–5.7).
DEFAULT_SHARED_ATTRIBUTE_FRACTION = 0.40
DEFAULT_HOURS_BAND_WIDTH = 10.0
MIN_COHORT_FOR_HOURS_BAND = 3


# ---------------------------------------------------------------------------
# Cohort claimant (input shape for the community / hours-band analyses)
# ---------------------------------------------------------------------------


@dataclass
class CohortClaimant:
    """One claimant in the employer's 24-month cohort.

    Attributes
    ----------
    sin_hash:
        SHA-256 hash of the claimant's SIN — the claimant identifier used in
        cluster listings.
    postal_fsa:
        First 3 characters (Forward Sortation Area) of the claimant's postal
        code, or ``None`` when unknown.
    surname_hash:
        SHA-256 hash of the claimant's normalised surname, or ``None``.
    insurable_hours:
        Total insurable hours declared on the claim, or ``None`` when unknown.
    claim_id:
        Optional claim identifier associated with this cohort member.
    """

    sin_hash: str
    postal_fsa: Optional[str] = None
    surname_hash: Optional[str] = None
    insurable_hours: Optional[float] = None
    claim_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Check result wrapper
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """Outcome of a cohort check: produced Indicators plus a disposition.

    ``disposition`` is "EVALUATED" when the check ran to completion, or
    "NOT_EVALUATED" when a precondition (e.g. cohort size) prevented evaluation
    (Requirement 5.7). ``detail`` carries a short machine-readable reason.
    """

    indicators: list[Indicator] = field(default_factory=list)
    disposition: str = "EVALUATED"
    detail: Optional[str] = None


# ---------------------------------------------------------------------------
# Indicator helper
# ---------------------------------------------------------------------------


def _coerce_claim_uuid(claim_id: Optional[str]) -> uuid.UUID:
    """Return a UUID for the Indicator's ``claim_id`` field.

    Mirrors ``detection._coerce_claim_uuid``: pass through UUIDs, parse UUID
    strings, deterministically derive from other strings, else random.
    """
    if isinstance(claim_id, uuid.UUID):
        return claim_id
    if claim_id:
        try:
            return uuid.UUID(str(claim_id))
        except (ValueError, AttributeError, TypeError):
            return uuid.uuid5(uuid.NAMESPACE_OID, str(claim_id))
    return uuid.uuid4()


def _new_indicator(
    *,
    claim_id: Optional[str],
    severity: Severity,
    algorithm: str,
    title: str,
    explanation_en: str,
    explanation_fr: str,
    payload: dict,
) -> Indicator:
    """Build a FRAUD_RING Indicator with a fresh id + UTC timestamp."""
    return Indicator(
        indicator_id=uuid.uuid4(),
        claim_id=_coerce_claim_uuid(claim_id),
        engine=EngineType.FRAUD_RING,
        severity=severity,
        algorithm=algorithm,
        title=title[:100],
        explanation_en=explanation_en[:500],
        explanation_fr=explanation_fr[:500],
        payload=payload,
        produced_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# 1. Shared-attribute community cluster (Req 5.5)
# ---------------------------------------------------------------------------


def _normalise_fsa(value: Optional[str]) -> Optional[str]:
    """Normalise a postal FSA to upper-case first-3, or ``None`` if unusable."""
    if not value:
        return None
    cleaned = str(value).strip().upper().replace(" ", "")
    if not cleaned:
        return None
    return cleaned[:3]


def _dominant_group(
    cohort: Sequence[CohortClaimant], attr: str
) -> tuple[Optional[str], list[CohortClaimant]]:
    """Return the most-shared value of ``attr`` and the members sharing it.

    Members whose attribute is missing/empty are ignored (they cannot form a
    shared cluster). Ties are broken by the lexicographically smallest value for
    deterministic output.
    """
    groups: dict[str, list[CohortClaimant]] = {}
    for member in cohort:
        if attr == "postal_fsa":
            key = _normalise_fsa(member.postal_fsa)
        else:
            raw = getattr(member, attr, None)
            key = str(raw).strip() if raw not in (None, "") else None
        if key is None:
            continue
        groups.setdefault(key, []).append(member)

    if not groups:
        return None, []

    best_value = max(groups, key=lambda v: (len(groups[v]), _neg_lexi(v)))
    return best_value, groups[best_value]


def _neg_lexi(value: str) -> tuple:
    """Tie-break helper: prefer the lexicographically smallest value."""
    return tuple(-ord(c) for c in value)


def check_shared_attribute_cluster(
    cohort: Iterable[CohortClaimant],
    *,
    fraction_threshold: float = DEFAULT_SHARED_ATTRIBUTE_FRACTION,
    claim_id: Optional[str] = None,
    employer_bn: Optional[str] = None,
) -> CheckResult:
    """Flag a connected claimant cluster sharing a postal FSA or surname.

    A CRITICAL Indicator is produced when at least ``fraction_threshold``
    (default 40%, Req 5.5) of the cohort share the same postal FSA OR the same
    ``surname_hash``. When both attributes qualify, the one covering the larger
    fraction of the cohort is reported (ties -> postal_fsa first).

    Parameters
    ----------
    cohort:
        Iterable of ``CohortClaimant`` for the employer's 24-month cohort.
    fraction_threshold:
        Minimum fraction of the cohort that must share an attribute value.
    claim_id:
        Optional id of the claim under analysis, attached to the Indicator.
    employer_bn:
        Optional employer BN recorded on the Indicator payload for evidence.

    Returns
    -------
    CheckResult
        ``indicators`` holds a single CRITICAL Indicator when a cluster is
        detected, otherwise it is empty. ``disposition`` is "NOT_EVALUATED" only
        when the cohort is empty (nothing to evaluate).
    """
    members = list(cohort)
    total = len(members)
    if total == 0:
        return CheckResult(indicators=[], disposition="NOT_EVALUATED", detail="EMPTY_COHORT")

    fsa_value, fsa_group = _dominant_group(members, "postal_fsa")
    surname_value, surname_group = _dominant_group(members, "surname_hash")

    fsa_fraction = len(fsa_group) / total if fsa_group else 0.0
    surname_fraction = len(surname_group) / total if surname_group else 0.0

    fsa_qualifies = fsa_fraction >= fraction_threshold
    surname_qualifies = surname_fraction >= fraction_threshold

    if not fsa_qualifies and not surname_qualifies:
        return CheckResult(indicators=[], disposition="EVALUATED")

    # Report whichever qualifying attribute covers the larger fraction; ties go
    # to postal_fsa for deterministic output.
    if fsa_qualifies and (fsa_fraction >= surname_fraction or not surname_qualifies):
        attr_type = "postal_fsa"
        matched_value = fsa_value
        group = fsa_group
        fraction = fsa_fraction
    else:
        attr_type = "surname_hash"
        matched_value = surname_value
        group = surname_group
        fraction = surname_fraction

    cluster_ids = sorted(m.sin_hash for m in group)
    pct = round(fraction * 100.0, 2)
    attr_label_en = "postal FSA" if attr_type == "postal_fsa" else "surname"
    attr_label_fr = "RTA postale" if attr_type == "postal_fsa" else "nom de famille"

    indicator = _new_indicator(
        claim_id=claim_id,
        severity=Severity.CRITICAL,
        algorithm="community_shared_attribute",
        title=(
            f"Claimant cluster: {len(group)}/{total} share {attr_label_en}"
        ),
        explanation_en=(
            f"{len(group)} of {total} claimants ({pct:.0f}%) in the employer's "
            f"cohort share the same {attr_label_en} ({matched_value}), forming a "
            f"connected cluster consistent with an organised fraud ring."
        ),
        explanation_fr=(
            f"{len(group)} des {total} prestataires ({pct:.0f}%) de la cohorte "
            f"de l'employeur partagent le meme {attr_label_fr} ({matched_value}), "
            f"formant une grappe connectee correspondant a un reseau de fraude "
            f"organise."
        ),
        payload={
            "employer_bn": employer_bn,
            "reason": "SHARED_ATTRIBUTE_CLUSTER",
            "matched_attribute_type": attr_type,
            "matched_value": matched_value,
            "cluster_size": len(group),
            "cohort_size": total,
            "cluster_fraction": round(fraction, 4),
            "fraction_threshold": fraction_threshold,
            "claimant_ids": cluster_ids,
        },
    )
    return CheckResult(indicators=[indicator], disposition="EVALUATED")


# ---------------------------------------------------------------------------
# 2. Hours-band analysis (Req 5.6, 5.7)
# ---------------------------------------------------------------------------


def check_hours_band(
    cohort: Iterable[CohortClaimant],
    min_qualifying_hours: float,
    *,
    band_width: float = DEFAULT_HOURS_BAND_WIDTH,
    min_cohort: int = MIN_COHORT_FOR_HOURS_BAND,
    claim_id: Optional[str] = None,
    employer_bn: Optional[str] = None,
) -> CheckResult:
    """Flag a cohort whose insurable hours cluster in a tight band above threshold.

    Requirement 5.7: when the cohort has fewer than ``min_cohort`` (default 3)
    claimants, the check is NOT_EVALUATED (no Indicator).

    Requirement 5.6: when the cohort has >= ``min_cohort`` claimants AND every
    claimant's insurable hours fall within a ``band_width`` (default 10-hour)
    band whose lower edge is at or above ``min_qualifying_hours``, a HIGH
    Indicator is produced stating the band range and the threshold.

    Parameters
    ----------
    cohort:
        Iterable of ``CohortClaimant`` for the employer's cohort.
    min_qualifying_hours:
        The minimum qualifying insurable-hours threshold for the active benefit
        period configuration (from ESDC config).
    band_width:
        Width of the acceptable hours band (default 10.0 hours).
    min_cohort:
        Minimum cohort size for the check to be evaluated (default 3).
    claim_id, employer_bn:
        Optional identifiers attached to the Indicator / payload.

    Returns
    -------
    CheckResult
        ``disposition`` is "NOT_EVALUATED" when cohort < ``min_cohort`` (Req 5.7)
        or when no member has usable insurable-hours data; otherwise "EVALUATED".
        ``indicators`` holds a single HIGH Indicator when the band condition
        holds.
    """
    members = list(cohort)
    total = len(members)

    # ── Req 5.7: too-small cohort -> NOT_EVALUATED ──
    if total < min_cohort:
        return CheckResult(
            indicators=[],
            disposition="NOT_EVALUATED",
            detail=f"COHORT_BELOW_MIN:{total}<{min_cohort}",
        )

    hours = [
        float(m.insurable_hours)
        for m in members
        if m.insurable_hours is not None
    ]
    # Every claimant must have usable hours to assert "all within band".
    if len(hours) < total or not hours:
        return CheckResult(
            indicators=[],
            disposition="NOT_EVALUATED",
            detail="INSUFFICIENT_HOURS_DATA",
        )

    lo = min(hours)
    hi = max(hours)
    span = hi - lo

    within_band = span <= band_width
    at_or_above_threshold = lo >= float(min_qualifying_hours)

    if not (within_band and at_or_above_threshold):
        return CheckResult(indicators=[], disposition="EVALUATED")

    indicator = _new_indicator(
        claim_id=claim_id,
        severity=Severity.HIGH,
        algorithm="cohort_hours_band",
        title=(
            f"Cohort insurable hours clustered in {span:.1f}h band "
            f"[{lo:.1f}-{hi:.1f}]"
        ),
        explanation_en=(
            f"All {total} claimants in the employer's cohort declared insurable "
            f"hours within a {band_width:.0f}-hour band ({lo:.1f}-{hi:.1f}h), at "
            f"or above the minimum qualifying threshold of "
            f"{float(min_qualifying_hours):.1f}h — consistent with manufactured "
            f"just-qualifying claims."
        ),
        explanation_fr=(
            f"Les {total} prestataires de la cohorte de l'employeur ont declare "
            f"des heures assurables dans une plage de {band_width:.0f} heures "
            f"({lo:.1f}-{hi:.1f} h), au niveau ou au-dessus du seuil "
            f"d'admissibilite minimal de {float(min_qualifying_hours):.1f} h — "
            f"ce qui correspond a des demandes fabriquees juste admissibles."
        ),
        payload={
            "employer_bn": employer_bn,
            "reason": "COHORT_HOURS_BAND",
            "cohort_size": total,
            "band_lower_hours": round(lo, 2),
            "band_upper_hours": round(hi, 2),
            "band_span_hours": round(span, 2),
            "band_width_hours": band_width,
            "min_qualifying_hours": round(float(min_qualifying_hours), 2),
        },
    )
    return CheckResult(indicators=[indicator], disposition="EVALUATED")
