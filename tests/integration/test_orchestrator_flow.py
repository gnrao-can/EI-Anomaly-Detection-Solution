"""
tests/integration/test_orchestrator_flow.py
============================================
Integration tests for the Detection Pipeline Orchestrator end-to-end event flow
(task 6.4).

The orchestrator is composed of four cooperating modules built by tasks 6.1–6.3:

  * ``correlator.py``      — pairs ``claim.received`` + ``roe.received`` (6.1)
  * ``engine_dispatch.py`` — dispatches all six engines concurrently (6.2)
  * ``scoring.py``         — calls the Ensemble Scorer within budget (6.3)
  * ``publisher.py``       — publishes ``claim.scored`` / ``claim.escalated`` (6.3)

These tests genuinely exercise that full chain
(correlator -> engine_dispatch -> scoring -> publisher) with in-memory fakes and
NO external services, so they run in any environment:

  * a fake pairing store (``InMemoryPairingStore`` from the correlator)
  * a fake engine caller (records concurrency; can simulate per-engine timeouts)
  * a fake scorer caller (returns a canned Ensemble Scorer response)
  * a fake Kafka producer (captures published events)
  * a fake ClickHouse audit sink (captures audit rows)

Two execution paths are provided, per the task's guidance:

  1. **testcontainers Kafka** — preferred when the ``testcontainers`` package and
     a Docker/Podman daemon are available. The import is guarded and the whole
     class is skipped with a clear reason when the dependency or daemon is
     absent (as in the CI sandbox). It is NOT required for the suite to pass.
  2. **in-process** — always runs. It wires the real orchestrator modules
     together through their public seams so the full flow is verified without a
     live broker.

Coverage of the task's requirement bullets:

  * Happy path: paired events -> correlator pairs -> all six engines called
    CONCURRENTLY -> scoring -> ``claim.scored`` published. (Req 2.1, 2.2)
  * Two engine timeouts: PARTIAL_RESULT Indicators for the two, >= 3 complete,
    scoring still completes, ``claim.scored`` published. (Req 2.3)
  * Four engine timeouts: < 3 complete -> INDETERMINATE decision, payment
    halted, published. (Req 2.4, 2.5)
  * INVESTIGATE path: scorer returns INVESTIGATE -> both ``claim.scored`` and
    ``claim.escalated`` published.

Requirements: 2.1, 2.3, 2.4, 2.5
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from uuid import uuid4

import pytest

# ---------------------------------------------------------------------------
# Load the hyphenated pipeline-orchestrator service modules collision-free.
# ---------------------------------------------------------------------------
#
# ``services/pipeline-orchestrator`` is not importable as a package, and its
# modules import their siblings by bare name. We use the shared
# ``load_service_module`` helper (tests/conftest.py) so these loads never collide
# with other services' bare ``config`` / ``client`` / ``main`` modules when the
# whole suite runs in one process.

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from tests.conftest import load_service_module  # noqa: E402

_SERVICE = "pipeline-orchestrator"

_correlator = load_service_module(_SERVICE, "correlator", siblings=["correlator"])
_engine_dispatch = load_service_module(
    _SERVICE, "engine_dispatch", siblings=["engine_dispatch"]
)
_scoring = load_service_module(_SERVICE, "scoring", siblings=["scoring"])
_publisher = load_service_module(_SERVICE, "publisher", siblings=["scoring", "publisher"])
_consumer = load_service_module(
    _SERVICE, "consumer", siblings=["correlator", "consumer"]
)

EventCorrelator = _correlator.EventCorrelator
InMemoryPairingStore = _correlator.InMemoryPairingStore
CLAIM_RECEIVED = _correlator.CLAIM_RECEIVED
ROE_RECEIVED = _correlator.ROE_RECEIVED

EngineDispatcher = _engine_dispatch.EngineDispatcher
ALL_ENGINES = _engine_dispatch.ALL_ENGINES
MIN_ENGINES_REQUIRED = _engine_dispatch.MIN_ENGINES_REQUIRED
PARTIAL_RESULT_ALGORITHM = _engine_dispatch.PARTIAL_RESULT_ALGORITHM

ScoringClient = _scoring.ScoringClient

ResultPublisher = _publisher.ResultPublisher
CLAIM_SCORED_TOPIC = _publisher.CLAIM_SCORED_TOPIC
CLAIM_ESCALATED_TOPIC = _publisher.CLAIM_ESCALATED_TOPIC
ADAPTER_AUDIT_TABLE = _publisher.ADAPTER_AUDIT_TABLE

OrchestratorConsumer = _consumer.OrchestratorConsumer

from shared.models import (  # noqa: E402 - after sys.path/module setup
    EngineType,
    Indicator,
    RiskDecision,
    Severity,
)


# ---------------------------------------------------------------------------
# In-memory fakes wiring the orchestrator together end-to-end
# ---------------------------------------------------------------------------


def _make_indicator(claim_id: str, engine: EngineType) -> Indicator:
    """Build a canonical Indicator attributed to a given engine."""
    return Indicator(
        indicator_id=uuid4(),
        claim_id=uuid4(),
        engine=engine,
        severity=Severity.HIGH,
        algorithm="integration_test",
        title=f"{engine.value} finding",
        explanation_en="engine finding",
        explanation_fr="constat du moteur",
        payload={"engine": engine.value},
        produced_at=datetime.now(timezone.utc),
    )


class ConcurrentEngineCaller:
    """Fake engine caller that records concurrency and can time engines out.

    Concurrency is asserted with an ``asyncio.Barrier``-style check: every engine
    that is *expected to complete* enters the caller and waits until all such
    engines have arrived before any returns. If the dispatcher were sequential,
    the second engine could never arrive while the first is blocked and the
    barrier would deadlock (surfaced as a test timeout). Reaching the barrier
    release therefore proves the six calls overlap in time.

    Engines listed in ``timeout_engines`` sleep past their per-engine budget so
    ``asyncio.wait_for`` in the dispatcher cancels them, producing PARTIAL_RESULT
    Indicators. Timed-out engines do NOT participate in the barrier (they never
    "complete").
    """

    def __init__(
        self,
        *,
        expected_concurrency: int,
        timeout_engines: set[EngineType] | None = None,
    ) -> None:
        self._timeout_engines = timeout_engines or set()
        self._expected = expected_concurrency
        self._arrived = 0
        self._all_arrived = asyncio.Event()
        self.calls: list[EngineType] = []
        self.max_observed_concurrency = 0
        self._in_flight = 0
        self._lock = asyncio.Lock()

    async def __call__(self, engine, url, claim_payload, roe_payload):
        self.calls.append(engine)

        async with self._lock:
            self._in_flight += 1
            self.max_observed_concurrency = max(
                self.max_observed_concurrency, self._in_flight
            )

        try:
            if engine in self._timeout_engines:
                # Sleep well past any per-engine budget so wait_for cancels us.
                await asyncio.sleep(3600)
                return []  # pragma: no cover - never reached

            # Barrier: wait until all completing engines have arrived, proving
            # the dispatcher launched them concurrently rather than serially.
            async with self._lock:
                self._arrived += 1
                if self._arrived >= self._expected:
                    self._all_arrived.set()
            await asyncio.wait_for(self._all_arrived.wait(), timeout=5.0)

            return [_make_indicator(str(claim_payload.get("claim_id")), engine)]
        finally:
            async with self._lock:
                self._in_flight -= 1


class FakeScorerCaller:
    """Fake Ensemble Scorer caller returning a canned score response."""

    def __init__(self, risk_score: int, risk_decision: str) -> None:
        self._risk_score = risk_score
        self._risk_decision = risk_decision
        self.calls: list[dict] = []

    async def __call__(self, body: dict) -> dict:
        self.calls.append(body)
        return {
            "assessment_id": str(uuid4()),
            "risk_score": self._risk_score,
            "risk_decision": self._risk_decision,
            "shap_base_value": 5.0,
            "shap_attributions": [],
            "model_version": "integration_v1",
        }


class FakeKafkaProducer:
    """Captures published (topic, key, value) tuples in order."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes | None, bytes]] = []

    async def send_and_wait(self, topic, value, key=None):
        self.sent.append((topic, key, value))
        return object()

    def topics(self) -> list[str]:
        return [t for (t, _k, _v) in self.sent]

    def event_for(self, topic: str) -> dict:
        for (t, k, v) in self.sent:
            if t == topic:
                return {
                    "key": k.decode() if k else None,
                    "payload": json.loads(v.decode()),
                }
        raise AssertionError(f"no event published for topic {topic}")


class FakeAuditSink:
    """Captures rows inserted into the audit table."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, dict]] = []

    async def insert(self, table: str, row: dict) -> None:
        self.rows.append((table, row))


class Orchestrator:
    """Minimal orchestrator that wires the four modules together end-to-end.

    This is the integration seam under test: it feeds a completed
    :class:`PairResult` from the correlator into the dispatcher, forwards the
    collected Indicators to the scoring client, and publishes the outcome — the
    exact hand-off chain the production orchestrator performs across tasks 6.1–6.3.
    """

    def __init__(self, dispatcher, scoring_client, publisher, audit_sink) -> None:
        self._dispatcher = dispatcher
        self._scoring = scoring_client
        self._publisher = publisher
        self._audit_sink = audit_sink
        self.last_dispatch = None
        self.last_outcome = None

    async def handle_pair(self, pair) -> dict:
        # 6.2: dispatch all six engines concurrently.
        dispatch = await self._dispatcher.dispatch(
            pair.claim_payload, pair.roe_payload
        )
        self.last_dispatch = dispatch

        # 6.3: score within budget (skip scorer if the < 3-engine rule failed).
        outcome = await self._scoring.score(
            dispatch.indicators,
            dispatch.claim_id,
            dispatch.engines_completed,
            dispatch.engines_partial,
            indeterminate=dispatch.indeterminate,
        )
        self.last_outcome = outcome

        # 6.3: publish claim.scored (+ claim.escalated if INVESTIGATE) and audit.
        publish = await self._publisher.publish_scored(dispatch.claim_id, outcome)
        await self._publisher.write_audit(
            claim_id=dispatch.claim_id,
            component="pipeline-orchestrator",
            action="score",
            fields_accessed=["indicators"],
            outcome="SUCCESS" if not outcome.fallback else outcome.error or "FALLBACK",
            duration_ms=outcome.elapsed_ms,
        )
        return {"dispatch": dispatch, "outcome": outcome, "publish": publish}


def _build_orchestrator(
    *,
    engine_caller,
    scorer_caller,
    producer,
    audit_sink,
    fast_timeouts_ms: dict | None = None,
):
    """Assemble a fully-wired in-process orchestrator from the real modules."""
    # Tight per-engine timeouts keep the "timeout" tests fast; completing engines
    # return well within these budgets.
    timeouts = fast_timeouts_ms or {engine: 300 for engine in ALL_ENGINES}
    dispatcher = EngineDispatcher(
        engine_caller,
        endpoints={engine: f"http://fake/{engine.value}" for engine in ALL_ENGINES},
        timeouts_ms=timeouts,
    )
    scoring_client = ScoringClient(scorer_caller, budget_ms=500)
    publisher = ResultPublisher(producer, audit_sink=audit_sink)
    return Orchestrator(dispatcher, scoring_client, publisher, audit_sink)


async def _correlate_pair(claim_id: str):
    """Drive the correlator with both events and return the completed PairResult."""
    store = InMemoryPairingStore()
    correlator = EventCorrelator(store)
    now = datetime.now(timezone.utc).isoformat()
    claim_payload = {"claim_id": claim_id, "received_at": now}
    roe_payload = {"claim_id": claim_id, "received_at": now}

    first = await correlator.on_event(CLAIM_RECEIVED, claim_id, claim_payload)
    assert first is None  # still waiting for the ROE event
    pair = await correlator.on_event(ROE_RECEIVED, claim_id, roe_payload)
    assert pair is not None  # pair completes on the second event
    return pair


# ===========================================================================
# In-process integration path (always runs — no external services)
# ===========================================================================


@pytest.mark.asyncio
async def test_happy_path_all_engines_concurrent_and_scored():
    """Paired events -> all six engines called concurrently -> claim.scored.

    Validates: Requirements 2.1, 2.2
    """
    claim_id = str(uuid4())
    engine_caller = ConcurrentEngineCaller(expected_concurrency=len(ALL_ENGINES))
    scorer = FakeScorerCaller(risk_score=42, risk_decision="FLAG_FOR_REVIEW")
    producer = FakeKafkaProducer()
    audit = FakeAuditSink()
    orch = _build_orchestrator(
        engine_caller=engine_caller,
        scorer_caller=scorer,
        producer=producer,
        audit_sink=audit,
    )

    pair = await _correlate_pair(claim_id)
    result = await orch.handle_pair(pair)

    dispatch = result["dispatch"]
    # All six engines were called ...
    assert set(engine_caller.calls) == set(ALL_ENGINES)
    assert len(engine_caller.calls) == len(ALL_ENGINES)
    # ... and concurrently: the barrier only releases when all six overlap.
    assert engine_caller.max_observed_concurrency == len(ALL_ENGINES)
    assert set(dispatch.engines_completed) == set(ALL_ENGINES)
    assert dispatch.indeterminate is False

    # Scoring was invoked with the collected indicators.
    assert len(scorer.calls) == 1
    assert result["outcome"].risk_decision == RiskDecision.FLAG_FOR_REVIEW

    # claim.scored was published (no escalation for FLAG_FOR_REVIEW).
    assert producer.topics() == [CLAIM_SCORED_TOPIC]
    scored = producer.event_for(CLAIM_SCORED_TOPIC)
    assert scored["key"] == dispatch.claim_id
    assert scored["payload"]["risk_decision"] == "FLAG_FOR_REVIEW"

    # An audit row was written to the fake ClickHouse sink.
    assert len(audit.rows) == 1
    assert audit.rows[0][0] == ADAPTER_AUDIT_TABLE


@pytest.mark.asyncio
async def test_two_engine_timeouts_partial_results_but_scoring_completes():
    """Two engines time out -> PARTIAL_RESULT for both, >= 3 complete, scored.

    Validates: Requirements 2.3
    """
    claim_id = str(uuid4())
    timed_out = {EngineType.FRAUD_RING, EngineType.DOCUMENT_FORGERY}
    completing = len(ALL_ENGINES) - len(timed_out)
    engine_caller = ConcurrentEngineCaller(
        expected_concurrency=completing, timeout_engines=timed_out
    )
    scorer = FakeScorerCaller(risk_score=55, risk_decision="FLAG_FOR_REVIEW")
    producer = FakeKafkaProducer()
    audit = FakeAuditSink()
    # Small per-engine budgets so the two timing-out engines trip quickly.
    orch = _build_orchestrator(
        engine_caller=engine_caller,
        scorer_caller=scorer,
        producer=producer,
        audit_sink=audit,
        fast_timeouts_ms={engine: 80 for engine in ALL_ENGINES},
    )

    pair = await _correlate_pair(claim_id)
    result = await orch.handle_pair(pair)
    dispatch = result["dispatch"]

    # Four engines completed (>= 3 minimum); two are partial.
    assert len(dispatch.engines_completed) == completing
    assert set(dispatch.engines_partial) == timed_out
    assert dispatch.indeterminate is False

    # A PARTIAL_RESULT Indicator exists for each timed-out engine.
    partial_engines = {
        ind.payload.get("engine")
        for ind in dispatch.indicators
        if getattr(ind, "algorithm", "") == PARTIAL_RESULT_ALGORITHM
    }
    assert partial_engines == {e.value for e in timed_out}

    # Scoring still completed and claim.scored was published.
    assert len(scorer.calls) == 1
    assert result["outcome"].risk_decision == RiskDecision.FLAG_FOR_REVIEW
    assert producer.topics() == [CLAIM_SCORED_TOPIC]


@pytest.mark.asyncio
async def test_four_engine_timeouts_indeterminate_and_payment_halted():
    """Four engines time out -> < 3 complete -> INDETERMINATE, payment halted.

    Validates: Requirements 2.4, 2.5
    """
    claim_id = str(uuid4())
    timed_out = {
        EngineType.FRAUD_RING,
        EngineType.DOCUMENT_FORGERY,
        EngineType.GEOGRAPHIC_ANOMALY,
        EngineType.REGIONAL_ANALYSIS,
    }
    completing = len(ALL_ENGINES) - len(timed_out)  # 2 < MIN_ENGINES_REQUIRED
    assert completing < MIN_ENGINES_REQUIRED
    engine_caller = ConcurrentEngineCaller(
        expected_concurrency=completing, timeout_engines=timed_out
    )
    # Scorer would return INVESTIGATE, but it must NOT be called on the
    # INDETERMINATE path.
    scorer = FakeScorerCaller(risk_score=90, risk_decision="INVESTIGATE")
    producer = FakeKafkaProducer()
    audit = FakeAuditSink()
    orch = _build_orchestrator(
        engine_caller=engine_caller,
        scorer_caller=scorer,
        producer=producer,
        audit_sink=audit,
        fast_timeouts_ms={engine: 80 for engine in ALL_ENGINES},
    )

    pair = await _correlate_pair(claim_id)
    result = await orch.handle_pair(pair)
    dispatch = result["dispatch"]

    # Fewer than three engines completed -> INDETERMINATE + payment halted.
    assert len(dispatch.engines_completed) == completing
    assert dispatch.indeterminate is True
    assert dispatch.risk_decision == RiskDecision.INDETERMINATE
    assert dispatch.payment_halted is True

    # The scorer was short-circuited (never called) on the INDETERMINATE path.
    assert scorer.calls == []
    assert result["outcome"].risk_decision == RiskDecision.INDETERMINATE
    assert result["outcome"].fallback is True

    # The INDETERMINATE decision is still published as claim.scored.
    assert CLAIM_SCORED_TOPIC in producer.topics()
    scored = producer.event_for(CLAIM_SCORED_TOPIC)
    assert scored["payload"]["risk_decision"] == "INDETERMINATE"
    # INDETERMINATE is not an escalation.
    assert CLAIM_ESCALATED_TOPIC not in producer.topics()


@pytest.mark.asyncio
async def test_investigate_path_publishes_scored_and_escalated():
    """Scorer returns INVESTIGATE -> both claim.scored and claim.escalated."""
    claim_id = str(uuid4())
    engine_caller = ConcurrentEngineCaller(expected_concurrency=len(ALL_ENGINES))
    scorer = FakeScorerCaller(risk_score=88, risk_decision="INVESTIGATE")
    producer = FakeKafkaProducer()
    audit = FakeAuditSink()
    orch = _build_orchestrator(
        engine_caller=engine_caller,
        scorer_caller=scorer,
        producer=producer,
        audit_sink=audit,
    )

    pair = await _correlate_pair(claim_id)
    result = await orch.handle_pair(pair)

    assert result["outcome"].risk_decision == RiskDecision.INVESTIGATE
    assert result["publish"].scored_published is True
    assert result["publish"].escalated_published is True
    # Both events published, keyed by the same claim_id (same-partition order).
    assert producer.topics() == [CLAIM_SCORED_TOPIC, CLAIM_ESCALATED_TOPIC]
    scored = producer.event_for(CLAIM_SCORED_TOPIC)
    escalated = producer.event_for(CLAIM_ESCALATED_TOPIC)
    assert scored["key"] == escalated["key"] == result["dispatch"].claim_id
    assert escalated["payload"]["risk_decision"] == "INVESTIGATE"


@pytest.mark.asyncio
async def test_consumer_correlates_paired_events_and_triggers_dispatch():
    """The Kafka consumer seam pairs two envelopes and triggers dispatch once.

    This exercises the ``consumer.py`` correlation/dispatch hook end-to-end
    without a live broker, feeding raw event envelopes through
    ``handle_message`` exactly as the aiokafka loop would.

    Validates: Requirements 2.1
    """
    claim_id = str(uuid4())
    store = InMemoryPairingStore()
    correlator = EventCorrelator(store)

    dispatched: list = []

    async def dispatch_callback(pair):
        dispatched.append(pair)

    consumer = OrchestratorConsumer(correlator, dispatch_callback)

    now = datetime.now(timezone.utc).isoformat()
    claim_env = {
        "event_type": CLAIM_RECEIVED,
        "claim_id": claim_id,
        "payload": {"claim_id": claim_id, "received_at": now},
    }
    roe_env = {
        "event_type": ROE_RECEIVED,
        "claim_id": claim_id,
        "payload": {"claim_id": claim_id, "received_at": now},
    }

    first = await consumer.handle_message(claim_env)
    assert first is None  # still waiting
    assert dispatched == []

    second = await consumer.handle_message(roe_env)
    assert second is not None  # pair completed
    assert len(dispatched) == 1
    assert dispatched[0].claim_id == claim_id


# ===========================================================================
# testcontainers Kafka path (preferred when available; skipped in the sandbox)
# ===========================================================================


def _testcontainers_kafka_available() -> tuple[bool, str]:
    """Report whether a real testcontainers Kafka path can run here.

    Requires both the ``testcontainers`` package AND a reachable container
    daemon (Docker/Podman). Neither is present in the CI sandbox, so this
    guard returns a clear skip reason rather than failing.
    """
    try:
        import testcontainers  # noqa: F401
        from testcontainers.kafka import KafkaContainer  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - ImportError or submodule missing
        return False, f"testcontainers not installed ({exc})"

    # A daemon socket must exist for the container to start.
    docker_host = os.environ.get("DOCKER_HOST")
    default_sock = "/var/run/docker.sock"
    if not docker_host and not os.path.exists(default_sock):
        return False, "no container daemon (Docker/Podman) available"
    return True, ""


_TC_AVAILABLE, _TC_SKIP_REASON = _testcontainers_kafka_available()


@pytest.mark.integration
@pytest.mark.skipif(not _TC_AVAILABLE, reason=_TC_SKIP_REASON or "testcontainers unavailable")
class TestOrchestratorFlowWithKafkaContainer:
    """End-to-end flow over a real Kafka broker started via testcontainers.

    Preferred when a container daemon is available. In the sandbox this entire
    class is skipped with a clear reason; the in-process tests above provide the
    equivalent coverage without external services.
    """

    @pytest.mark.asyncio
    async def test_paired_events_over_real_kafka_drive_dispatch(self):
        from testcontainers.kafka import KafkaContainer  # local import (guarded)

        with KafkaContainer() as kafka:
            bootstrap = kafka.get_bootstrap_server()

            from aiokafka import AIOKafkaProducer

            claim_id = str(uuid4())
            store = InMemoryPairingStore()
            correlator = EventCorrelator(store)

            dispatched: list = []

            async def dispatch_callback(pair):
                dispatched.append(pair)

            consumer = OrchestratorConsumer(
                correlator,
                dispatch_callback,
                bootstrap_servers=bootstrap,
                group_id="itest-orchestrator",
                degrade_gracefully=False,
            )
            started = await consumer.start()
            assert started is True

            producer = AIOKafkaProducer(bootstrap_servers=bootstrap)
            await producer.start()
            try:
                now = datetime.now(timezone.utc).isoformat()
                for event_type in (CLAIM_RECEIVED, ROE_RECEIVED):
                    envelope = {
                        "event_type": event_type,
                        "claim_id": claim_id,
                        "payload": {"claim_id": claim_id, "received_at": now},
                    }
                    await producer.send_and_wait(
                        event_type,
                        value=json.dumps(envelope).encode("utf-8"),
                        key=claim_id.encode("utf-8"),
                    )

                # Give the consumer loop time to pair both events.
                for _ in range(50):
                    if dispatched:
                        break
                    await asyncio.sleep(0.1)
                assert len(dispatched) == 1
                assert dispatched[0].claim_id == claim_id
            finally:
                await producer.stop()
                await consumer.stop()
