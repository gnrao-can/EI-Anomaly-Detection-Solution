"""
services/geographic-anomaly-engine/indicators.py
=================================================
Indicator builders for the Geographic Anomaly Engine's distance-dependent
checks (task 12.2).

Each function returns a fully-formed :class:`shared.models.Indicator` with
bilingual explanations and a structured payload, so ``main.py`` stays focused on
orchestration and budget enforcement. All builders are pure (no I/O).

Indicators produced here:
  * ``no_vehicle_indicator``      — HIGH; distance > 100 km AND no registered
                                     vehicle in any provincial MVR (Req 4.3).
  * ``no_transit_indicator``      — HIGH; distance > 100 km AND no public transit
                                     route connecting the locations (Req 4.4).
  * ``economic_implausibility_indicator`` — CRITICAL; weekly transport cost >
                                     weekly EI benefit, with CAD amounts (2 dp)
                                     and the cost-to-benefit ratio (Req 4.5).
  * ``transport_data_unavailable_indicator`` — MEDIUM; transport cost data
                                     unavailable from all sources (Req 4.6).
  * ``cross_border_absence_indicator`` — HIGH; >= 10 consecutive days outside
                                     Canada during employment (Req 4.9).

The prior-employer pattern (Req 4.7) is NOT a standalone Indicator; its details
are merged into whichever distance-implausibility Indicator is produced, via
``merge_prior_employer_payload``.

Requirements: 4.3, 4.4, 4.5, 4.6, 4.7, 4.9
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from shared.models import EngineType, Indicator, Severity

try:  # sibling modules imported by bare name in the service directory
    from transport import PriorEmployerPattern, TransportCostResult  # type: ignore
except ImportError:  # pragma: no cover - when imported as a package
    from .transport import PriorEmployerPattern, TransportCostResult  # type: ignore


def _coerce_uuid(claim_id: Optional[str]) -> uuid.UUID:
    """Coerce an arbitrary claim id to a UUID (deterministic fallback)."""
    if isinstance(claim_id, uuid.UUID):
        return claim_id
    if claim_id:
        try:
            return uuid.UUID(str(claim_id))
        except (ValueError, AttributeError, TypeError):
            return uuid.uuid5(uuid.NAMESPACE_OID, str(claim_id))
    return uuid.uuid4()


def _base(
    claim_id: Optional[str],
    *,
    severity: Severity,
    algorithm: str,
    title: str,
    explanation_en: str,
    explanation_fr: str,
    payload: dict[str, Any],
) -> Indicator:
    return Indicator(
        indicator_id=uuid.uuid4(),
        claim_id=_coerce_uuid(claim_id),
        engine=EngineType.GEOGRAPHIC_ANOMALY,
        severity=severity,
        algorithm=algorithm,
        title=title,
        explanation_en=explanation_en,
        explanation_fr=explanation_fr,
        payload=payload,
        produced_at=datetime.now(timezone.utc),
    )


def merge_prior_employer_payload(
    indicator: Indicator, pattern: Optional[PriorEmployerPattern]
) -> Indicator:
    """Merge prior-employer pattern details into a distance Indicator's payload.

    When BOTH of the two most recent prior employers are also > 100 km from the
    residence, their names, distances, and employment dates are added to the
    Indicator (Requirement 4.7). When fewer than 2 prior employers exist, or the
    pattern was not evaluated, nothing is merged (Requirement 4.8).
    """
    if pattern is None or not pattern.evaluated:
        return indicator
    if not pattern.both_over_threshold:
        # Still record that the analysis ran but did not match, for traceability.
        indicator.payload["prior_employers_both_over_100km"] = False
        return indicator
    indicator.payload.update(pattern.to_payload())
    return indicator


# ---------------------------------------------------------------------------
# HIGH — no registered vehicle (Req 4.3)
# ---------------------------------------------------------------------------


def no_vehicle_indicator(
    claim_id: Optional[str], *, distance_km: float, province: str
) -> Indicator:
    return _base(
        claim_id,
        severity=Severity.HIGH,
        algorithm="transport_implausibility_vehicle",
        title="No registered vehicle for long-distance commute",
        explanation_en=(
            f"The work location is {distance_km:.4f} km from the claimant's "
            "residence, yet no vehicle is registered to the claimant in any "
            "provincial motor vehicle registry, making the commute implausible."
        ),
        explanation_fr=(
            f"Le lieu de travail se trouve a {distance_km:.4f} km de la "
            "residence du prestataire, mais aucun vehicule n'est immatricule au "
            "nom du prestataire dans un registre provincial des vehicules, ce "
            "qui rend le trajet peu plausible."
        ),
        payload={
            "reason": "NO_REGISTERED_VEHICLE",
            "distance_km": round(float(distance_km), 4),
            "province": province,
        },
    )


# ---------------------------------------------------------------------------
# HIGH — no public transit route (Req 4.4)
# ---------------------------------------------------------------------------


def no_transit_indicator(claim_id: Optional[str], *, distance_km: float) -> Indicator:
    return _base(
        claim_id,
        severity=Severity.HIGH,
        algorithm="transport_implausibility_transit",
        title="No public transit route for long-distance commute",
        explanation_en=(
            f"The work location is {distance_km:.4f} km from the claimant's "
            "residence and no public transit route connecting the two locations "
            "exists, making the declared commute implausible."
        ),
        explanation_fr=(
            f"Le lieu de travail se trouve a {distance_km:.4f} km de la "
            "residence du prestataire et aucun itineraire de transport en commun "
            "ne relie les deux emplacements, ce qui rend le trajet declare peu "
            "plausible."
        ),
        payload={
            "reason": "NO_TRANSIT_ROUTE",
            "distance_km": round(float(distance_km), 4),
        },
    )


# ---------------------------------------------------------------------------
# CRITICAL — economic implausibility (Req 4.5)
# ---------------------------------------------------------------------------


def economic_implausibility_indicator(
    claim_id: Optional[str], *, distance_km: float, cost: TransportCostResult
) -> Indicator:
    payload: dict[str, Any] = {
        "reason": "TRANSPORT_COST_EXCEEDS_BENEFIT",
        "distance_km": round(float(distance_km), 4),
    }
    payload.update(cost.payload())
    ratio_text = (
        f"{cost.ratio:.4f}" if cost.ratio is not None else "n/a (zero benefit)"
    )
    return _base(
        claim_id,
        severity=Severity.CRITICAL,
        algorithm="economic_implausibility",
        title="Weekly transport cost exceeds weekly EI benefit",
        explanation_en=(
            f"The lowest-cost weekly transportation cost of CAD "
            f"{cost.weekly_transport_cost:.2f} exceeds the claimant's weekly EI "
            f"benefit rate of CAD {cost.weekly_benefit_rate:.2f} "
            f"(cost-to-benefit ratio {ratio_text}), making the commute "
            "economically implausible."
        ),
        explanation_fr=(
            f"Le cout hebdomadaire de transport le plus bas de "
            f"{cost.weekly_transport_cost:.2f} CAD depasse le taux de prestation "
            f"d'AE hebdomadaire du prestataire de "
            f"{cost.weekly_benefit_rate:.2f} CAD (ratio cout-prestation "
            f"{ratio_text}), rendant le trajet economiquement peu plausible."
        ),
        payload=payload,
    )


# ---------------------------------------------------------------------------
# MEDIUM — transport cost data unavailable (Req 4.6)
# ---------------------------------------------------------------------------


def transport_data_unavailable_indicator(
    claim_id: Optional[str], *, distance_km: float
) -> Indicator:
    return _base(
        claim_id,
        severity=Severity.MEDIUM,
        algorithm="economic_implausibility",
        title="Transportation cost data unavailable",
        explanation_en=(
            "Transportation cost data was unavailable from all sources; the "
            "economic implausibility check could not be completed for this claim."
        ),
        explanation_fr=(
            "Les donnees sur les couts de transport n'etaient disponibles "
            "aupres d'aucune source; la verification de l'invraisemblance "
            "economique n'a pas pu etre effectuee pour cette demande."
        ),
        payload={
            "reason": "TRANSPORT_COST_DATA_UNAVAILABLE",
            "distance_km": round(float(distance_km), 4),
        },
    )


# ---------------------------------------------------------------------------
# HIGH — cross-border absence (Req 4.9)
# ---------------------------------------------------------------------------


def cross_border_absence_indicator(
    claim_id: Optional[str], *, max_absence_days: int, partial: bool = False
) -> Indicator:
    return _base(
        claim_id,
        severity=Severity.HIGH,
        algorithm="cbsa_absence_detection",
        title="Extended absence from Canada during employment period",
        explanation_en=(
            f"CBSA travel records indicate the claimant was outside Canada for "
            f"{max_absence_days} consecutive days during the employment period, "
            "which is inconsistent with active employment."
        ),
        explanation_fr=(
            f"Les dossiers de voyage de l'ASFC indiquent que le prestataire a "
            f"ete a l'exterieur du Canada pendant {max_absence_days} jours "
            "consecutifs durant la periode d'emploi, ce qui est incompatible "
            "avec un emploi actif."
        ),
        payload={
            "reason": "CROSS_BORDER_ABSENCE",
            "max_absence_days": int(max_absence_days),
            "data_partial": bool(partial),
        },
    )


__all__ = [
    "merge_prior_employer_payload",
    "no_vehicle_indicator",
    "no_transit_indicator",
    "economic_implausibility_indicator",
    "transport_data_unavailable_indicator",
    "cross_border_absence_indicator",
]
