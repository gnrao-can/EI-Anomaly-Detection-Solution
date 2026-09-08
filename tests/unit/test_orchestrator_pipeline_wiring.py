"""
tests/unit/test_orchestrator_pipeline_wiring.py
===============================================
Guard the orchestrator SERVICE-level dispatch wiring (Req 2.2-2.7).

The deployed orchestrator (`services/pipeline-orchestrator/main.py`) must run the
REAL chain on each correlated pair: EngineDispatcher -> ScoringClient ->
ResultPublisher. An earlier version wired a logging-only ``_default_dispatch``
placeholder that paired events but produced no decision — the container looked
healthy while doing nothing. These tests lock in the real wiring so that
regression cannot recur:

  * ``OrchestratorPipeline.dispatch`` forwards the paired payloads to the
    dispatcher, feeds the collected Indicators (+ engine completion metadata,
    + the indeterminate flag) to the scorer, and publishes the outcome.
  * The service module no longer exposes the ``_default_dispatch`` placeholder.

These are pure unit tests over the service seam with fakes — no broker, no HTTP.
The full behaviour over real transports is covered by
``tests/integration/test_slice1_end_to_end.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.conftest import load_service_module

_SERVICE = "pipeline-orchestrator"
_SIBLINGS = [
    "config",
    "correlator",
    "consumer",
    "engine_dispatch",
    "scoring",
    "publisher",
    "main",
]

_main = load_service_module(_SERVICE, "main", siblings=_SIBLINGS)
OrchestratorPipeline = _main.OrchestratorPipeline


class _FakeDispatcher:
    """Captures the payloads passed to dispatch and returns a canned result."""

    def __init__(self, result):
        self._result = result
        self.calls = []

    async def dispatch(self, claim_payload, roe_payload):
        self.calls.append((claim_payload, roe_payload))
        return self._result


class _FakeScoring:
    """Captures scoring args and returns a canned outcome."""

    def __init__(self, outcome):
        self._outcome = outcome
        self.calls = []

    async def score(
        self, indicators, claim_id, engines_completed, engines_partial, *, indeterminate
    ):
        self.calls.append(
            SimpleNamespace(
                indicators=indicators,
                claim_id=claim_id,
                engines_completed=engines_completed,
                engines_partial=engines_partial,
                indeterminate=indeterminate,
            )
        )
        return self._outcome


class _FakePublisher:
    def __init__(self):
        self.published = []

    async def publish_scored(self, claim_id, outcome):
        self.published.append((claim_id, outcome))


def _pair(claim_id="c-1"):
    return SimpleNamespace(
        claim_id=claim_id,
        claim_payload={"claim_id": claim_id, "sin_hash": "h"},
        roe_payload={"claim_id": claim_id},
    )


@pytest.mark.asyncio
async def test_pipeline_runs_dispatch_then_score_then_publish():
    """The service pipeline drives the full real chain for a paired claim."""
    dispatch_result = SimpleNamespace(
        claim_id="c-1",
        indicators=["ind-1", "ind-2"],
        engines_completed=["REPETITIVE_PATTERN", "GEOGRAPHIC_ANOMALY", "FRAUD_RING"],
        engines_partial=[],
        indeterminate=False,
    )
    outcome = SimpleNamespace(risk_decision=SimpleNamespace(value="FLAG_FOR_REVIEW"))

    dispatcher = _FakeDispatcher(dispatch_result)
    scoring = _FakeScoring(outcome)
    publisher = _FakePublisher()

    pipeline = OrchestratorPipeline(
        dispatcher=dispatcher, scoring_client=scoring, publisher=publisher
    )
    await pipeline.dispatch(_pair("c-1"))

    # Dispatcher received the paired payloads (not a {claim,roe} envelope).
    assert dispatcher.calls == [({"claim_id": "c-1", "sin_hash": "h"}, {"claim_id": "c-1"})]
    # Scorer received the collected indicators + completion metadata.
    assert len(scoring.calls) == 1
    call = scoring.calls[0]
    assert call.indicators == ["ind-1", "ind-2"]
    assert call.claim_id == "c-1"
    assert call.indeterminate is False
    # The outcome was published, keyed by the claim id.
    assert publisher.published == [("c-1", outcome)]


@pytest.mark.asyncio
async def test_pipeline_forwards_indeterminate_flag():
    """When dispatch marks INDETERMINATE (<3 engines), it reaches the scorer."""
    dispatch_result = SimpleNamespace(
        claim_id="c-2",
        indicators=["only-1"],
        engines_completed=["REPETITIVE_PATTERN"],
        engines_partial=["FRAUD_RING", "GEOGRAPHIC_ANOMALY"],
        indeterminate=True,
    )
    outcome = SimpleNamespace(risk_decision=SimpleNamespace(value="INDETERMINATE"))
    scoring = _FakeScoring(outcome)
    publisher = _FakePublisher()

    pipeline = OrchestratorPipeline(
        dispatcher=_FakeDispatcher(dispatch_result),
        scoring_client=scoring,
        publisher=publisher,
    )
    await pipeline.dispatch(_pair("c-2"))

    assert scoring.calls[0].indeterminate is True
    assert publisher.published[0][0] == "c-2"


def test_service_has_no_placeholder_dispatch():
    """The logging-only placeholder must be gone (regression guard)."""
    assert not hasattr(_main, "_default_dispatch"), (
        "the orchestrator service must wire a real OrchestratorPipeline, not the "
        "removed _default_dispatch placeholder"
    )
    assert hasattr(_main, "OrchestratorPipeline")
