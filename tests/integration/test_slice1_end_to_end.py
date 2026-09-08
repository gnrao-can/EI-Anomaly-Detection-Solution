"""
tests/integration/test_slice1_end_to_end.py
============================================
Slice 1 — the thin end-to-end thread over REAL transports, asserting the REAL
behaviour of the current implementation (not a manufactured happy path).

    claim.received + roe.received (real Kafka)
        -> Orchestrator correlator pairs them
        -> EngineDispatcher POSTs /engine/analyze over real HTTP to the ONE
           implemented engine (Repetitive Pattern), with real shared.auth
           Bearer/JWKS validation; the other five engines are not implemented as
           services yet, so their calls fail
        -> fewer than three engines complete (1 < MIN_ENGINES_REQUIRED = 3)
        -> the pipeline assigns INDETERMINATE, halts payment, and does NOT call
           the Ensemble Scorer (design + Req 2.5)
        -> ResultPublisher publishes claim.scored to real Kafka
        -> the test consumes claim.scored and asserts risk_decision=INDETERMINATE.

Why INDETERMINATE is the correct assertion (grounding, not a workaround)
------------------------------------------------------------------------
Requirement 2.1/2.3: the pipeline runs ALL SIX engines concurrently. Requirement
2.5: "IF fewer than three Detection_Engines produce a result within their
allocated time budgets, THEN the Detection_Pipeline SHALL assign a Risk_Decision
of INDETERMINATE, halt payment processing, and queue the claim for manual
review." Only the Repetitive Pattern Engine exists as a service today, so exactly
one engine completes and the requirement-faithful outcome is INDETERMINATE. We
assert that real outcome. We do NOT point all six engine URLs at the one engine
to fake completion — that would violate Req 2.3/2.5 and the regeneration rule
against reshaping wiring to force a green.

As the other engines are implemented (Slice 3+), this test evolves to assert the
determinate scored path; until then, INDETERMINATE is the honest contract.

Topology (Option 1)
-------------------
  * Kafka — real broker via testcontainers (skipped if no container runtime).
  * Repetitive Pattern Engine — real FastAPI app on 127.0.0.1:<ephemeral>
    (LoopbackService), validating a real GCKey Bearer against the test JWKS from
    the ``itest_service_auth`` fixture.
  * The five not-yet-implemented engines — pointed at a CLOSED loopback port so
    their HTTP calls fail fast (connection refused) and become PARTIAL_RESULT,
    exactly as a down engine would in production.
  * Orchestrator correlator/dispatch/scoring/publisher — the real modules, wired
    as consumer.py wires them, driven by real Kafka events.
  * No PostgreSQL/ClickHouse: the engine returns a valid DATA_UNAVAILABLE
    Indicator; audit sink is None. Neither is needed for this thread.

Requirements exercised: 2.1, 2.3, 2.5, 2.7, 19.7.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from tests.conftest import load_service_module  # noqa: E402
from tests.integration.conftest import (  # noqa: E402
    CONTAINER_RUNTIME_AVAILABLE,
    CONTAINER_RUNTIME_SKIP_REASON,
)
from tests.integration.loopback_service import LoopbackService  # noqa: E402

pytestmark = pytest.mark.skipif(
    not CONTAINER_RUNTIME_AVAILABLE,
    reason=CONTAINER_RUNTIME_SKIP_REASON or "container runtime unavailable",
)

_ORCH = "pipeline-orchestrator"


def _closed_loopback_port() -> int:
    """Return a 127.0.0.1 port with nothing listening (connections refused).

    Used for the five not-yet-implemented engines so their HTTP calls fail fast
    with a connection error (→ PARTIAL_RESULT), the same way a down engine would.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # Socket is closed on exit; the port is now free (nothing listening).
    return port


def _load_orchestrator_modules():
    """Load the orchestrator modules collision-free (hyphenated service dir)."""
    correlator = load_service_module(_ORCH, "correlator", siblings=["correlator"])
    engine_dispatch = load_service_module(
        _ORCH, "engine_dispatch", siblings=["engine_dispatch"]
    )
    scoring = load_service_module(_ORCH, "scoring", siblings=["scoring"])
    publisher = load_service_module(
        _ORCH, "publisher", siblings=["scoring", "publisher"]
    )
    consumer = load_service_module(
        _ORCH, "consumer", siblings=["correlator", "consumer"]
    )
    return correlator, engine_dispatch, scoring, publisher, consumer


def _start_repetitive_pattern_engine(jwks_url: str) -> LoopbackService:
    """Start the ONE implemented engine on loopback HTTP (no DB, real auth)."""
    os.environ["GCKEY_JWKS_URL"] = jwks_url
    os.environ.pop("EI_ADS_PG_DSN", None)
    os.environ.pop("EI_ADS_CLICKHOUSE_HOST", None)

    engine_main = load_service_module(
        "repetitive-pattern-engine",
        "main",
        siblings=[
            "config",
            "patterns",
            "repository",
            "llm_client",
            "kafka_producer",
            "hours_clustering",
            "main",
        ],
    )
    return LoopbackService(engine_main.app, health_path="/healthz").start()


class _ScorerSpy:
    """A scorer caller that records whether it was invoked.

    On the INDETERMINATE (<3 engines) path the orchestrator MUST NOT call the
    Ensemble Scorer (design error table: "< 3 engines complete → assign
    INDETERMINATE; halt payment"). We inject this spy so the test can assert the
    scorer was never called, proving the short-circuit rather than assuming it.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, body: dict) -> dict:  # pragma: no cover - must not run
        self.calls += 1
        raise AssertionError(
            "Ensemble Scorer was called on the INDETERMINATE path; Req 2.5 "
            "requires the scorer to be skipped when < 3 engines complete."
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_under_three_engines_yields_indeterminate_over_real_transports(
    itest_service_auth,
):
    """One engine reachable → <3 complete → INDETERMINATE claim.scored published."""
    from testcontainers.kafka import KafkaContainer  # guarded import
    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

    correlator_mod, engine_dispatch_mod, scoring_mod, publisher_mod, consumer_mod = (
        _load_orchestrator_modules()
    )

    EventCorrelator = correlator_mod.EventCorrelator
    InMemoryPairingStore = correlator_mod.InMemoryPairingStore
    CLAIM_RECEIVED = correlator_mod.CLAIM_RECEIVED
    ROE_RECEIVED = correlator_mod.ROE_RECEIVED

    EngineDispatcher = engine_dispatch_mod.EngineDispatcher
    EngineType = engine_dispatch_mod.EngineType
    ALL_ENGINES = engine_dispatch_mod.ALL_ENGINES
    ENGINE_ANALYZE_PATH = engine_dispatch_mod.ENGINE_ANALYZE_PATH
    MIN_ENGINES_REQUIRED = engine_dispatch_mod.MIN_ENGINES_REQUIRED

    ScoringClient = scoring_mod.ScoringClient

    ResultPublisher = publisher_mod.ResultPublisher
    AIOKafkaProducerAdapter = publisher_mod.AIOKafkaProducerAdapter
    CLAIM_SCORED_TOPIC = publisher_mod.CLAIM_SCORED_TOPIC
    CLAIM_ESCALATED_TOPIC = publisher_mod.CLAIM_ESCALATED_TOPIC

    OrchestratorConsumer = consumer_mod.OrchestratorConsumer

    from shared.models import RiskDecision

    # Sanity: this test's premise is that exactly one engine is reachable, which
    # is below the minimum. If the minimum ever changes, revisit the assertion.
    assert MIN_ENGINES_REQUIRED >= 2

    jwks_url = itest_service_auth.auth_server_url
    token = itest_service_auth.mint_token(roles=["INVESTIGATOR"])
    os.environ["ORCHESTRATOR_SERVICE_TOKEN"] = token

    engine_service = None
    kafka = None
    orchestrator_consumer = None
    producer = None
    scored_consumer = None
    kafka_adapter = None
    scorer_spy = _ScorerSpy()
    try:
        # ── The ONE implemented engine on real loopback HTTP. ───────────────
        engine_service = _start_repetitive_pattern_engine(jwks_url)

        # Warm the shared JWKS cache once (first validation fetches the JWKS).
        warm = httpx.post(
            f"{engine_service.base_url}{ENGINE_ANALYZE_PATH}",
            json={"sin_hash": "warmup"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
        assert warm.status_code == 200, warm.text

        # Endpoints: the real engine for REPETITIVE_PATTERN; a CLOSED loopback
        # port for the five not-yet-implemented engines (connection refused →
        # PARTIAL_RESULT). This reflects the true system state today.
        dead_port = _closed_loopback_port()
        endpoints = {}
        for engine in ALL_ENGINES:
            if engine == EngineType.REPETITIVE_PATTERN:
                endpoints[engine] = f"{engine_service.base_url}{ENGINE_ANALYZE_PATH}"
            else:
                endpoints[engine] = (
                    f"http://127.0.0.1:{dead_port}{ENGINE_ANALYZE_PATH}"
                )
        # Generous budgets so the ONE real engine completes well within time and
        # the failures are genuine connection refusals, not timeouts.
        timeouts = {engine: 5000 for engine in ALL_ENGINES}

        # ── Real Kafka broker. ─────────────────────────────────────────────
        kafka = KafkaContainer()
        kafka.start()
        bootstrap = kafka.get_bootstrap_server()

        # ── Wire the real orchestrator chain (as consumer.py does). ─────────
        dispatcher = EngineDispatcher(endpoints=endpoints, timeouts_ms=timeouts)
        # Inject the scorer spy so we can prove it is never called on this path.
        scoring_client = ScoringClient(scorer_spy, budget_ms=5000)
        kafka_adapter = AIOKafkaProducerAdapter(bootstrap_servers=bootstrap)
        result_publisher = ResultPublisher(kafka_adapter, audit_sink=None)

        captured = {}

        async def dispatch_callback(pair):
            dispatch = await dispatcher.dispatch(pair.claim_payload, pair.roe_payload)
            captured["dispatch"] = dispatch
            outcome = await scoring_client.score(
                dispatch.indicators,
                dispatch.claim_id,
                dispatch.engines_completed,
                dispatch.engines_partial,
                indeterminate=dispatch.indeterminate,
            )
            captured["outcome"] = outcome
            await result_publisher.publish_scored(dispatch.claim_id, outcome)

        store = InMemoryPairingStore()
        correlator = EventCorrelator(store)
        orchestrator_consumer = OrchestratorConsumer(
            correlator,
            dispatch_callback,
            bootstrap_servers=bootstrap,
            group_id="itest-slice1-orchestrator",
            degrade_gracefully=False,
        )
        assert await orchestrator_consumer.start() is True

        scored_consumer = AIOKafkaConsumer(
            CLAIM_SCORED_TOPIC,
            CLAIM_ESCALATED_TOPIC,
            bootstrap_servers=bootstrap,
            group_id="itest-slice1-assert",
            auto_offset_reset="earliest",
            enable_auto_commit=True,
        )
        await scored_consumer.start()

        # ── Produce the paired intake events. ───────────────────────────────
        claim_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        claim_payload = {
            "claim_id": claim_id,
            "sin_hash": "a" * 64,
            "employer_bn": "123456789RP0001",
            "received_at": now,
        }
        roe_payload = {"claim_id": claim_id, "received_at": now}

        producer = AIOKafkaProducer(bootstrap_servers=bootstrap)
        await producer.start()
        for event_type, payload in (
            (CLAIM_RECEIVED, claim_payload),
            (ROE_RECEIVED, roe_payload),
        ):
            envelope = {
                "event_type": event_type,
                "claim_id": claim_id,
                "payload": payload,
            }
            await producer.send_and_wait(
                event_type,
                value=json.dumps(envelope).encode("utf-8"),
                key=claim_id.encode("utf-8"),
            )

        # ── Await the claim.scored event (bounded). ─────────────────────────
        scored = None
        topics_seen = []
        deadline = asyncio.get_event_loop().time() + 30.0
        while asyncio.get_event_loop().time() < deadline:
            try:
                msg = await asyncio.wait_for(scored_consumer.getone(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            event = json.loads(msg.value.decode("utf-8"))
            if event.get("claim_id") == claim_id:
                topics_seen.append(msg.topic)
                if msg.topic == CLAIM_SCORED_TOPIC:
                    scored = event
                    break

        # ── Assertions: the REAL <3-engine INDETERMINATE contract (Req 2.5). ─
        assert scored is not None, "no claim.scored event observed for the claim"
        assert scored["claim_id"] == claim_id
        assert scored["risk_decision"] == RiskDecision.INDETERMINATE.value, (
            f"expected INDETERMINATE with only 1 engine reachable, "
            f"got {scored['risk_decision']}"
        )

        # The dispatch genuinely completed fewer than the minimum engines.
        dispatch = captured["dispatch"]
        assert len(dispatch.engines_completed) == 1
        assert dispatch.engines_completed == [EngineType.REPETITIVE_PATTERN]
        assert len(dispatch.engines_partial) == len(ALL_ENGINES) - 1
        assert dispatch.completed_count < MIN_ENGINES_REQUIRED
        assert dispatch.indeterminate is True
        assert dispatch.payment_halted is True

        # The Ensemble Scorer was NOT called (short-circuited on INDETERMINATE).
        assert scorer_spy.calls == 0
        assert captured["outcome"].fallback is True

        # INDETERMINATE is not an escalation.
        assert CLAIM_ESCALATED_TOPIC not in topics_seen
    finally:
        if producer is not None:
            await producer.stop()
        if scored_consumer is not None:
            await scored_consumer.stop()
        if orchestrator_consumer is not None:
            await orchestrator_consumer.stop()
        if kafka_adapter is not None:
            await kafka_adapter.stop()
        if kafka is not None:
            kafka.stop()
        if engine_service is not None:
            engine_service.stop()
        os.environ.pop("ORCHESTRATOR_SERVICE_TOKEN", None)
