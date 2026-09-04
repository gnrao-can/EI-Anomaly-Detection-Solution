"""
services/repetitive-pattern-engine/llm_client.py
================================================
Async LLM Service client for the Repetitive Pattern Engine (task 11.2).

This module owns the engine's *outbound* dependency on the LLM Service, which
serves narrative similarity over ``POST /llm/similarity`` (request
``{"narrative_a": ..., "narrative_b": ...}`` -> response ``{"similarity": float}``;
see ``services/llm-service/main.py``).

The engine compares reason-for-separation narratives across a claimant's claims
and, per Requirement 3.5, treats a cosine similarity above the configured
threshold (0.92) as verbatim-equivalent and produces a MEDIUM Indicator carrying
the similarity score. Per Requirement 3.6, if the LLM Service does not answer
within 5 seconds (or otherwise fails), the engine records a
``NARRATIVE_ANALYSIS_TIMEOUT`` flag in an Indicator and proceeds without the
narrative result.

The HTTP transport is injectable. :class:`LLMServiceClient` accepts an optional
async ``transport`` callable so tests can supply a fake without any live
network. In production the default transport uses ``httpx.AsyncClient`` with the
configured 5-second timeout.

Requirements: 3.5, 3.6
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Optional, Sequence

from shared.models import EngineType, Indicator, Severity

logger = logging.getLogger("repetitive-pattern-engine.llm_client")

#: Async transport signature: given the similarity URL, a JSON request body and
#: a timeout in seconds, return the parsed JSON response body (a dict).
#: Implementations should raise on transport / HTTP errors; timeouts are
#: enforced by the client via ``asyncio.wait_for`` so a slow transport still
#: yields the NARRATIVE_ANALYSIS_TIMEOUT path.
Transport = Callable[[str, dict[str, Any], float], Awaitable[dict[str, Any]]]

# Flag recorded in the Indicator payload when the LLM Service does not answer in
# time or otherwise fails (Requirement 3.6).
NARRATIVE_ANALYSIS_TIMEOUT = "NARRATIVE_ANALYSIS_TIMEOUT"

DEFAULT_SIMILARITY_THRESHOLD = 0.92
DEFAULT_TIMEOUT_SECONDS = 5.0


class LLMServiceUnavailable(Exception):
    """Raised internally when the LLM Service cannot be reached / errors out."""


@dataclass(frozen=True)
class NarrativePair:
    """A pair of narratives to compare, with the identifiers they came from."""

    text_a: str
    text_b: str
    claim_id_a: Optional[str] = None
    claim_id_b: Optional[str] = None


def _coerce_claim_uuid(claim_id: Optional[str]) -> uuid.UUID:
    if isinstance(claim_id, uuid.UUID):
        return claim_id
    if claim_id:
        try:
            return uuid.UUID(str(claim_id))
        except (ValueError, AttributeError, TypeError):
            return uuid.uuid5(uuid.NAMESPACE_OID, str(claim_id))
    return uuid.uuid4()


class LLMServiceClient:
    """Async client that scores narrative similarity via the LLM Service.

    The transport is injectable for testing. When no transport is supplied a
    default ``httpx.AsyncClient``-backed transport is built lazily using the
    configured timeout.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        transport: Optional[Transport] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._similarity_threshold = similarity_threshold
        self._transport = transport or _build_default_transport()

    @property
    def similarity_url(self) -> str:
        return f"{self._base_url}/llm/similarity"

    async def score_similarity(self, narrative_a: str, narrative_b: str) -> float:
        """Return the cosine similarity of two narratives in ``[0, 1]``.

        Enforces the configured wall-clock timeout via ``asyncio.wait_for``. On
        timeout or any transport / HTTP / parse failure, raises
        :class:`LLMServiceUnavailable` so the caller can record a
        NARRATIVE_ANALYSIS_TIMEOUT flag (Requirement 3.6).
        """
        body = {"narrative_a": narrative_a, "narrative_b": narrative_b}
        try:
            response = await asyncio.wait_for(
                self._transport(self.similarity_url, body, self._timeout_seconds),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            logger.info("llm_similarity_timeout", extra={"reason": "wait_for_timeout"})
            raise LLMServiceUnavailable("llm similarity timed out") from exc
        except LLMServiceUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - any failure => unavailable
            logger.info("llm_similarity_error", extra={"reason": repr(exc)})
            raise LLMServiceUnavailable(str(exc)) from exc

        if not isinstance(response, dict) or "similarity" not in response:
            raise LLMServiceUnavailable("malformed llm similarity response")
        try:
            score = float(response["similarity"])
        except (TypeError, ValueError) as exc:
            raise LLMServiceUnavailable("non-numeric similarity") from exc
        return score

    async def check_narratives(
        self,
        pairs: Sequence[NarrativePair],
        *,
        claim_id: Optional[str] = None,
    ) -> list[Indicator]:
        """Score each narrative pair and build the resulting Indicators.

        For every pair whose similarity exceeds the configured threshold a
        MEDIUM verbatim-equivalence Indicator is produced carrying the cosine
        score (Req 3.5). If ANY pair's scoring times out or fails, a single
        NARRATIVE_ANALYSIS_TIMEOUT Indicator is appended and the remaining pairs
        are still attempted (Req 3.6: record the flag and proceed).

        Returns an empty list when ``pairs`` is empty (nothing to analyse).
        """
        indicators: list[Indicator] = []
        timed_out = False

        for pair in pairs:
            try:
                score = await self.score_similarity(pair.text_a, pair.text_b)
            except LLMServiceUnavailable:
                timed_out = True
                continue

            if score > self._similarity_threshold:
                indicators.append(
                    self._verbatim_indicator(pair, score, claim_id=claim_id)
                )

        if timed_out:
            indicators.append(self._timeout_indicator(claim_id=claim_id))

        return indicators

    # ------------------------------------------------------------------
    # Indicator builders
    # ------------------------------------------------------------------

    def _verbatim_indicator(
        self, pair: NarrativePair, score: float, *, claim_id: Optional[str]
    ) -> Indicator:
        return Indicator(
            indicator_id=uuid.uuid4(),
            claim_id=_coerce_claim_uuid(claim_id),
            engine=EngineType.REPETITIVE_PATTERN,
            severity=Severity.MEDIUM,
            algorithm="narrative_similarity",
            title=(
                f"Verbatim-equivalent separation narratives "
                f"(cosine {score:.3f})"
            )[:100],
            explanation_en=(
                f"Two reason-for-separation narratives from the same claimant "
                f"scored a cosine similarity of {score:.3f}, above the "
                f"{self._similarity_threshold:.2f} verbatim-equivalence "
                f"threshold — the narratives are effectively identical."
            )[:500],
            explanation_fr=(
                f"Deux motifs de cessation d'emploi du meme prestataire ont "
                f"obtenu une similarite cosinus de {score:.3f}, au-dessus du "
                f"seuil d'equivalence textuelle de "
                f"{self._similarity_threshold:.2f} — les recits sont "
                f"pratiquement identiques."
            )[:500],
            payload={
                "cosine_similarity": score,
                "threshold": self._similarity_threshold,
                "claim_id_a": pair.claim_id_a,
                "claim_id_b": pair.claim_id_b,
            },
            produced_at=datetime.now(timezone.utc),
        )

    def _timeout_indicator(self, *, claim_id: Optional[str]) -> Indicator:
        return Indicator(
            indicator_id=uuid.uuid4(),
            claim_id=_coerce_claim_uuid(claim_id),
            engine=EngineType.REPETITIVE_PATTERN,
            severity=Severity.UNRESOLVED,
            algorithm="narrative_similarity",
            title="Narrative similarity analysis timed out",
            explanation_en=(
                "The LLM Service did not return a narrative-similarity "
                "classification within the allotted time, so narrative analysis "
                "was skipped for this claim; other repetitive-pattern checks "
                "were still applied."
            )[:500],
            explanation_fr=(
                "Le service LLM n'a pas renvoye de classification de similarite "
                "narrative dans le delai imparti; l'analyse narrative a donc ete "
                "ignoree pour cette demande, mais les autres verifications de "
                "schemas repetitifs ont ete appliquees."
            )[:500],
            payload={"flag": NARRATIVE_ANALYSIS_TIMEOUT},
            produced_at=datetime.now(timezone.utc),
        )


def build_narrative_pairs(
    narratives: Iterable[tuple[Optional[str], str]],
) -> list[NarrativePair]:
    """Build the set of unique unordered narrative pairs to compare.

    Accepts an iterable of ``(claim_id, narrative_text)`` tuples. Blank /
    whitespace-only narratives are dropped. Returns every unordered pair of the
    remaining narratives (the design computes *pairwise* cosine similarity).
    """
    items = [
        (cid, text)
        for cid, text in narratives
        if text and str(text).strip()
    ]
    pairs: list[NarrativePair] = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            cid_a, text_a = items[i]
            cid_b, text_b = items[j]
            pairs.append(
                NarrativePair(
                    text_a=text_a,
                    text_b=text_b,
                    claim_id_a=cid_a,
                    claim_id_b=cid_b,
                )
            )
    return pairs


def _build_default_transport() -> Transport:
    """Build the default ``httpx.AsyncClient``-backed async transport.

    The returned coroutine issues a POST with the given per-request timeout and
    returns the parsed JSON body. Any transport / HTTP-status error is raised as
    :class:`LLMServiceUnavailable` so the client maps it to the timeout path.
    """

    async def _transport(
        url: str, body: dict[str, Any], timeout_seconds: float
    ) -> dict[str, Any]:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - httpx is a pinned dep
            raise LLMServiceUnavailable("httpx unavailable") from exc

        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as http:
                response = await http.post(url, json=body)
                response.raise_for_status()
                return response.json()
        except Exception as exc:  # noqa: BLE001 - map all failures to unavailable
            raise LLMServiceUnavailable(str(exc)) from exc

    return _transport


__all__ = [
    "Transport",
    "NarrativePair",
    "LLMServiceClient",
    "LLMServiceUnavailable",
    "NARRATIVE_ANALYSIS_TIMEOUT",
    "DEFAULT_SIMILARITY_THRESHOLD",
    "DEFAULT_TIMEOUT_SECONDS",
    "build_narrative_pairs",
]
