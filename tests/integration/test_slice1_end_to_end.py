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


# ===========================================================================
# Slice 3 — three engines reachable → DETERMINATE decision (graduates Slice 1)
# ===========================================================================


def _start_history_inconsistency_engine(jwks_url: str) -> LoopbackService:
    """Start the History Inconsistency engine on loopback HTTP (no CRA adapter)."""
    os.environ["GCKEY_JWKS_URL"] = jwks_url
    engine_main = load_service_module(
        "history-inconsistency-engine",
        "main",
        siblings=[
            "config",
            "client",
            "discrepancy",
            "noc",
            "llm_client",
            "kafka_publisher",
            "analysis",
            "main",
        ],
    )
    # No health_path: this engine's /healthz reports 200 once a CRA client
    # object exists (built in lifespan), which it always is.
    return LoopbackService(engine_main.app, health_path="/healthz").start()


def _start_regional_analysis_engine(jwks_url: str) -> LoopbackService:
    """Start the Regional Analysis engine on loopback HTTP (no adapters)."""
    os.environ["GCKEY_JWKS_URL"] = jwks_url
    engine_main = load_service_module(
        "regional-analysis-engine",
        "main",
        siblings=[
            "config",
            "client",
            "detection",
            "seasonality",
            "forecast",
            "providers",
            "kafka_producer",
            "main",
        ],
    )
    return LoopbackService(engine_main.app, health_path="/healthz").start()


def _start_ensemble_scorer(jwks_url: str) -> LoopbackService:
    """Start the Ensemble Scorer on loopback HTTP (heuristic; no DB/models)."""
    os.environ["GCKEY_JWKS_URL"] = jwks_url
    os.environ.pop("EI_ADS_PG_DSN", None)
    os.environ.pop("EI_ADS_CLICKHOUSE_HOST", None)
    scorer_main = load_service_module(
        "ensemble-scorer", "main", siblings=["scorer", "repository", "main"]
    )
    return LoopbackService(scorer_main.app, health_path="/healthz").start()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_three_engines_yield_determinate_over_real_transports(
    itest_service_auth,
):
    """Three engines reachable → ≥3 complete → scorer runs → DETERMINATE claim.scored.

    This graduates the Slice-1 thread now that engines 2 & 3 exist: with the
    Repetitive Pattern + History Inconsistency + Regional Analysis engines
    reachable (the other three still absent → PARTIAL_RESULT), the ≥3-engine
    minimum is met, so the pipeline runs the Ensemble Scorer and publishes a
    DETERMINATE decision instead of INDETERMINATE (Req 2.2, 2.5).

    No adapters are wired, so all three engines DEGRADE (DATA_UNAVAILABLE /
    UNRESOLVED indicators). The heuristic scorer maps those (severity UNRESOLVED
    = ordinal 0) to a low score → AUTO_APPROVE — a genuine determinate outcome
    proving the ≥3 → scorer → publish path over real transports. Getting a
    FLAG/INVESTIGATE decision additionally requires the mock-data adapters (a
    later Slice 3 step).
    """
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
    HttpxScorerCaller = scoring_mod.HttpxScorerCaller

    ResultPublisher = publisher_mod.ResultPublisher
    AIOKafkaProducerAdapter = publisher_mod.AIOKafkaProducerAdapter
    CLAIM_SCORED_TOPIC = publisher_mod.CLAIM_SCORED_TOPIC

    OrchestratorConsumer = consumer_mod.OrchestratorConsumer

    from shared.models import RiskDecision

    DETERMINATE = {
        RiskDecision.AUTO_APPROVE.value,
        RiskDecision.FLAG_FOR_REVIEW.value,
        RiskDecision.INVESTIGATE.value,
    }

    jwks_url = itest_service_auth.auth_server_url
    token = itest_service_auth.mint_token(roles=["INVESTIGATOR"])
    os.environ["ORCHESTRATOR_SERVICE_TOKEN"] = token

    services: list = []
    kafka = None
    orchestrator_consumer = producer = scored_consumer = kafka_adapter = None
    scoring_client = None
    try:
        # ── Three real engines + the scorer on loopback HTTP. ───────────────
        rpe = _start_repetitive_pattern_engine(jwks_url)
        history = _start_history_inconsistency_engine(jwks_url)
        regional = _start_regional_analysis_engine(jwks_url)
        scorer = _start_ensemble_scorer(jwks_url)
        services = [rpe, history, regional, scorer]

        # Warm the shared JWKS cache once against a real engine.
        warm = httpx.post(
            f"{rpe.base_url}{ENGINE_ANALYZE_PATH}",
            json={"sin_hash": "warmup"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
        assert warm.status_code == 200, warm.text

        # Endpoints: the three implemented engines reachable; the other three at
        # a CLOSED loopback port so they fail fast → PARTIAL_RESULT.
        dead_port = _closed_loopback_port()
        real = {
            EngineType.REPETITIVE_PATTERN: rpe.base_url,
            EngineType.HISTORY_INCONSISTENCY: history.base_url,
            EngineType.REGIONAL_ANALYSIS: regional.base_url,
        }
        endpoints = {}
        for engine in ALL_ENGINES:
            base = real.get(engine, f"http://127.0.0.1:{dead_port}")
            endpoints[engine] = f"{base}{ENGINE_ANALYZE_PATH}"
        timeouts = {engine: 5000 for engine in ALL_ENGINES}

        kafka = KafkaContainer()
        kafka.start()
        bootstrap = kafka.get_bootstrap_server()

        dispatcher = EngineDispatcher(endpoints=endpoints, timeouts_ms=timeouts)
        # REAL scorer call over loopback HTTP (not a spy) — must be invoked now.
        scoring_client = ScoringClient(
            HttpxScorerCaller(base_url=scorer.base_url), budget_ms=5000
        )
        kafka_adapter = AIOKafkaProducerAdapter(bootstrap_servers=bootstrap)
        result_publisher = ResultPublisher(kafka_adapter, audit_sink=None)

        captured: dict = {}

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
            group_id="itest-slice3-orchestrator",
            degrade_gracefully=False,
        )
        assert await orchestrator_consumer.start() is True

        scored_consumer = AIOKafkaConsumer(
            CLAIM_SCORED_TOPIC,
            bootstrap_servers=bootstrap,
            group_id="itest-slice3-assert",
            auto_offset_reset="earliest",
            enable_auto_commit=True,
        )
        await scored_consumer.start()

        # ── Produce paired events with the ENRICHED payloads the engines need. ─
        claim_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        claim_payload = {
            "claim_id": claim_id,
            "sin_hash": "a" * 64,
            "employer_bn": "123456789RP0001",
            "insurable_hours": "800",
            "insurable_earnings": "25000",
            "separation_date": "2024-03-15",
            "claim_start_date": "2024-04-01",
            "economic_region_code": "35",
            "noc_code": "12345",
            "received_at": now,
        }
        # ROE payload carries the employer-declared earnings (roe_earnings source).
        roe_payload = {
            "claim_id": claim_id,
            "sin_hash": "a" * 64,
            "employer_bn": "123456789RP0001",
            "insurable_earnings": "26500",
            "received_at": now,
        }

        producer = AIOKafkaProducer(bootstrap_servers=bootstrap)
        await producer.start()
        for event_type, payload in (
            (CLAIM_RECEIVED, claim_payload),
            (ROE_RECEIVED, roe_payload),
        ):
            envelope = {"event_type": event_type, "claim_id": claim_id, "payload": payload}
            await producer.send_and_wait(
                event_type,
                value=json.dumps(envelope).encode("utf-8"),
                key=claim_id.encode("utf-8"),
            )

        # ── Await the claim.scored event (bounded). ─────────────────────────
        scored = None
        deadline = asyncio.get_event_loop().time() + 30.0
        while asyncio.get_event_loop().time() < deadline:
            try:
                msg = await asyncio.wait_for(scored_consumer.getone(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            event = json.loads(msg.value.decode("utf-8"))
            if event.get("claim_id") == claim_id:
                scored = event
                break

        # ── Assertions: ≥3 engines complete → determinate decision. ─────────
        dispatch = captured["dispatch"]
        assert set(dispatch.engines_completed) == {
            EngineType.REPETITIVE_PATTERN,
            EngineType.HISTORY_INCONSISTENCY,
            EngineType.REGIONAL_ANALYSIS,
        }, dispatch.engines_completed
        assert dispatch.completed_count >= MIN_ENGINES_REQUIRED
        assert dispatch.indeterminate is False
        # The real scorer was called (not short-circuited) and did not fall back.
        assert captured["outcome"].fallback is False
        assert scored is not None, "no claim.scored event observed for the claim"
        assert scored["claim_id"] == claim_id
        assert scored["risk_decision"] in DETERMINATE
        assert scored["risk_decision"] != RiskDecision.INDETERMINATE.value
        assert isinstance(scored["risk_score"], int)
        assert 0 <= scored["risk_score"] <= 100
    finally:
        if producer is not None:
            await producer.stop()
        if scored_consumer is not None:
            await scored_consumer.stop()
        if orchestrator_consumer is not None:
            await orchestrator_consumer.stop()
        if kafka_adapter is not None:
            await kafka_adapter.stop()
        if scoring_client is not None:
            await scoring_client.aclose()
        if kafka is not None:
            kafka.stop()
        for svc in services:
            svc.stop()
        os.environ.pop("ORCHESTRATOR_SERVICE_TOKEN", None)


# ===========================================================================
# Slice 3 — WITH mock-data adapters: the engines find REAL signals, so the
# ≥3-engine determinate path yields a NON-AUTO_APPROVE decision (Req 8.4/8.6).
#
# The Regional Analysis engine, backed by the Job Bank adapter's open-data
# snapshot, sees 245 active postings (> 200 → HIGH, Req 8.4) and a median
# advertised wage of $24.10 that exceeds the claimant's $15.00/h insurable
# earnings by more than 1.15x (→ HIGH, Req 8.6). Two HIGH Indicators from a
# single real engine drive the heuristic scorer's Risk_Score to ≥ 30, so the
# determinate decision is FLAG_FOR_REVIEW / INVESTIGATE — never AUTO_APPROVE,
# never INDETERMINATE.
#
# Every engine→adapter call presents the engine's SERVICE_TOKEN (INVESTIGATOR
# Bearer); the adapters run their REAL shared.auth JWKS validation (Req 19.7).
# Nothing is faked: real Kafka, real HTTP, real auth, real fixtures.
# ===========================================================================


def _reset_service_settings(service_dir_name: str) -> None:
    """Clear a service's cached Settings so it re-reads freshly-set env vars.

    ``load_service_module`` caches modules by a namespaced key, so a config
    module loaded by an earlier test keeps its ``@lru_cache``d Settings. We
    evict that cache before (re)starting the service under new env vars.
    """
    config_mod = load_service_module(service_dir_name, "config", siblings=["config"])
    getter = getattr(config_mod, "get_settings", None)
    if getter is not None and hasattr(getter, "cache_clear"):
        getter.cache_clear()


def _start_cra_adapter(jwks_url: str) -> LoopbackService:
    """Start the CRA adapter (synthetic T4 in dev) on loopback HTTP, auth-gated."""
    os.environ["GCKEY_JWKS_URL"] = jwks_url
    os.environ["APP_ENV"] = "development"
    _reset_service_settings("cra-adapter")
    adapter_main = load_service_module(
        "cra-adapter", "main", siblings=["config", "client", "audit", "main"]
    )
    return LoopbackService(adapter_main.app, health_path="/healthz").start()


def _start_stats_canada_adapter(jwks_url: str) -> LoopbackService:
    """Start the Stats Canada adapter (mock-data LFS) on loopback HTTP, auth-gated."""
    os.environ["GCKEY_JWKS_URL"] = jwks_url
    os.environ["APP_ENV"] = "development"
    os.environ["STATSCAN_USE_MOCK"] = "true"
    _reset_service_settings("stats-canada-adapter")
    adapter_main = load_service_module(
        "stats-canada-adapter", "main", siblings=["config", "client", "main"]
    )
    return LoopbackService(adapter_main.app, health_path="/healthz").start()


def _start_job_bank_adapter(jwks_url: str) -> LoopbackService:
    """Start the Job Bank adapter (open-data snapshot) on loopback HTTP, auth-gated."""
    os.environ["GCKEY_JWKS_URL"] = jwks_url
    os.environ["APP_ENV"] = "development"
    _reset_service_settings("job-bank-adapter")
    adapter_main = load_service_module(
        "job-bank-adapter", "main", siblings=["config", "client", "serialization", "main"]
    )
    return LoopbackService(adapter_main.app, health_path="/healthz").start()


def _start_history_engine_with_adapter(jwks_url: str, cra_url: str) -> LoopbackService:
    """Start History Inconsistency wired to a REAL CRA adapter (forwards token)."""
    os.environ["GCKEY_JWKS_URL"] = jwks_url
    os.environ["CRA_ADAPTER_URL"] = cra_url
    _reset_service_settings("history-inconsistency-engine")
    engine_main = load_service_module(
        "history-inconsistency-engine",
        "main",
        siblings=[
            "config", "client", "discrepancy", "noc",
            "llm_client", "kafka_publisher", "analysis", "main",
        ],
    )
    # The module may be cached from an earlier (no-adapter) test: refresh the
    # app's settings and drop the CRA client so it is rebuilt against the new
    # adapter URL + SERVICE_TOKEN on the next request.
    config_mod = load_service_module(
        "history-inconsistency-engine", "config", siblings=["config"]
    )
    engine_main.app.state.settings = config_mod.get_settings()
    engine_main.app.state.cra_client = None
    return LoopbackService(engine_main.app, health_path="/healthz").start()


def _start_regional_engine_with_adapters(
    jwks_url: str, statscan_url: str, job_bank_url: str
) -> LoopbackService:
    """Start Regional Analysis wired to REAL Stats Canada + Job Bank adapters."""
    os.environ["GCKEY_JWKS_URL"] = jwks_url
    os.environ["STATSCAN_ADAPTER_URL"] = statscan_url
    os.environ["JOB_BANK_ADAPTER_URL"] = job_bank_url
    _reset_service_settings("regional-analysis-engine")
    engine_main = load_service_module(
        "regional-analysis-engine",
        "main",
        siblings=[
            "config", "client", "detection", "seasonality",
            "forecast", "providers", "kafka_producer", "main",
        ],
    )
    config_mod = load_service_module(
        "regional-analysis-engine", "config", siblings=["config"]
    )
    engine_main.app.state.settings = config_mod.get_settings()
    # Drop cached adapter clients so they rebuild against the new URLs + token.
    engine_main.app.state.statscan_client = None
    engine_main.app.state.job_bank_client = None
    return LoopbackService(engine_main.app, health_path="/healthz").start()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_three_engines_with_adapters_yield_flag_over_real_transports(
    itest_service_auth,
):
    """≥3 engines + mock-data adapters → REAL signals → determinate NON-AUTO_APPROVE.

    Backed by the Job Bank open-data snapshot, the Regional Analysis engine
    produces two HIGH Indicators for NOC 63201 / region 59 / 2024-05 (245
    postings > 200, Req 8.4; median wage $24.10 > $15.00×1.15, Req 8.6). With
    ≥3 engines complete the pipeline runs the Ensemble Scorer, whose heuristic
    maps two HIGH Indicators to a Risk_Score ≥ 30 → FLAG_FOR_REVIEW /
    INVESTIGATE (never AUTO_APPROVE, never INDETERMINATE).

    Engines authenticate to the auth-gated adapters with their SERVICE_TOKEN
    (INVESTIGATOR Bearer, Req 19.7); the adapters run real JWKS validation.
    """
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
    HttpxScorerCaller = scoring_mod.HttpxScorerCaller

    ResultPublisher = publisher_mod.ResultPublisher
    AIOKafkaProducerAdapter = publisher_mod.AIOKafkaProducerAdapter
    CLAIM_SCORED_TOPIC = publisher_mod.CLAIM_SCORED_TOPIC

    OrchestratorConsumer = consumer_mod.OrchestratorConsumer

    from shared.models import RiskDecision

    jwks_url = itest_service_auth.auth_server_url
    token = itest_service_auth.mint_token(roles=["INVESTIGATOR"])
    # Both the orchestrator (engine/scorer calls) and the engines (adapter calls)
    # present this INVESTIGATOR service Bearer on internal calls (Req 19.7).
    os.environ["ORCHESTRATOR_SERVICE_TOKEN"] = token
    os.environ["SERVICE_TOKEN"] = token

    services: list = []
    kafka = None
    orchestrator_consumer = producer = scored_consumer = kafka_adapter = None
    scoring_client = None
    try:
        # ── Adapters first (their loopback URLs feed the engine config). ────
        cra = _start_cra_adapter(jwks_url)
        statscan = _start_stats_canada_adapter(jwks_url)
        job_bank = _start_job_bank_adapter(jwks_url)

        # ── Three engines; regional + history wired to the real adapters. ──
        rpe = _start_repetitive_pattern_engine(jwks_url)
        history = _start_history_engine_with_adapter(jwks_url, cra.base_url)
        regional = _start_regional_engine_with_adapters(
            jwks_url, statscan.base_url, job_bank.base_url
        )
        scorer = _start_ensemble_scorer(jwks_url)
        services = [cra, statscan, job_bank, rpe, history, regional, scorer]

        # Warm the shared JWKS cache once against a real engine.
        warm = httpx.post(
            f"{rpe.base_url}{ENGINE_ANALYZE_PATH}",
            json={"sin_hash": "warmup"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
        assert warm.status_code == 200, warm.text

        # Sanity-check the auth-gated adapter path directly: the Job Bank adapter
        # must serve the 245-posting fixture WITH the INVESTIGATOR Bearer and
        # reject the same request WITHOUT it (Req 19.7).
        jb_url = f"{job_bank.base_url}/jobbank/postings/63201/59/2024-05"
        jb_ok = httpx.get(
            jb_url, headers={"Authorization": f"Bearer {token}"}, timeout=5.0
        )
        assert jb_ok.status_code == 200, jb_ok.text
        assert int(jb_ok.json()["active_posting_count"]) == 245, jb_ok.text
        jb_401 = httpx.get(jb_url, timeout=5.0)
        assert jb_401.status_code == 401, "adapter must reject an unauthenticated call"

        # Endpoints: the three implemented engines reachable; the other three at
        # a CLOSED loopback port so they fail fast → PARTIAL_RESULT.
        dead_port = _closed_loopback_port()
        real = {
            EngineType.REPETITIVE_PATTERN: rpe.base_url,
            EngineType.HISTORY_INCONSISTENCY: history.base_url,
            EngineType.REGIONAL_ANALYSIS: regional.base_url,
        }
        endpoints = {}
        for engine in ALL_ENGINES:
            base = real.get(engine, f"http://127.0.0.1:{dead_port}")
            endpoints[engine] = f"{base}{ENGINE_ANALYZE_PATH}"
        timeouts = {engine: 5000 for engine in ALL_ENGINES}

        kafka = KafkaContainer()
        kafka.start()
        bootstrap = kafka.get_bootstrap_server()

        dispatcher = EngineDispatcher(endpoints=endpoints, timeouts_ms=timeouts)
        scoring_client = ScoringClient(
            HttpxScorerCaller(base_url=scorer.base_url), budget_ms=5000
        )
        kafka_adapter = AIOKafkaProducerAdapter(bootstrap_servers=bootstrap)
        result_publisher = ResultPublisher(kafka_adapter, audit_sink=None)

        captured: dict = {}

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
            group_id="itest-slice3-adapters-orchestrator",
            degrade_gracefully=False,
        )
        assert await orchestrator_consumer.start() is True

        scored_consumer = AIOKafkaConsumer(
            CLAIM_SCORED_TOPIC,
            bootstrap_servers=bootstrap,
            group_id="itest-slice3-adapters-assert",
            auto_offset_reset="earliest",
            enable_auto_commit=True,
        )
        await scored_consumer.start()

        # ── Produce paired events with the WITH-SIGNALS payloads. ───────────
        # claim_start_date 2024-06-01 → reference_month derives to 2024-05 (the
        # month the Job Bank + LFS fixtures cover). insurable_earnings/hours =
        # 12000/800 = $15.00/h. region 59 + NOC 63201 select the 245-posting,
        # $24.10-wage fixture that trips Req 8.4 + Req 8.6.
        claim_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        claim_payload = {
            "claim_id": claim_id,
            "sin_hash": "b" * 64,
            "employer_bn": "987654321RP0001",
            "insurable_hours": "800",
            "insurable_earnings": "12000",
            "separation_date": "2024-03-15",
            "claim_start_date": "2024-06-01",
            "economic_region_code": "59",
            "noc_code": "63201",
            "received_at": now,
        }
        roe_payload = {
            "claim_id": claim_id,
            "sin_hash": "b" * 64,
            "employer_bn": "987654321RP0001",
            "insurable_earnings": "12000",
            "received_at": now,
        }

        producer = AIOKafkaProducer(bootstrap_servers=bootstrap)
        await producer.start()
        for event_type, payload in (
            (CLAIM_RECEIVED, claim_payload),
            (ROE_RECEIVED, roe_payload),
        ):
            envelope = {"event_type": event_type, "claim_id": claim_id, "payload": payload}
            await producer.send_and_wait(
                event_type,
                value=json.dumps(envelope).encode("utf-8"),
                key=claim_id.encode("utf-8"),
            )

        # ── Await the claim.scored event (bounded). ─────────────────────────
        scored = None
        deadline = asyncio.get_event_loop().time() + 30.0
        while asyncio.get_event_loop().time() < deadline:
            try:
                msg = await asyncio.wait_for(scored_consumer.getone(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            event = json.loads(msg.value.decode("utf-8"))
            if event.get("claim_id") == claim_id:
                scored = event
                break

        # ── Assertions: ≥3 complete → determinate NON-AUTO_APPROVE. ─────────
        dispatch = captured["dispatch"]
        assert set(dispatch.engines_completed) == {
            EngineType.REPETITIVE_PATTERN,
            EngineType.HISTORY_INCONSISTENCY,
            EngineType.REGIONAL_ANALYSIS,
        }, dispatch.engines_completed
        assert dispatch.completed_count >= MIN_ENGINES_REQUIRED
        assert dispatch.indeterminate is False
        assert captured["outcome"].fallback is False

        # The Regional engine really produced ≥2 HIGH Indicators from the Job
        # Bank fixture (proves the adapter signals reached the engine, not a
        # DATA_UNAVAILABLE degrade).
        from shared.models import EngineType as _ET, Severity as _Sev
        regional_high = [
            ind for ind in dispatch.indicators
            if ind.engine == _ET.REGIONAL_ANALYSIS and ind.severity == _Sev.HIGH
        ]
        assert len(regional_high) >= 2, (
            "expected ≥2 HIGH regional Indicators from the Job Bank fixture; "
            f"got {[(i.algorithm, i.severity) for i in dispatch.indicators]}"
        )

        assert scored is not None, "no claim.scored event observed for the claim"
        assert scored["claim_id"] == claim_id
        # Real signals push the decision out of AUTO_APPROVE and INDETERMINATE.
        assert scored["risk_decision"] in {
            RiskDecision.FLAG_FOR_REVIEW.value,
            RiskDecision.INVESTIGATE.value,
        }, f"expected FLAG/INVESTIGATE with real signals, got {scored['risk_decision']}"
        assert scored["risk_decision"] != RiskDecision.AUTO_APPROVE.value
        assert scored["risk_decision"] != RiskDecision.INDETERMINATE.value
        assert isinstance(scored["risk_score"], int)
        assert scored["risk_score"] >= 30, scored["risk_score"]
    finally:
        if producer is not None:
            await producer.stop()
        if scored_consumer is not None:
            await scored_consumer.stop()
        if orchestrator_consumer is not None:
            await orchestrator_consumer.stop()
        if kafka_adapter is not None:
            await kafka_adapter.stop()
        if scoring_client is not None:
            await scoring_client.aclose()
        if kafka is not None:
            kafka.stop()
        for svc in services:
            svc.stop()
        os.environ.pop("ORCHESTRATOR_SERVICE_TOKEN", None)
        os.environ.pop("SERVICE_TOKEN", None)


# ===========================================================================
# Slice 3+ — FOUR engines including Geographic Anomaly (engine #4) WITH its
# GeoNames + MVR + CBSA mock-data adapters, over REAL transports.
#
# The Geographic Anomaly engine resolves the residence (FSA A1C → St. John's,
# NL) and work location (M5H → Toronto) via the GeoNames adapter's local dump,
# computes a Haversine distance of ~2108 km (> 100 km), derives the province NL
# from the FSA (Req 15.1), and queries the MVR adapter — whose NL fixture pins
# vehicle_ownership_status=NONE — yielding a HIGH "no registered vehicle"
# Indicator (Req 4.3). That single real HIGH signal, combined with ≥3 engines
# completing, drives the Ensemble Scorer to a determinate NON-AUTO_APPROVE
# decision (Req 2.2/9.2).
#
# Every engine→adapter call presents the engine's SERVICE_TOKEN (INVESTIGATOR
# Bearer); the three adapters run their REAL shared.auth JWKS validation
# (Req 19.7). Nothing is faked: real Kafka, real HTTP, real auth, real fixtures.
# ===========================================================================


def _start_geonames_adapter(jwks_url: str) -> LoopbackService:
    """Start the GeoNames adapter (dev = local dump) on loopback HTTP, auth-gated."""
    os.environ["GCKEY_JWKS_URL"] = jwks_url
    os.environ["APP_ENV"] = "development"
    _reset_service_settings("geonames-adapter")
    adapter_main = load_service_module(
        "geonames-adapter", "main", siblings=["config", "geo", "main"]
    )
    return LoopbackService(adapter_main.app, health_path="/healthz").start()


def _start_mvr_adapter(jwks_url: str) -> LoopbackService:
    """Start the MVR adapter (dev = synthetic fixtures) on loopback HTTP, auth-gated."""
    os.environ["GCKEY_JWKS_URL"] = jwks_url
    os.environ["APP_ENV"] = "development"
    _reset_service_settings("mvr-adapter")
    adapter_main = load_service_module(
        "mvr-adapter", "main", siblings=["config", "client", "main"]
    )
    return LoopbackService(adapter_main.app, health_path="/healthz").start()


def _start_cbsa_adapter(jwks_url: str) -> LoopbackService:
    """Start the CBSA adapter (dev = synthetic fixtures) on loopback HTTP, auth-gated."""
    os.environ["GCKEY_JWKS_URL"] = jwks_url
    os.environ["APP_ENV"] = "development"
    _reset_service_settings("cbsa-adapter")
    adapter_main = load_service_module(
        "cbsa-adapter", "main", siblings=["config", "client", "absence", "main"]
    )
    return LoopbackService(adapter_main.app, health_path="/healthz").start()


def _start_geographic_engine_with_adapters(
    jwks_url: str, geonames_url: str, mvr_url: str, cbsa_url: str
) -> LoopbackService:
    """Start Geographic Anomaly wired to REAL GeoNames + MVR + CBSA adapters."""
    os.environ["GCKEY_JWKS_URL"] = jwks_url
    os.environ["GEONAMES_ADAPTER_URL"] = geonames_url
    os.environ["MVR_ADAPTER_URL"] = mvr_url
    os.environ["CBSA_ADAPTER_URL"] = cbsa_url
    _reset_service_settings("geographic-anomaly-engine")
    engine_main = load_service_module(
        "geographic-anomaly-engine",
        "main",
        siblings=[
            "config", "geo", "province", "client", "transport",
            "mvr_client", "cbsa_client", "kafka_producer", "indicators", "main",
        ],
    )
    config_mod = load_service_module(
        "geographic-anomaly-engine", "config", siblings=["config"]
    )
    engine_main.app.state.settings = config_mod.get_settings()
    # Drop any adapter clients cached on app.state from an earlier import so the
    # lifespan rebuilds them against the new URLs + SERVICE_TOKEN. NOTE: the
    # engine's lifespan only builds a client when the attribute is ABSENT
    # (``not hasattr``), and its analyze() does NOT rebuild on None — so we must
    # delattr (not set to None), else the client stays None → RESOLUTION_FAILED.
    for _attr in ("geonames", "mvr", "cbsa"):
        if hasattr(engine_main.app.state, _attr):
            delattr(engine_main.app.state, _attr)
    return LoopbackService(engine_main.app, health_path="/healthz").start()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_four_engines_with_geographic_adapters_yield_determinate_over_real_transports(
    itest_service_auth,
):
    """≥4 engines incl. Geographic + its adapters → REAL HIGH signal → determinate.

    Geographic Anomaly (engine #4) resolves A1C (NL) → M5H (Toronto) = ~2108 km
    via the GeoNames adapter, derives province NL from the FSA (Req 15.1), and
    the MVR adapter's NL fixture (vehicle_ownership_status=NONE) drives a HIGH
    "no registered vehicle" Indicator (Req 4.3). With the Repetitive Pattern +
    History + Regional engines also completing (≥3, actually 4), the pipeline
    runs the Ensemble Scorer and publishes a determinate NON-AUTO_APPROVE
    decision (Req 2.2). Engines authenticate to the auth-gated adapters with
    their SERVICE_TOKEN (INVESTIGATOR Bearer, Req 19.7).
    """
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
    HttpxScorerCaller = scoring_mod.HttpxScorerCaller

    ResultPublisher = publisher_mod.ResultPublisher
    AIOKafkaProducerAdapter = publisher_mod.AIOKafkaProducerAdapter
    CLAIM_SCORED_TOPIC = publisher_mod.CLAIM_SCORED_TOPIC

    OrchestratorConsumer = consumer_mod.OrchestratorConsumer

    from shared.models import RiskDecision

    jwks_url = itest_service_auth.auth_server_url
    token = itest_service_auth.mint_token(roles=["INVESTIGATOR"])
    # Orchestrator (engine/scorer calls) + engines (adapter calls) both present
    # this INVESTIGATOR service Bearer on internal calls (Req 19.7).
    os.environ["ORCHESTRATOR_SERVICE_TOKEN"] = token
    os.environ["SERVICE_TOKEN"] = token

    services: list = []
    kafka = None
    orchestrator_consumer = producer = scored_consumer = kafka_adapter = None
    scoring_client = None
    try:
        # ── Geographic's three adapters first (their URLs feed the engine). ─
        geonames = _start_geonames_adapter(jwks_url)
        mvr = _start_mvr_adapter(jwks_url)
        cbsa = _start_cbsa_adapter(jwks_url)

        # ── Four engines: geographic wired to its adapters; the other three
        #    degrade (no adapters) but still COMPLETE. ────────────────────────
        rpe = _start_repetitive_pattern_engine(jwks_url)
        history = _start_history_inconsistency_engine(jwks_url)
        regional = _start_regional_analysis_engine(jwks_url)
        geographic = _start_geographic_engine_with_adapters(
            jwks_url, geonames.base_url, mvr.base_url, cbsa.base_url
        )
        scorer = _start_ensemble_scorer(jwks_url)
        services = [geonames, mvr, cbsa, rpe, history, regional, geographic, scorer]

        # Warm the shared JWKS cache once against a real engine.
        warm = httpx.post(
            f"{rpe.base_url}{ENGINE_ANALYZE_PATH}",
            json={"sin_hash": "warmup"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
        assert warm.status_code == 200, warm.text

        # Sanity-check the auth-gated adapter path directly (Req 19.7): the MVR
        # adapter must serve the NL fixture (NONE) WITH the Bearer and reject the
        # same request WITHOUT it.
        mvr_url = f"{mvr.base_url}/mvr/geo-nl-nocar?province=NL"
        mvr_ok = httpx.get(
            mvr_url, headers={"Authorization": f"Bearer {token}"}, timeout=5.0
        )
        assert mvr_ok.status_code == 200, mvr_ok.text
        assert mvr_ok.json()["record"]["vehicle_ownership_status"] == "NONE", mvr_ok.text
        mvr_401 = httpx.get(mvr_url, timeout=5.0)
        assert mvr_401.status_code == 401, "adapter must reject an unauthenticated call"

        # Endpoints: four implemented engines reachable; the other two at a
        # CLOSED loopback port so they fail fast → PARTIAL_RESULT.
        dead_port = _closed_loopback_port()
        real = {
            EngineType.REPETITIVE_PATTERN: rpe.base_url,
            EngineType.HISTORY_INCONSISTENCY: history.base_url,
            EngineType.REGIONAL_ANALYSIS: regional.base_url,
            EngineType.GEOGRAPHIC_ANOMALY: geographic.base_url,
        }
        endpoints = {}
        for engine in ALL_ENGINES:
            base = real.get(engine, f"http://127.0.0.1:{dead_port}")
            endpoints[engine] = f"{base}{ENGINE_ANALYZE_PATH}"
        timeouts = {engine: 5000 for engine in ALL_ENGINES}

        kafka = KafkaContainer()
        kafka.start()
        bootstrap = kafka.get_bootstrap_server()

        dispatcher = EngineDispatcher(endpoints=endpoints, timeouts_ms=timeouts)
        scoring_client = ScoringClient(
            HttpxScorerCaller(base_url=scorer.base_url), budget_ms=5000
        )
        kafka_adapter = AIOKafkaProducerAdapter(bootstrap_servers=bootstrap)
        result_publisher = ResultPublisher(kafka_adapter, audit_sink=None)

        captured: dict = {}

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
            group_id="itest-slice3plus-geo-orchestrator",
            degrade_gracefully=False,
        )
        assert await orchestrator_consumer.start() is True

        scored_consumer = AIOKafkaConsumer(
            CLAIM_SCORED_TOPIC,
            bootstrap_servers=bootstrap,
            group_id="itest-slice3plus-geo-assert",
            auto_offset_reset="earliest",
            enable_auto_commit=True,
        )
        await scored_consumer.start()

        # ── Produce paired events. residential_postal_code A1C (NL) is far from
        #    employer_work_location M5H (Toronto): ~2108 km > 100 km, and the NL
        #    MVR fixture reports NONE → HIGH no-vehicle (Req 4.3). ─────────────
        claim_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        claim_payload = {
            "claim_id": claim_id,
            "sin_hash": "c" * 64,
            "employer_bn": "111222333RP0001",
            "insurable_hours": "800",
            "insurable_earnings": "25000",
            "separation_date": "2024-03-15",
            "claim_start_date": "2024-04-01",
            "economic_region_code": "10",
            "noc_code": "12345",
            "residential_postal_code": "A1C",
            "employer_work_location": "M5H",
            "received_at": now,
        }
        roe_payload = {
            "claim_id": claim_id,
            "sin_hash": "c" * 64,
            "employer_bn": "111222333RP0001",
            "insurable_earnings": "25000",
            "received_at": now,
        }

        producer = AIOKafkaProducer(bootstrap_servers=bootstrap)
        await producer.start()
        for event_type, payload in (
            (CLAIM_RECEIVED, claim_payload),
            (ROE_RECEIVED, roe_payload),
        ):
            envelope = {"event_type": event_type, "claim_id": claim_id, "payload": payload}
            await producer.send_and_wait(
                event_type,
                value=json.dumps(envelope).encode("utf-8"),
                key=claim_id.encode("utf-8"),
            )

        # ── Await the claim.scored event (bounded). ─────────────────────────
        scored = None
        deadline = asyncio.get_event_loop().time() + 30.0
        while asyncio.get_event_loop().time() < deadline:
            try:
                msg = await asyncio.wait_for(scored_consumer.getone(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            event = json.loads(msg.value.decode("utf-8"))
            if event.get("claim_id") == claim_id:
                scored = event
                break

        # ── Assertions: ≥3 (here 4) complete → determinate; geographic HIGH. ─
        dispatch = captured["dispatch"]
        assert set(dispatch.engines_completed) == {
            EngineType.REPETITIVE_PATTERN,
            EngineType.HISTORY_INCONSISTENCY,
            EngineType.REGIONAL_ANALYSIS,
            EngineType.GEOGRAPHIC_ANOMALY,
        }, dispatch.engines_completed
        assert dispatch.completed_count >= MIN_ENGINES_REQUIRED
        assert dispatch.indeterminate is False
        assert captured["outcome"].fallback is False

        # The Geographic engine really produced a HIGH no-vehicle Indicator from
        # the FSA-derived province NL + the MVR fixture (proves the whole
        # engine→GeoNames→MVR chain over real transports, not a degrade).
        from shared.models import EngineType as _ET, Severity as _Sev
        geo_high = [
            ind for ind in dispatch.indicators
            if ind.engine == _ET.GEOGRAPHIC_ANOMALY and ind.severity == _Sev.HIGH
        ]
        assert len(geo_high) >= 1, (
            "expected ≥1 HIGH geographic Indicator (no-vehicle, Req 4.3); got "
            f"{[(i.engine, i.algorithm, i.severity) for i in dispatch.indicators]}"
        )
        # The HIGH indicator is the transport-implausibility (no vehicle) one and
        # carries the FSA-derived province NL in its payload.
        no_vehicle = [
            i for i in geo_high if i.algorithm == "transport_implausibility_vehicle"
        ]
        assert no_vehicle, [i.algorithm for i in geo_high]
        assert no_vehicle[0].payload.get("province") == "NL", no_vehicle[0].payload

        assert scored is not None, "no claim.scored event observed for the claim"
        assert scored["claim_id"] == claim_id
        # Determinate, and the real HIGH signal keeps it out of INDETERMINATE.
        assert scored["risk_decision"] != RiskDecision.INDETERMINATE.value
        assert scored["risk_decision"] in {
            RiskDecision.AUTO_APPROVE.value,
            RiskDecision.FLAG_FOR_REVIEW.value,
            RiskDecision.INVESTIGATE.value,
        }
        # A single HIGH indicator lifts the score above the AUTO_APPROVE band
        # (Req 9.2: AUTO_APPROVE is [0,29]); the decision is FLAG/INVESTIGATE.
        assert scored["risk_decision"] != RiskDecision.AUTO_APPROVE.value, (
            f"a real HIGH geographic signal should lift the score out of "
            f"AUTO_APPROVE; got {scored['risk_decision']} @ {scored['risk_score']}"
        )
        assert isinstance(scored["risk_score"], int)
        assert 0 <= scored["risk_score"] <= 100
    finally:
        if producer is not None:
            await producer.stop()
        if scored_consumer is not None:
            await scored_consumer.stop()
        if orchestrator_consumer is not None:
            await orchestrator_consumer.stop()
        if kafka_adapter is not None:
            await kafka_adapter.stop()
        if scoring_client is not None:
            await scoring_client.aclose()
        if kafka is not None:
            kafka.stop()
        for svc in services:
            svc.stop()
        os.environ.pop("ORCHESTRATOR_SERVICE_TOKEN", None)
        os.environ.pop("SERVICE_TOKEN", None)


# ===========================================================================
# Slice 3+ — FIVE engines including Fraud Ring (engine #5) over the REAL GRAPH
# relationship seam: a real Neo4j (testcontainers) behind the Graph Service,
# plus the Business Registry adapter, over real transports.
#
# The graph relationship is the value proposition (Req 5.1/5.5/5.10), so this
# test validates it with the real components:
#   1. Seed a 3-claimant fraud ring into REAL Neo4j via the Graph Service's
#      /graph/construct (shared employer BN + postal FSA "K1A", same separation
#      date 2024-03-01, ROEs within a 24h window).
#   2. Run the pipeline. The Fraud Ring engine reads the 24-month cohort BACK
#      from the graph (GET /graph/cohort/{bn} -> Neo4j) and drives:
#        - CRITICAL community_shared_attribute (>=40% share FSA, Req 5.5),
#        - CRITICAL coordinated_layoff (same date + ROEs within 24h, Req 5.4),
#        - HIGH shell_employer_no_record (no registry record, Req 5.3),
#      and community-detect returns a complete partition (Req 5.10).
#   3. With >=3 engines completing, the pipeline reaches a determinate
#      NON-AUTO_APPROVE decision reflecting the real graph evidence.
#
# Every engine->adapter/graph call presents the engine's SERVICE_TOKEN
# (INVESTIGATOR Bearer); the Graph Service + Business Registry adapter run their
# real shared.auth JWKS validation (Req 19.7). Nothing is faked: real Neo4j,
# real Kafka, real HTTP, real auth.
# ===========================================================================


def _start_graph_service_with_neo4j(jwks_url: str, bolt_url: str, password: str) -> "LoopbackService":
    """Start the Graph Service backed by a REAL Neo4j (testcontainers)."""
    os.environ["GCKEY_JWKS_URL"] = jwks_url
    _reset_service_settings("graph-service")
    graph_main = load_service_module(
        "graph-service", "main", siblings=["config", "community", "graph_repo", "main"]
    )
    graph_repo_mod = load_service_module(
        "graph-service", "graph_repo", siblings=["config", "community", "graph_repo", "main"]
    )
    # Build a repository against the LIVE Neo4j (test_mode=False so it opens a
    # real Bolt connection, not the in-memory fake).
    repo = graph_repo_mod.GraphRepository(
        uri=bolt_url, user="neo4j", password=password, test_mode=False
    )
    app = graph_main.create_app(repository=repo)
    return LoopbackService(app, health_path="/healthz").start()


def _start_business_registry_adapter(jwks_url: str) -> "LoopbackService":
    """Start the Business Registry adapter (dev = fixtures) on loopback, auth-gated."""
    os.environ["GCKEY_JWKS_URL"] = jwks_url
    os.environ["APP_ENV"] = "development"
    _reset_service_settings("business-registry-adapter")
    adapter_main = load_service_module(
        "business-registry-adapter", "main", siblings=["config", "client", "main"]
    )
    return LoopbackService(adapter_main.app, health_path="/healthz").start()


def _start_fraud_ring_engine_with_deps(
    jwks_url: str, registry_url: str, graph_url: str
) -> "LoopbackService":
    """Start the Fraud Ring engine wired to the REAL Business Registry + Graph Service."""
    os.environ["GCKEY_JWKS_URL"] = jwks_url
    os.environ["APP_ENV"] = "development"
    os.environ["BUSINESS_REGISTRY_ADAPTER_URL"] = registry_url
    os.environ["GRAPH_SERVICE_URL"] = graph_url
    _reset_service_settings("fraud-ring-engine")
    engine_main = load_service_module(
        "fraud-ring-engine",
        "main",
        siblings=[
            "config", "detection", "client", "community_analysis",
            "graph_client", "publisher", "main",
        ],
    )
    return LoopbackService(engine_main.app, health_path="/healthz").start()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_five_engines_with_fraud_ring_graph_seam_over_real_neo4j(
    itest_service_auth,
):
    """Fraud Ring (engine #5) over a REAL Neo4j graph seam -> CRITICAL evidence.

    Seeds a 3-claimant ring into real Neo4j via /graph/construct, then runs the
    pipeline: the Fraud Ring engine reads the cohort back from the graph and
    produces CRITICAL community_shared_attribute (Req 5.5) + CRITICAL
    coordinated_layoff (Req 5.4) + HIGH shell_employer_no_record (Req 5.3). With
    >=3 engines completing the decision is determinate NON-AUTO_APPROVE, and the
    community partition is complete (Req 5.10). Engines authenticate to the
    graph/adapter with their SERVICE_TOKEN (Req 19.7).
    """
    from testcontainers.kafka import KafkaContainer  # guarded import
    from testcontainers.neo4j import Neo4jContainer
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
    HttpxScorerCaller = scoring_mod.HttpxScorerCaller

    ResultPublisher = publisher_mod.ResultPublisher
    AIOKafkaProducerAdapter = publisher_mod.AIOKafkaProducerAdapter
    CLAIM_SCORED_TOPIC = publisher_mod.CLAIM_SCORED_TOPIC

    OrchestratorConsumer = consumer_mod.OrchestratorConsumer

    from shared.models import RiskDecision, EngineType as _ET, Severity as _Sev

    jwks_url = itest_service_auth.auth_server_url
    token = itest_service_auth.mint_token(roles=["INVESTIGATOR"])
    os.environ["ORCHESTRATOR_SERVICE_TOKEN"] = token
    os.environ["SERVICE_TOKEN"] = token

    employer_bn = "999000111RT0001"
    services: list = []
    kafka = neo4j = None
    orchestrator_consumer = producer = scored_consumer = kafka_adapter = None
    scoring_client = None
    try:
        # ── REAL Neo4j (testcontainers) behind the Graph Service. ───────────
        neo4j = Neo4jContainer("neo4j:5.26.0-community", password="itest_pw_change_me")
        neo4j.start()
        bolt_url = neo4j.get_connection_url()

        graph = _start_graph_service_with_neo4j(jwks_url, bolt_url, "itest_pw_change_me")
        registry = _start_business_registry_adapter(jwks_url)

        # ── Seed a 3-claimant fraud ring into REAL Neo4j via /graph/construct. ─
        # All share employer BN + FSA "K1A"; same separation date; ROEs in a 24h
        # window. This is the accumulated 24-month graph the engine reads back.
        for i in range(3):
            resp = httpx.post(
                f"{graph.base_url}/graph/construct",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "sin_hash": f"ring-s{i}",
                    "employer_bn": employer_bn,
                    "postal_fsa": "K1A",
                    "separation_date": "2024-03-01",
                    "roe_submission_ts": f"2024-03-01T09:0{i}:00+00:00",
                    "insurable_hours": 700.0 + i,
                    "claim_id": f"ring-c{i}",
                },
                timeout=10.0,
            )
            assert resp.status_code == 200, resp.text

        # Sanity: the cohort is readable BACK from the real graph, and the
        # endpoint 401s without the INVESTIGATOR Bearer (Req 19.7).
        cohort_url = f"{graph.base_url}/graph/cohort/{employer_bn}"
        c_ok = httpx.get(
            cohort_url, headers={"Authorization": f"Bearer {token}"}, timeout=10.0
        )
        assert c_ok.status_code == 200, c_ok.text
        assert c_ok.json()["cohort_size"] == 3, c_ok.text
        c_401 = httpx.get(cohort_url, timeout=10.0)
        assert c_401.status_code == 401, "graph cohort must reject unauthenticated"
        # The Business Registry adapter is also auth-gated (Req 19.7).
        reg_401 = httpx.get(f"{registry.base_url}/registry/employer/{employer_bn}", timeout=10.0)
        assert reg_401.status_code == 401, "registry must reject unauthenticated"

        # ── Engines: fraud-ring wired to the real graph + registry; the three
        #    light engines complete (degrade); document-forgery absent. ───────
        fraud = _start_fraud_ring_engine_with_deps(
            jwks_url, registry.base_url, graph.base_url
        )
        rpe = _start_repetitive_pattern_engine(jwks_url)
        history = _start_history_inconsistency_engine(jwks_url)
        regional = _start_regional_analysis_engine(jwks_url)
        scorer = _start_ensemble_scorer(jwks_url)
        services = [graph, registry, fraud, rpe, history, regional, scorer]

        # Warm the shared JWKS cache once.
        warm = httpx.post(
            f"{rpe.base_url}{ENGINE_ANALYZE_PATH}",
            json={"sin_hash": "warmup"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
        assert warm.status_code == 200, warm.text

        dead_port = _closed_loopback_port()
        real = {
            EngineType.FRAUD_RING: fraud.base_url,
            EngineType.REPETITIVE_PATTERN: rpe.base_url,
            EngineType.HISTORY_INCONSISTENCY: history.base_url,
            EngineType.REGIONAL_ANALYSIS: regional.base_url,
        }
        endpoints = {}
        for engine in ALL_ENGINES:
            base = real.get(engine, f"http://127.0.0.1:{dead_port}")
            endpoints[engine] = f"{base}{ENGINE_ANALYZE_PATH}"
        # Generous per-engine budgets so the graph round-trips complete in-test.
        timeouts = {engine: 8000 for engine in ALL_ENGINES}

        kafka = KafkaContainer()
        kafka.start()
        bootstrap = kafka.get_bootstrap_server()

        dispatcher = EngineDispatcher(endpoints=endpoints, timeouts_ms=timeouts)
        scoring_client = ScoringClient(
            HttpxScorerCaller(base_url=scorer.base_url), budget_ms=8000
        )
        kafka_adapter = AIOKafkaProducerAdapter(bootstrap_servers=bootstrap)
        result_publisher = ResultPublisher(kafka_adapter, audit_sink=None)

        captured: dict = {}

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
            group_id="itest-slice3plus-fraud-orchestrator",
            degrade_gracefully=False,
        )
        assert await orchestrator_consumer.start() is True

        scored_consumer = AIOKafkaConsumer(
            CLAIM_SCORED_TOPIC,
            bootstrap_servers=bootstrap,
            group_id="itest-slice3plus-fraud-assert",
            auto_offset_reset="earliest",
            enable_auto_commit=True,
        )
        await scored_consumer.start()

        # ── Produce paired events for a claim by one of the ring members. The
        #    fraud-ring engine sources the FULL cohort from the graph by BN. ──
        claim_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        claim_payload = {
            "claim_id": claim_id,
            "sin_hash": "ring-s0",
            "employer_bn": employer_bn,
            "insurable_hours": "700",
            "insurable_earnings": "20000",
            "separation_date": "2024-03-01",
            "claim_start_date": "2024-04-01",
            "economic_region_code": "35",
            "noc_code": "12345",
            "received_at": now,
        }
        roe_payload = {
            "claim_id": claim_id,
            "sin_hash": "ring-s0",
            "employer_bn": employer_bn,
            "insurable_earnings": "20000",
            "received_at": now,
        }

        producer = AIOKafkaProducer(bootstrap_servers=bootstrap)
        await producer.start()
        for event_type, payload in (
            (CLAIM_RECEIVED, claim_payload),
            (ROE_RECEIVED, roe_payload),
        ):
            envelope = {"event_type": event_type, "claim_id": claim_id, "payload": payload}
            await producer.send_and_wait(
                event_type,
                value=json.dumps(envelope).encode("utf-8"),
                key=claim_id.encode("utf-8"),
            )

        scored = None
        deadline = asyncio.get_event_loop().time() + 40.0
        while asyncio.get_event_loop().time() < deadline:
            try:
                msg = await asyncio.wait_for(scored_consumer.getone(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            event = json.loads(msg.value.decode("utf-8"))
            if event.get("claim_id") == claim_id:
                scored = event
                break

        # ── Assertions: the Fraud Ring engine's GRAPH-SOURCED evidence. ─────
        dispatch = captured["dispatch"]
        assert EngineType.FRAUD_RING in dispatch.engines_completed, (
            dispatch.engines_completed
        )
        assert dispatch.completed_count >= MIN_ENGINES_REQUIRED
        assert dispatch.indeterminate is False
        assert captured["outcome"].fallback is False

        fraud_inds = [
            i for i in dispatch.indicators if i.engine == _ET.FRAUD_RING
        ]
        algos = {i.algorithm for i in fraud_inds}
        # Req 5.5 community cluster CRITICAL from the graph-sourced cohort.
        cluster = [i for i in fraud_inds if i.algorithm == "community_shared_attribute"]
        assert cluster, f"expected community cluster; got {sorted(algos)}"
        assert cluster[0].severity == _Sev.CRITICAL
        assert cluster[0].payload.get("matched_value") == "K1A"
        assert cluster[0].payload.get("matched_attribute_type") == "postal_fsa"
        # Req 5.4 coordinated-layoff CRITICAL from the graph-sourced cohort.
        layoff = [i for i in fraud_inds if i.algorithm == "coordinated_layoff"]
        assert layoff and layoff[0].severity == _Sev.CRITICAL, sorted(algos)
        # Req 5.3 shell-employer HIGH (no registry record for this synthetic BN).
        shell = [i for i in fraud_inds if i.algorithm == "shell_employer_no_record"]
        assert shell and shell[0].severity == _Sev.HIGH, sorted(algos)

        # Req 5.10 completeness: every ring claimant placed in exactly one
        # community (partition covers the 3 seeded claimants).
        cohort_ids = set(cluster[0].payload.get("claimant_ids") or [])
        assert {"ring-s0", "ring-s1", "ring-s2"} <= cohort_ids, cohort_ids

        # The determinate decision reflects the CRITICAL graph evidence.
        assert scored is not None, "no claim.scored event observed for the claim"
        assert scored["claim_id"] == claim_id
        assert scored["risk_decision"] != RiskDecision.INDETERMINATE.value
        assert scored["risk_decision"] != RiskDecision.AUTO_APPROVE.value, (
            f"CRITICAL fraud-ring evidence should lift the score out of "
            f"AUTO_APPROVE; got {scored['risk_decision']} @ {scored['risk_score']}"
        )
        assert scored["risk_decision"] in {
            RiskDecision.FLAG_FOR_REVIEW.value,
            RiskDecision.INVESTIGATE.value,
        }
        assert isinstance(scored["risk_score"], int)
    finally:
        if producer is not None:
            await producer.stop()
        if scored_consumer is not None:
            await scored_consumer.stop()
        if orchestrator_consumer is not None:
            await orchestrator_consumer.stop()
        if kafka_adapter is not None:
            await kafka_adapter.stop()
        if scoring_client is not None:
            await scoring_client.aclose()
        if kafka is not None:
            kafka.stop()
        for svc in services:
            svc.stop()
        if neo4j is not None:
            neo4j.stop()
        os.environ.pop("ORCHESTRATOR_SERVICE_TOKEN", None)
        os.environ.pop("SERVICE_TOKEN", None)


# ===========================================================================
# Slice 3+ — SIX engines including Document Forgery (engine #6) with the REAL
# CRA payroll seam (Req 7.2/7.7/19.7), over real transports.
#
# The pipeline forwards the ROE fields it carries to the Document Forgery engine
# as a nested ``document`` (orchestrator _build_engine_request). This test
# proves, over real Kafka + loopback HTTP + real GCKey JWKS auth + the REAL CRA
# adapter:
#   * Req 7.2 date-order CRITICAL — the forwarded last_day_paid (separation date)
#     precedes first_day_worked (claim start), an impossible ROE date order.
#   * Req 7.7 payroll CRITICAL — the claimant SIN (a ``ghost-`` marker) is
#     confirmed ABSENT from the employer's payroll by the real CRA adapter
#     /cra/payroll call, authenticated with the engine's SERVICE_TOKEN Bearer.
# With >=3 engines completing the decision is determinate NON-AUTO_APPROVE.
#
# OCR is the LayoutLMv3 plug-in point (stub here — the model is a deployment
# concern). The hours*rate consistency check (7.2a), font (7.4), signature
# (7.5/7.6), and LLM style (7.8) are NOT fed from claim.received/roe.received
# and are honestly not exercised in-pipeline (no hourly_rate / document / registry
# source) — see docs/regeneration/STATUS.md.
# ===========================================================================


def _start_document_forgery_engine(jwks_url: str, cra_url: str) -> "LoopbackService":
    """Start Document Forgery wired to the REAL CRA adapter (payroll, Req 7.7)."""
    os.environ["GCKEY_JWKS_URL"] = jwks_url
    os.environ["APP_ENV"] = "development"
    os.environ["USE_STUB_OCR"] = "true"
    os.environ["CRA_ADAPTER_BASE_URL"] = cra_url
    _reset_service_settings("document-forgery-engine")
    engine_main = load_service_module(
        "document-forgery-engine",
        "main",
        siblings=[
            "config", "ocr", "consistency", "metadata_forensics",
            "font_fingerprint", "signature_verification", "style_analysis",
            "payroll_cross_reference", "roe_canonical", "kafka_publisher", "main",
        ],
    )
    # The engine builds its CRA payroll client in the lifespan from the freshly
    # set settings (CRA_ADAPTER_BASE_URL + SERVICE_TOKEN); LoopbackService runs
    # the lifespan on start(), so no app.state fix-up is needed here.
    return LoopbackService(engine_main.app, health_path="/healthz").start()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_six_engines_with_document_forgery_cra_payroll_over_real_transports(
    itest_service_auth,
):
    """Document Forgery (engine #6) over the real CRA payroll seam -> CRITICAL.

    A ghost-SIN forged ROE (last_day_paid before first_day_worked) runs the
    6-engine pipeline: Document Forgery produces a Req 7.2 date-order CRITICAL
    and a Req 7.7 payroll CRITICAL (SIN confirmed absent by the real CRA adapter,
    authenticated with the engine's SERVICE_TOKEN). With >=3 engines completing
    the decision is determinate NON-AUTO_APPROVE. The CRA adapter 401s without
    the Bearer (Req 19.7).
    """
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
    HttpxScorerCaller = scoring_mod.HttpxScorerCaller

    ResultPublisher = publisher_mod.ResultPublisher
    AIOKafkaProducerAdapter = publisher_mod.AIOKafkaProducerAdapter
    CLAIM_SCORED_TOPIC = publisher_mod.CLAIM_SCORED_TOPIC

    OrchestratorConsumer = consumer_mod.OrchestratorConsumer

    from shared.models import RiskDecision, EngineType as _ET, Severity as _Sev

    jwks_url = itest_service_auth.auth_server_url
    token = itest_service_auth.mint_token(roles=["INVESTIGATOR"])
    os.environ["ORCHESTRATOR_SERVICE_TOKEN"] = token
    os.environ["SERVICE_TOKEN"] = token

    employer_bn = "987654321RT0001"
    services: list = []
    kafka = None
    orchestrator_consumer = producer = scored_consumer = kafka_adapter = None
    scoring_client = None
    try:
        # ── CRA adapter (real payroll seam) + Document Forgery engine. ──────
        cra = _start_cra_adapter(jwks_url)
        docf = _start_document_forgery_engine(jwks_url, cra.base_url)
        # Three light engines so >=3 complete; the other two absent.
        rpe = _start_repetitive_pattern_engine(jwks_url)
        history = _start_history_inconsistency_engine(jwks_url)
        regional = _start_regional_analysis_engine(jwks_url)
        scorer = _start_ensemble_scorer(jwks_url)
        services = [cra, docf, rpe, history, regional, scorer]

        # Warm the shared JWKS cache once.
        warm = httpx.post(
            f"{rpe.base_url}{ENGINE_ANALYZE_PATH}",
            json={"sin_hash": "warmup"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
        assert warm.status_code == 200, warm.text

        # Sanity-check the real CRA payroll seam directly (Req 19.7 + 7.7): the
        # ghost SIN is confirmed absent WITH the Bearer, and 401s WITHOUT it.
        pay_url = f"{cra.base_url}/cra/payroll/ghost-itest/{employer_bn}"
        pay_ok = httpx.get(
            pay_url,
            params={"start": "2024-03-01", "end": "2024-01-15"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
        assert pay_ok.status_code == 200, pay_ok.text
        assert pay_ok.json()["sin_in_payroll"] is False, pay_ok.text
        pay_401 = httpx.get(
            pay_url, params={"start": "2024-03-01", "end": "2024-01-15"}, timeout=5.0
        )
        assert pay_401.status_code == 401, "CRA payroll must reject unauthenticated"

        dead_port = _closed_loopback_port()
        real = {
            EngineType.DOCUMENT_FORGERY: docf.base_url,
            EngineType.REPETITIVE_PATTERN: rpe.base_url,
            EngineType.HISTORY_INCONSISTENCY: history.base_url,
            EngineType.REGIONAL_ANALYSIS: regional.base_url,
        }
        endpoints = {}
        for engine in ALL_ENGINES:
            base = real.get(engine, f"http://127.0.0.1:{dead_port}")
            endpoints[engine] = f"{base}{ENGINE_ANALYZE_PATH}"
        timeouts = {engine: 5000 for engine in ALL_ENGINES}

        kafka = KafkaContainer()
        kafka.start()
        bootstrap = kafka.get_bootstrap_server()

        dispatcher = EngineDispatcher(endpoints=endpoints, timeouts_ms=timeouts)
        scoring_client = ScoringClient(
            HttpxScorerCaller(base_url=scorer.base_url), budget_ms=5000
        )
        kafka_adapter = AIOKafkaProducerAdapter(bootstrap_servers=bootstrap)
        result_publisher = ResultPublisher(kafka_adapter, audit_sink=None)

        captured: dict = {}

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
            group_id="itest-slice3plus-docf-orchestrator",
            degrade_gracefully=False,
        )
        assert await orchestrator_consumer.start() is True

        scored_consumer = AIOKafkaConsumer(
            CLAIM_SCORED_TOPIC,
            bootstrap_servers=bootstrap,
            group_id="itest-slice3plus-docf-assert",
            auto_offset_reset="earliest",
            enable_auto_commit=True,
        )
        await scored_consumer.start()

        # ── Produce the forged-ROE paired events. sin_hash 'ghost-...' -> CRA
        #    payroll ABSENT (7.7). separation_date (last_day_paid) 2024-03-01
        #    PRECEDES claim_start_date (first_day_worked) 2024-06-01 -> impossible
        #    date order (7.2). ──────────────────────────────────────────────
        claim_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        claim_payload = {
            "claim_id": claim_id,
            "sin_hash": "ghost-" + ("d" * 58),
            "employer_bn": employer_bn,
            "insurable_hours": "800",
            "insurable_earnings": "25000",
            "separation_date": "2024-03-01",   # -> last_day_paid
            "claim_start_date": "2024-06-01",  # -> first_day_worked (later!)
            "economic_region_code": "35",
            "noc_code": "12345",
            "received_at": now,
        }
        roe_payload = {
            "claim_id": claim_id,
            "sin_hash": "ghost-" + ("d" * 58),
            "employer_bn": employer_bn,
            "insurable_earnings": "25000",
            "received_at": now,
        }

        producer = AIOKafkaProducer(bootstrap_servers=bootstrap)
        await producer.start()
        for event_type, payload in (
            (CLAIM_RECEIVED, claim_payload),
            (ROE_RECEIVED, roe_payload),
        ):
            envelope = {"event_type": event_type, "claim_id": claim_id, "payload": payload}
            await producer.send_and_wait(
                event_type,
                value=json.dumps(envelope).encode("utf-8"),
                key=claim_id.encode("utf-8"),
            )

        scored = None
        deadline = asyncio.get_event_loop().time() + 40.0
        while asyncio.get_event_loop().time() < deadline:
            try:
                msg = await asyncio.wait_for(scored_consumer.getone(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            event = json.loads(msg.value.decode("utf-8"))
            if event.get("claim_id") == claim_id:
                scored = event
                break

        # ── Assertions: Document Forgery's real evidence over transports. ───
        dispatch = captured["dispatch"]
        assert EngineType.DOCUMENT_FORGERY in dispatch.engines_completed, (
            dispatch.engines_completed
        )
        assert dispatch.completed_count >= MIN_ENGINES_REQUIRED
        assert dispatch.indeterminate is False
        assert captured["outcome"].fallback is False

        docf_inds = [i for i in dispatch.indicators if i.engine == _ET.DOCUMENT_FORGERY]
        algos = {i.algorithm for i in docf_inds}
        # Req 7.7 payroll CRITICAL via the REAL CRA adapter call.
        payroll = [i for i in docf_inds if i.algorithm == "payroll_cross_reference"]
        assert payroll and payroll[0].severity == _Sev.CRITICAL, sorted(algos)
        assert payroll[0].payload.get("flag") == "SIN_NOT_IN_PAYROLL"
        # Req 7.2 date-order CRITICAL from the forwarded ROE dates.
        date_order = [i for i in docf_inds if i.algorithm == "date_order_consistency"]
        assert date_order and date_order[0].severity == _Sev.CRITICAL, sorted(algos)

        # The determinate decision reflects the CRITICAL forgery evidence.
        assert scored is not None, "no claim.scored event observed for the claim"
        assert scored["claim_id"] == claim_id
        assert scored["risk_decision"] != RiskDecision.INDETERMINATE.value
        assert scored["risk_decision"] != RiskDecision.AUTO_APPROVE.value, (
            f"CRITICAL forgery evidence should lift the score out of AUTO_APPROVE; "
            f"got {scored['risk_decision']} @ {scored['risk_score']}"
        )
        assert scored["risk_decision"] in {
            RiskDecision.FLAG_FOR_REVIEW.value,
            RiskDecision.INVESTIGATE.value,
        }
        assert isinstance(scored["risk_score"], int)
    finally:
        if producer is not None:
            await producer.stop()
        if scored_consumer is not None:
            await scored_consumer.stop()
        if orchestrator_consumer is not None:
            await orchestrator_consumer.stop()
        if kafka_adapter is not None:
            await kafka_adapter.stop()
        if scoring_client is not None:
            await scoring_client.aclose()
        if kafka is not None:
            kafka.stop()
        for svc in services:
            svc.stop()
        os.environ.pop("ORCHESTRATOR_SERVICE_TOKEN", None)
        os.environ.pop("SERVICE_TOKEN", None)
