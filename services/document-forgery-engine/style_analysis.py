"""
services/document-forgery-engine/style_analysis.py
==================================================
Async LLM termination-narrative style analysis for the Document Forgery Engine
(task 15.3).

Design step 7 of the Document Forgery Engine (design "5. Document Forgery
Engine"):

    7. LLM style analysis (async):
       - If employer has >= 5 authenticated ROEs:
           -> Pass termination narrative against corpus to Llama 3.1
           -> "Is this narrative statistically inconsistent with prior style?
               (YES/NO + score)"
           -> If YES -> MEDIUM Indicator (Requirement 7.8)
       - Else -> skip (Requirement 7.9)

The LLM Service is injectable: :class:`StyleAnalysisChecker` takes an async
callable (the "client") so tests supply a fake — no live LLM/network is used.
The client returns a dict shaped ``{"inconsistent": bool, "score": float}``
(a similarity/confidence score in ``[0, 1]``).

This check runs *outside* the engine's 700 ms wall-clock budget: it is awaited
asynchronously and never blocks the core forensic checks or Indicator
publication (Requirement 7.10 measures the core pipeline). A slow or errored LLM
must not crash the engine, so timeouts / errors yield no style Indicator.

Requirements: 7.8, 7.9
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from shared.models import EngineType, Indicator, Severity

logger = logging.getLogger("document-forgery-engine.style")

#: Async LLM client signature: given the ROE termination narrative and the
#: employer's prior-ROE corpus (list of narrative strings), return a
#: classification dict ``{"inconsistent": bool, "score": float}``.
LLMStyleClient = Callable[[str, list[str]], Awaitable[dict[str, Any]]]

#: Minimum number of prior authenticated ROEs required from the same employer
#: before the LLM style check runs (Requirement 7.8 corpus size / 7.9 skip).
MIN_AUTHENTICATED_ROES_FOR_STYLE_CHECK = 5

#: Default LLM timeout in seconds. The style check is async and off the 700 ms
#: critical path; this bounds how long the engine waits for the optional result.
DEFAULT_LLM_TIMEOUT_SECONDS: float = 5.0


class StyleAnalysisChecker:
    """Run the async LLM style-inconsistency check with a bounded timeout.

    Parameters
    ----------
    llm_client:
        An async callable ``(narrative, corpus) -> {"inconsistent": bool,
        "score": float}``. Tests inject a fake; production wires the LLM Service
        HTTP client here.
    timeout_seconds:
        Maximum time to wait for the LLM before proceeding without a result.
    min_corpus_size:
        Minimum prior authenticated ROE count required to run the check
        (Requirement 7.8 / 7.9). Defaults to
        :data:`MIN_AUTHENTICATED_ROES_FOR_STYLE_CHECK`.
    """

    def __init__(
        self,
        llm_client: LLMStyleClient,
        *,
        timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS,
        min_corpus_size: int = MIN_AUTHENTICATED_ROES_FOR_STYLE_CHECK,
    ) -> None:
        self._llm_client = llm_client
        self._timeout_seconds = timeout_seconds
        self._min_corpus_size = min_corpus_size

    async def check(
        self,
        narrative: str,
        prior_narratives: list[str],
        *,
        prior_authenticated_roe_count: Optional[int] = None,
        claim_id: Optional[str] = None,
    ) -> list[Indicator]:
        """Classify the narrative's style and return Indicators.

        Behaviour:
          * Fewer than :attr:`_min_corpus_size` prior authenticated ROEs -> the
            check is SKIPPED and no Indicator is produced (Requirement 7.9).
          * LLM answers ``inconsistent=True`` within the timeout -> one MEDIUM
            Indicator carrying the score (Requirement 7.8).
          * LLM answers ``inconsistent=False`` -> no Indicator.
          * LLM times out or raises -> no Indicator (the engine proceeds; the
            style signal is best-effort and off the critical path).

        The corpus size is taken from ``prior_authenticated_roe_count`` when
        supplied, otherwise from ``len(prior_narratives)``.
        """
        corpus_size = (
            prior_authenticated_roe_count
            if prior_authenticated_roe_count is not None
            else len(prior_narratives)
        )
        if corpus_size < self._min_corpus_size:
            # Requirement 7.9: skip the LLM style analysis entirely.
            return []

        try:
            result = await asyncio.wait_for(
                self._llm_client(narrative, list(prior_narratives)),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.info("llm_style_timeout", extra={"claim_id": str(claim_id)})
            return []
        except Exception as exc:  # noqa: BLE001 - LLM failure must not crash engine
            logger.info(
                "llm_style_error",
                extra={"claim_id": str(claim_id), "error": repr(exc)},
            )
            return []

        inconsistent = bool(result.get("inconsistent", False))
        if not inconsistent:
            return []

        score = result.get("score")
        return [self._style_indicator(score, corpus_size, claim_id)]

    def _style_indicator(
        self, score: Any, corpus_size: int, claim_id: Optional[str]
    ) -> Indicator:
        try:
            score_value: Optional[float] = float(score) if score is not None else None
        except (TypeError, ValueError):
            score_value = None
        return Indicator(
            indicator_id=uuid.uuid4(),
            claim_id=_coerce_uuid(claim_id),
            engine=EngineType.DOCUMENT_FORGERY,
            severity=Severity.MEDIUM,
            algorithm="llm_style_analysis",
            title="ROE termination narrative inconsistent with employer style",
            explanation_en=(
                "The LLM Service classified the ROE termination narrative as "
                "statistically inconsistent with the employer's historical ROE "
                f"communication style, based on a corpus of {corpus_size} prior "
                "authenticated ROEs from the same employer."
            ),
            explanation_fr=(
                "Le service LLM a classe le recit de cessation d'emploi du "
                "releve d'emploi comme statistiquement incoherent avec le style "
                "de communication historique des releves d'emploi de "
                f"l'employeur, sur la base d'un corpus de {corpus_size} releves "
                "d'emploi authentifies anterieurs du meme employeur."
            ),
            payload={
                "flag": "STYLE_INCONSISTENT",
                "inconsistent": True,
                "score": score_value,
                "corpus_size": corpus_size,
            },
            produced_at=datetime.now(timezone.utc),
        )


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
    "LLMStyleClient",
    "MIN_AUTHENTICATED_ROES_FOR_STYLE_CHECK",
    "DEFAULT_LLM_TIMEOUT_SECONDS",
    "StyleAnalysisChecker",
]
