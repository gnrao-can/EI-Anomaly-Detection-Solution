"""
services/llm-service/templates.py
=================================
Deterministic EN/FR explanation templates for the EI-ADS LLM Service.

These templates back the *fallback* explanation path used by
:class:`explanation.ExplanationGenerator` whenever the LLM produces a malformed
or empty response (or is unavailable in the sandbox). They are intentionally
plain, bilingual, and deterministic so that:

  * every claim still receives a human-readable EN **and** FR explanation, and
  * the two language outputs stay roughly balanced in length (they are built
    from parallel sentence structures), satisfying the spirit of P15.

The templates are keyed by :class:`~shared.models.EngineType` (which engine
produced the indicator) with a generic fallback, and the decision preamble is
keyed by :class:`~shared.models.RiskDecision`.

Requirements: 9.4, 9.5, 9.9, 20.2, 20.3
"""

from __future__ import annotations

from shared.models import EngineType, RiskDecision

# ---------------------------------------------------------------------------
# Decision preambles — one short sentence stating the outcome + score.
# ``{score}`` is substituted at render time.
# ---------------------------------------------------------------------------

DECISION_PREAMBLE_EN: dict[RiskDecision, str] = {
    RiskDecision.AUTO_APPROVE: "This claim was automatically approved with a risk score of {score}/100.",
    RiskDecision.FLAG_FOR_REVIEW: "This claim was flagged for review with a risk score of {score}/100.",
    RiskDecision.INVESTIGATE: "This claim was referred for investigation with a risk score of {score}/100.",
    RiskDecision.INDETERMINATE: "This claim could not be scored conclusively (risk score {score}/100).",
}

# Note (P15 reconciliation): the FR wording below is kept intentionally concise
# so the EN/FR *character* lengths stay within the 20% balance band required by
# Requirement 20.6 (reconciled from word count to character length).
DECISION_PREAMBLE_FR: dict[RiskDecision, str] = {
    RiskDecision.AUTO_APPROVE: "Demande approuvee automatiquement, score de risque {score}/100.",
    RiskDecision.FLAG_FOR_REVIEW: "Demande signalee pour examen, score de risque {score}/100.",
    RiskDecision.INVESTIGATE: "Demande transmise pour enquete, score de risque {score}/100.",
    RiskDecision.INDETERMINATE: "Demande non evaluee de facon concluante, score {score}/100.",
}

# ---------------------------------------------------------------------------
# Per-engine indicator phrases. ``{title}`` is the indicator title.
# One EN and one FR phrase per engine, plus a generic fallback.
# ---------------------------------------------------------------------------

ENGINE_PHRASE_EN: dict[EngineType, str] = {
    EngineType.REPETITIVE_PATTERN: "a repetitive claim pattern ({title})",
    EngineType.GEOGRAPHIC_ANOMALY: "a geographic anomaly ({title})",
    EngineType.FRAUD_RING: "a potential fraud ring link ({title})",
    EngineType.HISTORY_INCONSISTENCY: "an employment history inconsistency ({title})",
    EngineType.DOCUMENT_FORGERY: "a document integrity concern ({title})",
    EngineType.REGIONAL_ANALYSIS: "a regional labour market signal ({title})",
}

ENGINE_PHRASE_FR: dict[EngineType, str] = {
    EngineType.REPETITIVE_PATTERN: "un schema de demandes repetitives ({title})",
    EngineType.GEOGRAPHIC_ANOMALY: "une anomalie geographique ({title})",
    EngineType.FRAUD_RING: "un lien avec un reseau de fraude ({title})",
    EngineType.HISTORY_INCONSISTENCY: "une incoherence dans l'historique d'emploi ({title})",
    EngineType.DOCUMENT_FORGERY: "un probleme d'integrite documentaire ({title})",
    EngineType.REGIONAL_ANALYSIS: "un signal du marche du travail regional ({title})",
}

_GENERIC_PHRASE_EN = "an anomaly indicator ({title})"
_GENERIC_PHRASE_FR = "un indicateur d'anomalie ({title})"

# Sentence connectors used when stitching the top indicators together.
_LEAD_EN = "Key factors: "
_LEAD_FR = "Facteurs cles : "
_NO_INDICATORS_EN = "No anomaly indicators were raised for this claim."
_NO_INDICATORS_FR = "Aucun indicateur d'anomalie n'a ete releve pour cette demande."


def engine_phrase_en(engine: EngineType, title: str) -> str:
    """Return the English phrase for ``engine`` with ``title`` substituted."""
    template = ENGINE_PHRASE_EN.get(engine, _GENERIC_PHRASE_EN)
    return template.format(title=title)


def engine_phrase_fr(engine: EngineType, title: str) -> str:
    """Return the French phrase for ``engine`` with ``title`` substituted."""
    template = ENGINE_PHRASE_FR.get(engine, _GENERIC_PHRASE_FR)
    return template.format(title=title)


def decision_preamble_en(decision: RiskDecision, score: int) -> str:
    """Return the English decision preamble sentence."""
    template = DECISION_PREAMBLE_EN.get(
        decision, DECISION_PREAMBLE_EN[RiskDecision.FLAG_FOR_REVIEW]
    )
    return template.format(score=score)


def decision_preamble_fr(decision: RiskDecision, score: int) -> str:
    """Return the French decision preamble sentence."""
    template = DECISION_PREAMBLE_FR.get(
        decision, DECISION_PREAMBLE_FR[RiskDecision.FLAG_FOR_REVIEW]
    )
    return template.format(score=score)


__all__ = [
    "DECISION_PREAMBLE_EN",
    "DECISION_PREAMBLE_FR",
    "ENGINE_PHRASE_EN",
    "ENGINE_PHRASE_FR",
    "engine_phrase_en",
    "engine_phrase_fr",
    "decision_preamble_en",
    "decision_preamble_fr",
]
