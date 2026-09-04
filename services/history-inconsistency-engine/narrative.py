"""
services/history-inconsistency-engine/narrative.py
====================================================
Async LLM narrative-contradiction check for the Employment History
Inconsistency Engine (task 14.2).

Design ("Employment History Inconsistency Engine", step 4):

    4. LLM contradiction detection (async, timeout 10 s):
       - If CRA shows continued employment during claimed separation period:
         -> Pass to LLM: "Does this narrative contradict continued
            employment? (YES/NO + evidence)"
         -> If LLM returns YES -> CRITICAL Indicator

Requirements 6.6 and 6.7:
  * 6.6 — when the LLM classifies the ROE reason-for-separation narrative as
    *contradicted* by CRA records showing continued employment for the same SIN
    at the same employer during the claimed separation period, produce a
    CRITICAL Indicator.
  * 6.7 — if the LLM does not return a classification within **10 seconds**,
    record a ``NARRATIVE_ANALYSIS_TIMEOUT`` flag in the Indicator and proceed
    without the narrative result.

The LLM Service is injectable: :class:`NarrativeContradictionChecker` takes an
async callable (the "client") so tests supply a fake — no live LLM/network is
used. The client returns a dict shaped ``{"contradicted": bool, "evidence":
str}``. On timeout the checker emits a low-severity (UNRESOLVED) Indicator
carrying the ``NARRATIVE_ANALYSIS_TIMEOUT`` flag rather than blocking.

This check runs *outside* the engine's 500 ms wall-clock budget (Requirement
6.9 excludes LLM wait time). The engine awaits it separately so a slow or timed
out LLM never pushes the core analysis over budget.

Requirements: 6.6, 6.7, 6.9
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from shared.models import EngineType, Indicator, Severity

logger = logging.getLogger("history-inconsistency-engine.narrative")

#: Async LLM client signature: given the ROE narrative and a short description
#: of the CRA continued-employment evidence, return a classification dict
#: ``{"contradicted": bool, "evidence": str}``.
LLMClient = Callable[[str, str], Awaitable[dict[str, Any]]]

#: Flag recorded on the Indicator when the LLM does not answer in time (6.7).
NARRATIVE_ANALYSIS_TIMEOUT = "NARRATIVE_ANALYSIS_TIMEOUT"

#: Default LLM timeout in seconds (Requirement 6.7).
DEFAULT_LLM_TIMEOUT_SECONDS: float = 10.0


class NarrativeContradictionChecker:
    """Run the async LLM contradiction check with a hard 10-second timeout.

    Parameters
    ----------
    llm_client:
        An async callable ``(narrative, cra_evidence) -> {"contradicted": bool,
        "evidence": str}``. Tests inject a fake; production wires the LLM
        Service HTTP client here.
    timeout_seconds:
        Maximum time to wait for the LLM (Requirement 6.7). Defaults to 10 s.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS,
    ) -> None:
        self._llm_client = llm_client
        self._timeout_seconds = timeout_seconds

    async def check(
        self,
        narrative: str,
        cra_evidence: str,
        *,
        claim_id: Optional[str] = None,
    ) -> list[Indicator]:
        """Classify the narrative and return Indicators.

        Behaviour:
          * LLM answers ``contradicted=True`` within the timeout -> one CRITICAL
            Indicator (Requirement 6.6).
          * LLM answers ``contradicted=False`` -> no Indicator.
          * LLM does not answer within :attr:`_timeout_seconds` -> one Indicator
            carrying the ``NARRATIVE_ANALYSIS_TIMEOUT`` flag (Requirement 6.7).
          * LLM raises -> treated like a timeout / non-result (flagged), never
            crashing the engine.
        """
        try:
            result = await asyncio.wait_for(
                self._llm_client(narrative, cra_evidence),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.info("llm_narrative_timeout", extra={"claim_id": str(claim_id)})
            return [self._timeout_indicator(claim_id)]
        except Exception as exc:  # noqa: BLE001 - LLM failure must not crash engine
            logger.info(
                "llm_narrative_error",
                extra={"claim_id": str(claim_id), "error": repr(exc)},
            )
            return [self._timeout_indicator(claim_id, reason="LLM_ERROR")]

        contradicted = bool(result.get("contradicted", False))
        if not contradicted:
            return []

        evidence = str(result.get("evidence", ""))
        return [self._contradiction_indicator(evidence, claim_id)]

    def _contradiction_indicator(
        self, evidence: str, claim_id: Optional[str]
    ) -> Indicator:
        return Indicator(
            indicator_id=uuid.uuid4(),
            claim_id=_coerce_uuid(claim_id),
            engine=EngineType.HISTORY_INCONSISTENCY,
            severity=Severity.CRITICAL,
            algorithm="llm_narrative_contradiction",
            title="ROE separation narrative contradicted by continued employment",
            explanation_en=(
                "The LLM Service classified the ROE reason-for-separation "
                "narrative as contradicted by CRA records showing continued "
                "employment for the same SIN at the same employer during the "
                "claimed separation period."
            ),
            explanation_fr=(
                "Le service LLM a classe le motif de cessation d'emploi du "
                "releve d'emploi comme contredit par les dossiers de l'ARC "
                "indiquant un emploi continu pour le meme NAS chez le meme "
                "employeur pendant la periode de cessation declaree."
            ),
            payload={
                "contradicted": True,
                "evidence": evidence,
            },
            produced_at=datetime.now(timezone.utc),
        )

    def _timeout_indicator(
        self, claim_id: Optional[str], *, reason: str = NARRATIVE_ANALYSIS_TIMEOUT
    ) -> Indicator:
        return Indicator(
            indicator_id=uuid.uuid4(),
            claim_id=_coerce_uuid(claim_id),
            engine=EngineType.HISTORY_INCONSISTENCY,
            severity=Severity.UNRESOLVED,
            algorithm="llm_narrative_contradiction",
            title="Narrative contradiction analysis unavailable",
            explanation_en=(
                "The LLM Service did not return a narrative classification "
                "within the 10-second budget; the engine proceeded without the "
                "narrative result."
            ),
            explanation_fr=(
                "Le service LLM n'a pas retourne de classification narrative "
                "dans le delai de 10 secondes; le moteur a poursuivi sans le "
                "resultat narratif."
            ),
            payload={"flag": NARRATIVE_ANALYSIS_TIMEOUT, "reason": reason},
            produced_at=datetime.now(timezone.utc),
        )


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
    "LLMClient",
    "NARRATIVE_ANALYSIS_TIMEOUT",
    "DEFAULT_LLM_TIMEOUT_SECONDS",
    "NarrativeContradictionChecker",
]
