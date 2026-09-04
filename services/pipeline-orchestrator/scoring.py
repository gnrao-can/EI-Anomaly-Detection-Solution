"""
services/pipeline-orchestrator/scoring.py
==========================================
Ensemble-Scorer client for the Detection Pipeline Orchestrator (task 6.3).

After the orchestrator has dispatched the six detection engines and collected
their Indicators, it must call the Ensemble Scorer service to obtain the final
Risk_Score and Risk_Decision, and it must do so within a tight time budget.

This module implements that call:

  * :class:`ScoringClient` POSTs the collected Indicator list (in the canonical
    ``Indicator.to_dict()`` shape) to the Ensemble Scorer's ``POST /score``
    endpoint via ``httpx.AsyncClient``. The whole call is wrapped in
    ``asyncio.wait_for`` with a 300 ms budget (design "Time Budget Allocation":
    *Ensemble Scorer (post-engines) | 300 ms*).
  * On success it returns a :class:`ScoreOutcome` carrying the risk score, the
    routing decision, the raw assessment dict, and the elapsed time in ms.
  * On timeout or any transport/HTTP error it surfaces a documented fallback —
    an ``INDETERMINATE`` :class:`ScoreOutcome` with ``risk_score = 0`` — so the
    pipeline can still publish a ``claim.scored`` event and never silently drops
    a claim (mirrors the design's "< 3 engines complete -> INDETERMINATE"
    degradation contract for the scoring hop).

Scope / integration boundary (task 6.3)
---------------------------------------
This module deliberately does **not** import from ``engine_dispatch.py`` (owned
by task 6.2). Instead it accepts the *output of engine dispatch* as an abstract
input, :class:`EngineCollectionResult`, documented below. Task 6.2 can adapt its
own result object to this shape (or pass a compatible dict) without either
module depending on the other's concrete types.

Expected engine-dispatch output shape
--------------------------------------
``EngineCollectionResult`` (or any object/dict with these attributes/keys):

  * ``indicators``: ``list[Indicator]`` — every Indicator produced by the
    engines that completed (and any PARTIAL_RESULT / DATA_UNAVAILABLE markers
    the dispatch layer chose to include).
  * ``engines_completed``: ``list`` of engine identifiers that finished within
    budget.
  * ``engines_partial``: ``list`` of engine identifiers that timed out / were
    partial.
  * ``indeterminate``: ``bool`` — ``True`` when the dispatch layer already
    determined the < 3-engine minimum was not met. When ``True``, this client
    short-circuits to an ``INDETERMINATE`` outcome without calling the scorer.

Requirements: 2.6
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol

try:
    from shared.models import Indicator, RiskDecision
except ImportError:  # pragma: no cover - service-local fallback
    from models import Indicator, RiskDecision  # type: ignore

logger = logging.getLogger("pipeline-orchestrator.scoring")


# ---------------------------------------------------------------------------
# Local constants (defined here to avoid coupling to 6.2 / config.py)
# ---------------------------------------------------------------------------

#: Post-engine Ensemble Scorer budget (design Time Budget Allocation: 300 ms).
SCORE_BUDGET_MS: int = 300

#: Default Ensemble Scorer base URL; overridable via env for k8s service DNS.
DEFAULT_SCORER_BASE_URL: str = "http://ensemble-scorer:8007"

#: The scoring endpoint path on the Ensemble Scorer service.
SCORE_PATH: str = "/score"


def _scorer_base_url() -> str:
    """Resolve the Ensemble Scorer base URL from the environment.

    Reads ``ORCHESTRATOR_ENSEMBLE_SCORER_URL`` (falls back to
    ``ENSEMBLE_SCORER_URL``) and defaults to the in-cluster service DNS name.
    """
    return (
        os.environ.get("ORCHESTRATOR_ENSEMBLE_SCORER_URL")
        or os.environ.get("ENSEMBLE_SCORER_URL")
        or DEFAULT_SCORER_BASE_URL
    )


# ---------------------------------------------------------------------------
# Abstract engine-dispatch output (documented input; not imported from 6.2)
# ---------------------------------------------------------------------------


@dataclass
class EngineCollectionResult:
    """The abstract output of engine dispatch consumed by :class:`ScoringClient`.

    Task 6.2 owns the concrete dispatch implementation; this dataclass documents
    the shape :class:`ScoringClient.score` needs. Any object exposing these
    attributes — or a plain dict with these keys — is accepted.

    Attributes
    ----------
    indicators:
        All Indicators produced by the engines.
    engines_completed:
        Engine identifiers that completed within budget.
    engines_partial:
        Engine identifiers that timed out or returned partial results.
    indeterminate:
        ``True`` when the < 3-engine minimum was not met; scoring is skipped and
        an INDETERMINATE outcome is returned directly.
    """

    indicators: list[Indicator] = field(default_factory=list)
    engines_completed: list[Any] = field(default_factory=list)
    engines_partial: list[Any] = field(default_factory=list)
    indeterminate: bool = False


# ---------------------------------------------------------------------------
# Outcome value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoreOutcome:
    """The result of a scoring attempt.

    Attributes
    ----------
    risk_score:
        Integer Risk_Score in [0, 100]. ``0`` on the INDETERMINATE fallback.
    risk_decision:
        The :class:`RiskDecision` (AUTO_APPROVE / FLAG_FOR_REVIEW / INVESTIGATE /
        INDETERMINATE).
    assessment:
        The raw assessment dict returned by the Ensemble Scorer (empty on the
        fallback path).
    elapsed_ms:
        Wall-clock time spent on the scoring call, in milliseconds.
    fallback:
        ``True`` when this outcome came from the timeout/error fallback rather
        than a successful scorer response.
    error:
        A short description of the failure when ``fallback`` is ``True``.
    """

    risk_score: int
    risk_decision: RiskDecision
    assessment: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0
    fallback: bool = False
    error: Optional[str] = None

    @property
    def is_investigate(self) -> bool:
        """``True`` when the decision routes the claim to escalation."""
        return self.risk_decision == RiskDecision.INVESTIGATE


# ---------------------------------------------------------------------------
# Scorer caller abstraction (DI seam for tests)
# ---------------------------------------------------------------------------


class ScorerCaller(Protocol):
    """Callable that performs the HTTP POST to the Ensemble Scorer.

    Given the JSON body ``{"indicators": [...]}`` it returns the parsed response
    dict (matching the Ensemble Scorer ``ScoreResponse`` schema). Implementations
    may raise on transport/HTTP failure; :class:`ScoringClient` treats any
    exception (and timeout) as a signal to fall back to INDETERMINATE.
    """

    async def __call__(self, body: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover - protocol
        ...


class HttpxScorerCaller:
    """Production :class:`ScorerCaller` backed by ``httpx.AsyncClient``.

    A single client is created lazily and reused. ``aclose`` should be called on
    shutdown; :class:`ScoringClient.aclose` forwards to it.
    """

    def __init__(self, base_url: Optional[str] = None) -> None:
        self._base_url = (base_url or _scorer_base_url()).rstrip("/")
        self._client: Any = None

    async def _get_client(self) -> Any:
        if self._client is None:
            import httpx

            # Per-request timeout is enforced by asyncio.wait_for in the client;
            # we still set a generous transport timeout as a safety net.
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(5.0))
        return self._client

    async def __call__(self, body: dict[str, Any]) -> dict[str, Any]:
        client = await self._get_client()
        response = await client.post(f"{self._base_url}{SCORE_PATH}", json=body)
        response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None


# ---------------------------------------------------------------------------
# ScoringClient
# ---------------------------------------------------------------------------


class ScoringClient:
    """Calls the Ensemble Scorer within the 300 ms post-engine budget.

    Parameters
    ----------
    caller:
        The :class:`ScorerCaller` used to perform the POST. Defaults to
        :class:`HttpxScorerCaller` (production). Tests inject a fake caller.
    budget_ms:
        The scoring time budget in milliseconds. Defaults to
        :data:`SCORE_BUDGET_MS` (300 ms).
    """

    def __init__(
        self,
        caller: Optional[ScorerCaller] = None,
        *,
        budget_ms: int = SCORE_BUDGET_MS,
    ) -> None:
        self._caller: ScorerCaller = caller or HttpxScorerCaller()
        self._budget_ms = budget_ms

    @property
    def budget_ms(self) -> int:
        """The scoring time budget in milliseconds (300 by default)."""
        return self._budget_ms

    async def score(
        self,
        indicators: list[Indicator],
        claim_id: str,
        engines_completed: Optional[list[Any]] = None,
        engines_partial: Optional[list[Any]] = None,
        *,
        indeterminate: bool = False,
    ) -> ScoreOutcome:
        """Score a claim via the Ensemble Scorer within the 300 ms budget.

        Parameters
        ----------
        indicators:
            The Indicators collected from the detection engines.
        claim_id:
            The claim identifier (used only for logging / fallback outcome).
        engines_completed / engines_partial:
            Engine completion metadata from the dispatch layer (currently passed
            through for logging and future use; the Ensemble Scorer derives its
            own completion features from the Indicators).
        indeterminate:
            When ``True``, the < 3-engine minimum was not met upstream; the
            scorer call is skipped and an INDETERMINATE outcome is returned.

        Returns
        -------
        ScoreOutcome
            The scoring result, or the INDETERMINATE fallback on timeout/error.
        """
        engines_completed = engines_completed or []
        engines_partial = engines_partial or []

        # Upstream already determined the minimum-engine rule failed: do not call
        # the scorer; return INDETERMINATE directly (payment halted downstream).
        if indeterminate:
            logger.info(
                "claim %s marked INDETERMINATE upstream (completed=%d, partial=%d); "
                "skipping scorer call",
                claim_id,
                len(engines_completed),
                len(engines_partial),
            )
            return ScoreOutcome(
                risk_score=0,
                risk_decision=RiskDecision.INDETERMINATE,
                assessment={},
                elapsed_ms=0.0,
                fallback=True,
                error="indeterminate_upstream",
            )

        body = {"indicators": [ind.to_dict() for ind in indicators]}
        budget_seconds = self._budget_ms / 1000.0
        start = time.monotonic()
        try:
            raw = await asyncio.wait_for(self._caller(body), timeout=budget_seconds)
        except asyncio.TimeoutError:
            elapsed_ms = (time.monotonic() - start) * 1000.0
            logger.warning(
                "scoring timed out for claim %s after %.1f ms (budget %d ms); "
                "falling back to INDETERMINATE",
                claim_id,
                elapsed_ms,
                self._budget_ms,
            )
            return self._fallback(elapsed_ms, "timeout")
        except Exception as exc:  # noqa: BLE001 - any transport/HTTP error
            elapsed_ms = (time.monotonic() - start) * 1000.0
            logger.warning(
                "scoring failed for claim %s after %.1f ms: %s; "
                "falling back to INDETERMINATE",
                claim_id,
                elapsed_ms,
                exc,
            )
            return self._fallback(elapsed_ms, f"error:{type(exc).__name__}")

        elapsed_ms = (time.monotonic() - start) * 1000.0
        return self._parse_outcome(raw, elapsed_ms, claim_id)

    def _parse_outcome(
        self, raw: dict[str, Any], elapsed_ms: float, claim_id: str
    ) -> ScoreOutcome:
        """Convert a raw scorer response dict to a :class:`ScoreOutcome`.

        A malformed response (missing/invalid fields) is treated as a failure
        and mapped to the INDETERMINATE fallback so the pipeline can still
        publish.
        """
        try:
            risk_score = int(raw["risk_score"])
            decision = RiskDecision(raw["risk_decision"])
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning(
                "malformed scorer response for claim %s (%s); "
                "falling back to INDETERMINATE",
                claim_id,
                exc,
            )
            return self._fallback(elapsed_ms, "malformed_response")

        if elapsed_ms > self._budget_ms:
            logger.warning(
                "scoring for claim %s completed in %.1f ms, exceeding the "
                "%d ms budget",
                claim_id,
                elapsed_ms,
                self._budget_ms,
            )
        return ScoreOutcome(
            risk_score=risk_score,
            risk_decision=decision,
            assessment=raw,
            elapsed_ms=elapsed_ms,
            fallback=False,
            error=None,
        )

    @staticmethod
    def _fallback(elapsed_ms: float, reason: str) -> ScoreOutcome:
        """Build the documented INDETERMINATE fallback outcome."""
        return ScoreOutcome(
            risk_score=0,
            risk_decision=RiskDecision.INDETERMINATE,
            assessment={},
            elapsed_ms=elapsed_ms,
            fallback=True,
            error=reason,
        )

    async def aclose(self) -> None:
        """Close the underlying HTTP client if it owns one."""
        aclose = getattr(self._caller, "aclose", None)
        if aclose is not None:
            await aclose()


__all__ = [
    "SCORE_BUDGET_MS",
    "DEFAULT_SCORER_BASE_URL",
    "SCORE_PATH",
    "EngineCollectionResult",
    "ScoreOutcome",
    "ScorerCaller",
    "HttpxScorerCaller",
    "ScoringClient",
]
