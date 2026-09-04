"""
tests/unit/test_orchestrator_scoring.py
=======================================
Unit tests for the Detection Pipeline Orchestrator scoring call and final event
publication (task 6.3).

Covers the three requirement bullets for 6.3:

  * POST the Indicator list to the Ensemble Scorer within the 300 ms budget,
    with a documented INDETERMINATE fallback on timeout/error (Req 2.6).
  * Publish ``claim.scored`` and, when INVESTIGATE, ``claim.escalated`` — both
    keyed by ``claim_id`` (Req 2.7).
  * Write a complete audit-trail entry to ClickHouse via the injected sink
    (Req 2.8).

All tests use dependency-injected fakes: no network, no live Kafka, and no live
ClickHouse. The service modules import their siblings by bare name, so the
service directory is placed on ``sys.path`` before loading them (same pattern as
``test_correlator.py``).

Requirements: 2.6, 2.7, 2.8
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from uuid import uuid4

import pytest

# ---------------------------------------------------------------------------
# Load the pipeline-orchestrator service modules from the service directory.
# ---------------------------------------------------------------------------

_SERVICE_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "services", "pipeline-orchestrator"
    )
)


def _load_module(name: str, filename: str):
    if os.path.isdir(_SERVICE_DIR) and _SERVICE_DIR not in sys.path:
        sys.path.insert(0, _SERVICE_DIR)
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_SERVICE_DIR, filename)
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_scoring = _load_module("orch_scoring", "scoring.py")
_publisher = _load_module("orch_publisher", "publisher.py")

ScoringClient = _scoring.ScoringClient
ScoreOutcome = _scoring.ScoreOutcome
SCORE_BUDGET_MS = _scoring.SCORE_BUDGET_MS

ResultPublisher = _publisher.ResultPublisher
KafkaEvent = _publisher.KafkaEvent
CLAIM_SCORED_TOPIC = _publisher.CLAIM_SCORED_TOPIC
CLAIM_ESCALATED_TOPIC = _publisher.CLAIM_ESCALATED_TOPIC
ADAPTER_AUDIT_TABLE = _publisher.ADAPTER_AUDIT_TABLE

from shared.models import (  # noqa: E402 - after sys.path/module setup
    EngineType,
    Indicator,
    RiskDecision,
    Severity,
)


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


def _make_indicator(claim_id: str) -> Indicator:
    return Indicator(
        indicator_id=uuid4(),
        claim_id=claim_id if isinstance(claim_id, type(uuid4())) else uuid4(),
        engine=EngineType.REPETITIVE_PATTERN,
        severity=Severity.HIGH,
        algorithm="sequential_pattern_mining",
        title="Repeated same-employer claims",
        explanation_en="Three claims from the same employer in 36 months.",
        explanation_fr="Trois demandes du meme employeur en 36 mois.",
        payload={"count": 3},
        produced_at=datetime.now(timezone.utc),
    )


class _FakeScorerCaller:
    """A fake ScorerCaller returning a canned response dict."""

    def __init__(self, response: dict, *, delay: float = 0.0) -> None:
        self._response = response
        self._delay = delay
        self.calls: list[dict] = []

    async def __call__(self, body: dict) -> dict:
        self.calls.append(body)
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._response


class _SlowScorerCaller:
    """A fake ScorerCaller that never returns in time (triggers timeout)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, body: dict) -> dict:
        self.calls.append(body)
        await asyncio.sleep(3600)
        return {}


class _FakeProducer:
    """Captures published (topic, key, value) tuples."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes, bytes]] = []

    async def send_and_wait(self, topic, value, key=None):
        self.sent.append((topic, key, value))
        return object()

    # -- test conveniences --------------------------------------------
    def topics(self) -> list[str]:
        return [t for (t, _k, _v) in self.sent]

    def event_for(self, topic: str) -> dict:
        for (t, k, v) in self.sent:
            if t == topic:
                return {"key": k.decode() if k else None, "payload": json.loads(v.decode())}
        raise AssertionError(f"no event published for topic {topic}")


class _FakeAuditSink:
    """Captures rows inserted into the audit table."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, dict]] = []

    async def insert(self, table: str, row: dict) -> None:
        self.rows.append((table, row))


# ---------------------------------------------------------------------------
# 1: successful scoring within budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_scoring_returns_outcome():
    claim_id = str(uuid4())
    response = {
        "assessment_id": str(uuid4()),
        "claim_id": claim_id,
        "risk_score": 42,
        "risk_decision": "FLAG_FOR_REVIEW",
        "shap_base_value": 10.0,
        "shap_attributions": [],
        "model_version": "xgboost_v1",
    }
    caller = _FakeScorerCaller(response)
    client = ScoringClient(caller)

    indicators = [_make_indicator(claim_id)]
    outcome = await client.score(indicators, claim_id, ["ENG1", "ENG2", "ENG3"], [])

    assert outcome.risk_score == 42
    assert outcome.risk_decision == RiskDecision.FLAG_FOR_REVIEW
    assert outcome.fallback is False
    assert outcome.assessment["model_version"] == "xgboost_v1"
    # The Indicator list was POSTed in canonical to_dict() shape.
    assert len(caller.calls) == 1
    assert caller.calls[0]["indicators"][0]["engine"] == EngineType.REPETITIVE_PATTERN.value


# ---------------------------------------------------------------------------
# 2: scoring timeout -> documented INDETERMINATE fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scoring_timeout_falls_back_to_indeterminate():
    claim_id = str(uuid4())
    # Very small budget so the slow caller trips the wait_for timeout quickly.
    client = ScoringClient(_SlowScorerCaller(), budget_ms=20)

    outcome = await client.score([_make_indicator(claim_id)], claim_id, [], [])

    assert outcome.fallback is True
    assert outcome.risk_decision == RiskDecision.INDETERMINATE
    assert outcome.risk_score == 0
    assert outcome.error == "timeout"


@pytest.mark.asyncio
async def test_scoring_error_falls_back_to_indeterminate():
    claim_id = str(uuid4())

    class _ErrorCaller:
        async def __call__(self, body):
            raise RuntimeError("connection refused")

    client = ScoringClient(_ErrorCaller())
    outcome = await client.score([_make_indicator(claim_id)], claim_id, [], [])

    assert outcome.fallback is True
    assert outcome.risk_decision == RiskDecision.INDETERMINATE
    assert outcome.error is not None and outcome.error.startswith("error:")


@pytest.mark.asyncio
async def test_indeterminate_upstream_skips_scorer_call():
    claim_id = str(uuid4())
    caller = _FakeScorerCaller({"risk_score": 90, "risk_decision": "INVESTIGATE"})
    client = ScoringClient(caller)

    outcome = await client.score(
        [_make_indicator(claim_id)], claim_id, ["ENG1"], ["ENG2", "ENG3"],
        indeterminate=True,
    )

    assert outcome.risk_decision == RiskDecision.INDETERMINATE
    assert outcome.fallback is True
    assert caller.calls == []  # scorer never called


# ---------------------------------------------------------------------------
# 3: non-INVESTIGATE decisions publish only claim.scored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [RiskDecision.AUTO_APPROVE, RiskDecision.FLAG_FOR_REVIEW])
async def test_non_investigate_publishes_only_scored(decision):
    claim_id = str(uuid4())
    producer = _FakeProducer()
    publisher = ResultPublisher(producer)

    outcome = ScoreOutcome(risk_score=15, risk_decision=decision)
    result = await publisher.publish_scored(claim_id, outcome)

    assert result.scored_published is True
    assert result.escalated_published is False
    assert producer.topics() == [CLAIM_SCORED_TOPIC]

    scored = producer.event_for(CLAIM_SCORED_TOPIC)
    assert scored["key"] == claim_id
    assert scored["payload"]["claim_id"] == claim_id
    assert scored["payload"]["risk_decision"] == decision.value


# ---------------------------------------------------------------------------
# 4: INVESTIGATE publishes both claim.scored and claim.escalated (keyed by id)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_investigate_publishes_scored_and_escalated():
    claim_id = str(uuid4())
    producer = _FakeProducer()
    publisher = ResultPublisher(producer)

    outcome = ScoreOutcome(risk_score=88, risk_decision=RiskDecision.INVESTIGATE)
    result = await publisher.publish_scored(claim_id, outcome)

    assert result.scored_published is True
    assert result.escalated_published is True
    assert producer.topics() == [CLAIM_SCORED_TOPIC, CLAIM_ESCALATED_TOPIC]

    # Both events keyed by the same claim_id (same-partition ordering).
    scored = producer.event_for(CLAIM_SCORED_TOPIC)
    escalated = producer.event_for(CLAIM_ESCALATED_TOPIC)
    assert scored["key"] == claim_id
    assert escalated["key"] == claim_id
    assert escalated["payload"]["risk_decision"] == RiskDecision.INVESTIGATE.value
    assert result.within_budget is True


# ---------------------------------------------------------------------------
# 5: audit-trail entry written to the fake ClickHouse sink
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_entry_written_to_sink():
    claim_id = str(uuid4())
    sink = _FakeAuditSink()
    publisher = ResultPublisher(_FakeProducer(), audit_sink=sink)

    row = await publisher.write_audit(
        claim_id=claim_id,
        component="ensemble-scorer",
        action="score",
        fields_accessed=["indicators"],
        outcome="SUCCESS",
        duration_ms=123.4,
    )

    assert len(sink.rows) == 1
    table, written = sink.rows[0]
    assert table == ADAPTER_AUDIT_TABLE
    assert written["claim_id"] == claim_id
    assert written["component"] == "ensemble-scorer"
    assert written["action"] == "score"
    assert written["fields_accessed"] == ["indicators"]
    assert written["outcome"] == "SUCCESS"
    assert written["duration_ms"] == pytest.approx(123.4)
    assert len(written["sin_hash"]) == 64  # padded, no raw SIN
    # Returned row matches what was written.
    assert written == row


@pytest.mark.asyncio
async def test_audit_no_sink_is_noop_but_returns_row():
    claim_id = str(uuid4())
    publisher = ResultPublisher(_FakeProducer(), audit_sink=None)

    row = await publisher.write_audit(
        claim_id=claim_id,
        component="cra-adapter",
        action="t4_fetch",
        fields_accessed=["employment_income"],
        outcome="SUCCESS",
        duration_ms=50.0,
    )
    assert row["component"] == "cra-adapter"


@pytest.mark.asyncio
async def test_audit_failure_is_swallowed():
    claim_id = str(uuid4())

    class _FailingSink:
        async def insert(self, table, row):
            raise RuntimeError("clickhouse down")

    publisher = ResultPublisher(_FakeProducer(), audit_sink=_FailingSink())
    # Should not raise despite the sink failing.
    row = await publisher.write_audit(
        claim_id=claim_id,
        component="geonames-adapter",
        action="resolve",
        fields_accessed=["postal_code"],
        outcome="ERROR",
        duration_ms=10.0,
    )
    assert row["outcome"] == "ERROR"
