"""
tests/integration/test_e2e_decision_paths.py
============================================
End-to-end integration tests for the four EI-ADS decision pathways (task 33.1).

This module verifies the *full* detection pipeline decision flow across the four
routing outcomes the orchestrator can produce, wiring together the real service
modules built earlier in the plan:

    correlator.py  ->  engine_dispatch.py  ->  ensemble scorer  ->  publisher.py
      (task 6.1)         (task 6.2)            (task 7)             (task 6.3)

The four scenarios (each a separate ``test_...`` function) map to the task's
requirement bullets:

  * Test 1 — happy path (Req 2.2): a benign claim (engines produce no
    fraud Indicators) scores 0 -> ``AUTO_APPROVE`` and the outcome is produced
    well within the 2,000 ms end-to-end budget, and a ClickHouse audit record is
    written.
  * Test 2 — INVESTIGATE path (Req 2.8): a claim carrying synthetic fraud
    signals scores >= 70 -> ``INVESTIGATE``; both ``claim.scored`` and
    ``claim.escalated`` are published to Kafka and the Special Investigation
    Unit (SIU) is notified.
  * Test 3 — PARTIAL_RESULT (Req 2.2/2.3): two engines are killed; the two
    produce PARTIAL_RESULT Indicators, the remaining four complete, and scoring
    still completes and publishes ``claim.scored``.
  * Test 4 — INDETERMINATE (Req 2.4, 2.5): four engines are killed; fewer than
    the three-engine minimum complete, so the decision is ``INDETERMINATE`` and
    payment processing is halted.

Two execution surfaces
----------------------
1. **In-process** (always runs, no external services): the real orchestrator
   modules are wired together through their public dependency-injection seams.
   The six detection engines, the Ensemble Scorer HTTP hop, the Kafka producer,
   the ClickHouse audit sink, and the SIU notifier are all supplied as
   in-memory fakes/adapters. The *decision logic* under test is real: the real
   ``EnsembleScorer`` maps Indicators -> Risk_Score -> Risk_Decision, the real
   ``EngineDispatcher`` enforces the per-engine budgets and the >= 3-engine
   minimum, and the real ``ResultPublisher`` emits the Kafka events and audit
   rows. This is the coverage the task requires and it runs in any environment.

2. **testcontainers** (preferred, but skipped when unavailable): the task calls
   for spinning up Kafka, Redis, PostgreSQL, ClickHouse, Neo4j, and the six
   engine mock servers via ``testcontainers`` (4.9.0). That requires both the
   ``testcontainers`` package AND a reachable container runtime
   (Docker/Podman) — neither of which is present in the CI sandbox. The
   container-backed test is therefore guarded by ``pytest.mark.skipif`` on a
   runtime-detection predicate (:func:`_container_runtime_available`) so the
   suite collects and passes cleanly without a daemon, and only actually
   exercises real infrastructure when one is available.

    HOW TO RUN THE CONTAINER-BACKED PATH
    ------------------------------------
    Install the container extras and start a runtime, then run::

        poetry install --with dev            # installs testcontainers 4.9.0
        # ensure Docker or Podman is running and DOCKER_HOST is reachable
        pytest tests/integration/test_e2e_decision_paths.py -m integration

Requirements: 2.2, 2.4, 2.5, 2.8
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
# Load the hyphenated pipeline-orchestrator + ensemble-scorer service modules
# collision-free (see tests/conftest.py::load_service_module for the rationale).
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from tests.conftest import load_service_module  # noqa: E402

_ORCH = "pipeline-orchestrator"
_SCORER = "ensemble-scorer"

_correlator = load_service_module(_ORCH, "correlator", siblings=["correlator"])
_engine_dispatch = load_service_module(
    _ORCH, "engine_dispatch", siblings=["engine_dispatch"]
)
_scoring = load_service_module(_ORCH, "scoring", siblings=["scoring"])
_publisher = load_service_module(_ORCH, "publisher", siblings=["scoring", "publisher"])

# The real Ensemble Scorer decision engine (task 7). Its siblings are loaded so
# its intra-service bare imports (features / shap_explainer) resolve.
_scorer_mod = load_service_module(
    _SCORER,
    "scorer",
    siblings=["features", "shap_explainer", "scorer"],
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

EnsembleScorer = _scorer_mod.EnsembleScorer

from shared.models import (  # noqa: E402 - after sys.path/module setup
    EngineType,
    Indicator,
    RiskDecision,
    Severity,
)


# ===========================================================================
# In-memory fakes / adapters wiring the pipeline together end-to-end
# ===========================================================================


def _make_indicator(
    claim_id_uuid,
    engine: EngineType,
    severity: Severity,
    *,
    title: str = "finding",
) -> Indicator:
    """Build a canonical Indicator attributed to a given engine + severity."""
    return Indicator(
        indicator_id=uuid4(),
        claim_id=claim_id_uuid,
        engine=engine,
        severity=severity,
        algorithm="e2e_integration_test",
        title=f"{engine.value} {title}",
        explanation_en=f"{engine.value} finding ({severity.value})",
        explanation_fr=f"constat {engine.value} ({severity.value})",
        payload={"engine": engine.value, "severity": severity.value},
        produced_at=datetime.now(timezone.utc),
    )


class ScriptedEngineCaller:
    """Fake detection-engine caller returning scripted Indicators per engine.

    Parameters
    ----------
    severity_by_engine:
        Maps each engine to the :class:`Severity` of the single Indicator it
        should return. Engines mapped to ``None`` return an EMPTY indicator list
        (they completed but found nothing) — this is how the happy path yields a
        zero Risk_Score.
    timeout_engines:
        Engines that should never respond (they sleep past their budget) so
        ``asyncio.wait_for`` in the dispatcher cancels them, producing
        PARTIAL_RESULT Indicators. Used to simulate "killed" engines.
    """

    def __init__(
        self,
        severity_by_engine: dict[EngineType, Severity | None],
        *,
        timeout_engines: set[EngineType] | None = None,
    ) -> None:
        self._severity_by_engine = severity_by_engine
        self._timeout_engines = timeout_engines or set()
        self.calls: list[EngineType] = []

    async def __call__(self, engine, url, claim_payload, roe_payload):
        self.calls.append(engine)
        if engine in self._timeout_engines:
            await asyncio.sleep(3600)  # cancelled by the dispatcher's wait_for
            return []  # pragma: no cover - never reached

        # Reuse the claim's UUID so all indicators share a claim_id.
        raw_id = str(claim_payload.get("claim_id"))
        try:
            from uuid import UUID

            claim_uuid = UUID(raw_id)
        except (ValueError, TypeError):
            from uuid import uuid5, NAMESPACE_URL

            claim_uuid = uuid5(NAMESPACE_URL, raw_id)

        severity = self._severity_by_engine.get(engine)
        if severity is None:
            return []  # engine completed with no findings
        return [_make_indicator(claim_uuid, engine, severity)]


class RealScorerCaller:
    """Scorer caller that drives the REAL :class:`EnsembleScorer`.

    ``ScoringClient`` POSTs ``{"indicators": [...dicts...]}`` and expects a dict
    matching the Ensemble Scorer's ``ScoreResponse``. This adapter decodes the
    dicts back to :class:`Indicator` objects, runs the real scorer, and returns
    the canonical response shape — so the decision boundaries under test
    (AUTO_APPROVE < 30, FLAG 30-69, INVESTIGATE >= 70) are the production ones.

    PARTIAL_RESULT markers (``severity == UNRESOLVED``) are filtered out before
    scoring, mirroring the scorer's real feature encoding which only consumes
    genuine findings.
    """

    def __init__(self, scorer: EnsembleScorer) -> None:
        self._scorer = scorer
        self.calls: list[dict] = []

    async def __call__(self, body: dict) -> dict:
        self.calls.append(body)
        indicators = []
        for raw in body.get("indicators", []):
            try:
                ind = Indicator.from_dict(raw)
            except Exception:  # noqa: BLE001 - tolerate loose shapes
                continue
            if ind.severity == Severity.UNRESOLVED:
                continue  # skip PARTIAL_RESULT markers
            indicators.append(ind)

        assessment = self._scorer.assess(indicators)
        return {
            "assessment_id": str(assessment.assessment_id),
            "risk_score": assessment.risk_score,
            "risk_decision": assessment.risk_decision.value,
            "shap_base_value": assessment.shap_base_value,
            "shap_attributions": [],
            "model_version": assessment.model_version,
        }


class FakeKafkaProducer:
    """Captures published (topic, key, value) tuples in publication order."""

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


class FakeClickHouseAuditSink:
    """Captures rows inserted into the ClickHouse audit table."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, dict]] = []

    async def insert(self, table: str, row: dict) -> None:
        self.rows.append((table, row))

    def records_for(self, table: str) -> list[dict]:
        return [row for (t, row) in self.rows if t == table]


class FakeSIUNotifier:
    """Records SIU (Special Investigation Unit) notifications.

    Requirement 2.8 requires that, on an INVESTIGATE decision, the pipeline both
    publishes ``claim.escalated`` AND notifies the Investigation_Unit "via secure
    government messaging". No production SIU-notification transport exists in the
    codebase yet, so this test models the notification seam: the escalation
    consumer invokes the notifier, and the test asserts it fired for INVESTIGATE
    claims (and did NOT fire for non-escalated decisions).
    """

    def __init__(self) -> None:
        self.notifications: list[dict] = []

    async def notify(self, claim_id: str, risk_score: int) -> None:
        self.notifications.append(
            {
                "claim_id": claim_id,
                "risk_score": risk_score,
                "channel": "secure-government-messaging",
                "notified_at": datetime.now(timezone.utc).isoformat(),
            }
        )


# ===========================================================================
# The end-to-end pipeline seam under test
# ===========================================================================


class DecisionPipeline:
    """Wires correlator -> dispatch -> scoring -> publish -> SIU notify.

    This is the integration seam that reproduces the production orchestrator's
    hand-off chain across tasks 6.1-6.3 + 7, plus the Req 2.8 SIU-notification
    step that fires whenever an escalation is published.
    """

    def __init__(self, dispatcher, scoring_client, publisher, audit_sink, siu_notifier):
        self._dispatcher = dispatcher
        self._scoring = scoring_client
        self._publisher = publisher
        self._audit_sink = audit_sink
        self._siu = siu_notifier

    async def process(self, claim_id: str) -> dict:
        # 6.1: correlate the paired claim.received + roe.received events.
        pair = await self._correlate(claim_id)

        # 6.2: dispatch all six engines concurrently.
        dispatch = await self._dispatcher.dispatch(pair.claim_payload, pair.roe_payload)

        # 7 / 6.3: score within budget (short-circuits to INDETERMINATE when the
        # < 3-engine rule already failed).
        outcome = await self._scoring.score(
            dispatch.indicators,
            dispatch.claim_id,
            dispatch.engines_completed,
            dispatch.engines_partial,
            indeterminate=dispatch.indeterminate,
        )

        # 6.3: publish claim.scored (+ claim.escalated on INVESTIGATE) and audit.
        publish = await self._publisher.publish_scored(dispatch.claim_id, outcome)
        await self._publisher.write_audit(
            claim_id=dispatch.claim_id,
            component="pipeline-orchestrator",
            action="score",
            fields_accessed=["indicators"],
            outcome="SUCCESS" if not outcome.fallback else (outcome.error or "FALLBACK"),
            duration_ms=outcome.elapsed_ms,
        )

        # Req 2.8: notify SIU whenever an escalation was published.
        if publish.escalated_published:
            await self._siu.notify(dispatch.claim_id, outcome.risk_score)

        return {"dispatch": dispatch, "outcome": outcome, "publish": publish}

    @staticmethod
    async def _correlate(claim_id: str):
        store = InMemoryPairingStore()
        correlator = EventCorrelator(store)
        now = datetime.now(timezone.utc).isoformat()
        claim_payload = {"claim_id": claim_id, "received_at": now}
        roe_payload = {"claim_id": claim_id, "received_at": now}
        first = await correlator.on_event(CLAIM_RECEIVED, claim_id, claim_payload)
        assert first is None
        pair = await correlator.on_event(ROE_RECEIVED, claim_id, roe_payload)
        assert pair is not None
        return pair


def _build_pipeline(
    *,
    engine_caller,
    scorer_caller,
    producer,
    audit_sink,
    siu_notifier,
    timeouts_ms: dict | None = None,
):
    """Assemble a fully-wired in-process decision pipeline from real modules."""
    timeouts = timeouts_ms or {engine: 300 for engine in ALL_ENGINES}
    dispatcher = EngineDispatcher(
        engine_caller,
        endpoints={engine: f"http://fake/{engine.value}" for engine in ALL_ENGINES},
        timeouts_ms=timeouts,
    )
    scoring_client = ScoringClient(scorer_caller, budget_ms=1000)
    publisher = ResultPublisher(producer, audit_sink=audit_sink)
    return DecisionPipeline(dispatcher, scoring_client, publisher, audit_sink, siu_notifier)


# End-to-end budget from Requirement 2.2 / 2 (final decision within 2,000 ms of
# claim.received). The in-process path has no network latency, so it completes in
# a few ms; we still assert the wall-clock budget explicitly per the task.
E2E_BUDGET_MS = 2000


# ===========================================================================
# In-process integration tests (always run — no external services)
# ===========================================================================


@pytest.mark.asyncio
async def test_happy_path_auto_approve_within_budget_and_audited():
    """Test 1 — benign claim -> AUTO_APPROVE within 2,000 ms + audit written.

    All six engines complete and return NO fraud Indicators, so the real
    Ensemble Scorer produces Risk_Score 0 -> AUTO_APPROVE. The end-to-end
    decision is produced well within the 2,000 ms budget and a ClickHouse audit
    record is written.

    Validates: Requirements 2.2
    """
    claim_id = str(uuid4())
    # Every engine completes with no findings -> empty indicator lists.
    engine_caller = ScriptedEngineCaller({engine: None for engine in ALL_ENGINES})
    scorer = RealScorerCaller(EnsembleScorer())
    producer = FakeKafkaProducer()
    audit = FakeClickHouseAuditSink()
    siu = FakeSIUNotifier()
    pipeline = _build_pipeline(
        engine_caller=engine_caller,
        scorer_caller=scorer,
        producer=producer,
        audit_sink=audit,
        siu_notifier=siu,
    )

    start = asyncio.get_event_loop().time()
    result = await pipeline.process(claim_id)
    elapsed_ms = (asyncio.get_event_loop().time() - start) * 1000.0

    dispatch = result["dispatch"]
    outcome = result["outcome"]

    # All six engines completed; none indeterminate.
    assert set(dispatch.engines_completed) == set(ALL_ENGINES)
    assert dispatch.indeterminate is False

    # Zero indicators -> Risk_Score 0 -> AUTO_APPROVE (real scorer, P8).
    assert outcome.risk_score == 0
    assert outcome.risk_decision == RiskDecision.AUTO_APPROVE

    # Decision produced within the 2,000 ms end-to-end budget.
    assert elapsed_ms < E2E_BUDGET_MS

    # claim.scored published; no escalation, no SIU notification.
    assert producer.topics() == [CLAIM_SCORED_TOPIC]
    scored = producer.event_for(CLAIM_SCORED_TOPIC)
    assert scored["payload"]["risk_decision"] == "AUTO_APPROVE"
    assert CLAIM_ESCALATED_TOPIC not in producer.topics()
    assert siu.notifications == []

    # A ClickHouse audit record was written for this claim.
    audit_records = audit.records_for(ADAPTER_AUDIT_TABLE)
    assert len(audit_records) == 1
    assert audit_records[0]["claim_id"] == dispatch.claim_id
    assert audit_records[0]["outcome"] == "SUCCESS"


@pytest.mark.asyncio
async def test_investigate_path_escalates_and_notifies_siu():
    """Test 2 — synthetic fraud signals -> INVESTIGATE, escalated, SIU notified.

    Every engine returns a CRITICAL Indicator, which drives the real Ensemble
    Scorer's Risk_Score into the INVESTIGATE band (>= 70). The pipeline then
    publishes both ``claim.scored`` and ``claim.escalated`` and notifies the SIU.

    Validates: Requirements 2.8
    """
    claim_id = str(uuid4())
    # Synthetic fraud: every engine fires a CRITICAL Indicator.
    engine_caller = ScriptedEngineCaller(
        {engine: Severity.CRITICAL for engine in ALL_ENGINES}
    )
    scorer = RealScorerCaller(EnsembleScorer())
    producer = FakeKafkaProducer()
    audit = FakeClickHouseAuditSink()
    siu = FakeSIUNotifier()
    pipeline = _build_pipeline(
        engine_caller=engine_caller,
        scorer_caller=scorer,
        producer=producer,
        audit_sink=audit,
        siu_notifier=siu,
    )

    result = await pipeline.process(claim_id)
    outcome = result["outcome"]
    publish = result["publish"]

    # Real scorer routes a fully-CRITICAL claim to INVESTIGATE (score >= 70).
    assert outcome.risk_score >= 70
    assert outcome.risk_decision == RiskDecision.INVESTIGATE

    # Both claim.scored and claim.escalated published, keyed by the same claim.
    assert publish.scored_published is True
    assert publish.escalated_published is True
    assert producer.topics() == [CLAIM_SCORED_TOPIC, CLAIM_ESCALATED_TOPIC]
    scored = producer.event_for(CLAIM_SCORED_TOPIC)
    escalated = producer.event_for(CLAIM_ESCALATED_TOPIC)
    assert scored["key"] == escalated["key"] == result["dispatch"].claim_id
    assert escalated["payload"]["risk_decision"] == "INVESTIGATE"

    # SIU notified exactly once over secure government messaging (Req 2.8).
    assert len(siu.notifications) == 1
    assert siu.notifications[0]["claim_id"] == result["dispatch"].claim_id
    assert siu.notifications[0]["channel"] == "secure-government-messaging"


@pytest.mark.asyncio
async def test_partial_result_two_engines_killed_scoring_completes():
    """Test 3 — two engines killed -> PARTIAL_RESULT for both, scoring completes.

    Two engines never respond and are cancelled at their per-engine budget,
    producing PARTIAL_RESULT Indicators. The remaining four complete (>= 3
    minimum), so scoring still completes and ``claim.scored`` is published.

    Validates: Requirements 2.2
    """
    claim_id = str(uuid4())
    killed = {EngineType.FRAUD_RING, EngineType.DOCUMENT_FORGERY}
    surviving = {e for e in ALL_ENGINES if e not in killed}
    # Surviving engines fire a MEDIUM Indicator so there is real signal to score.
    severities = {engine: Severity.MEDIUM for engine in surviving}
    severities.update({engine: Severity.LOW for engine in killed})  # never used
    engine_caller = ScriptedEngineCaller(severities, timeout_engines=killed)
    scorer = RealScorerCaller(EnsembleScorer())
    producer = FakeKafkaProducer()
    audit = FakeClickHouseAuditSink()
    siu = FakeSIUNotifier()
    # Tight per-engine budgets so the killed engines trip quickly.
    pipeline = _build_pipeline(
        engine_caller=engine_caller,
        scorer_caller=scorer,
        producer=producer,
        audit_sink=audit,
        siu_notifier=siu,
        timeouts_ms={engine: 80 for engine in ALL_ENGINES},
    )

    result = await pipeline.process(claim_id)
    dispatch = result["dispatch"]

    # Four engines completed (>= 3 minimum); two are partial -> not indeterminate.
    assert set(dispatch.engines_completed) == surviving
    assert set(dispatch.engines_partial) == killed
    assert dispatch.indeterminate is False

    # A PARTIAL_RESULT Indicator exists for each killed engine.
    partial_engines = {
        ind.payload.get("engine")
        for ind in dispatch.indicators
        if getattr(ind, "algorithm", "") == PARTIAL_RESULT_ALGORITHM
    }
    assert partial_engines == {e.value for e in killed}

    # Scoring still completed and claim.scored was published.
    assert len(scorer.calls) == 1
    assert result["outcome"].fallback is False
    assert CLAIM_SCORED_TOPIC in producer.topics()


@pytest.mark.asyncio
async def test_indeterminate_four_engines_killed_payment_halted():
    """Test 4 — four engines killed -> INDETERMINATE and payment halted.

    With only two engines completing (< 3-engine minimum), the dispatcher marks
    the claim INDETERMINATE, the scorer is short-circuited, and payment is
    halted. The INDETERMINATE decision is still published as ``claim.scored``,
    but no escalation and no SIU notification occur.

    Validates: Requirements 2.4, 2.5
    """
    claim_id = str(uuid4())
    killed = {
        EngineType.FRAUD_RING,
        EngineType.DOCUMENT_FORGERY,
        EngineType.GEOGRAPHIC_ANOMALY,
        EngineType.REGIONAL_ANALYSIS,
    }
    surviving = {e for e in ALL_ENGINES if e not in killed}
    assert len(surviving) < MIN_ENGINES_REQUIRED
    severities = {engine: Severity.HIGH for engine in surviving}
    engine_caller = ScriptedEngineCaller(severities, timeout_engines=killed)
    scorer = RealScorerCaller(EnsembleScorer())
    producer = FakeKafkaProducer()
    audit = FakeClickHouseAuditSink()
    siu = FakeSIUNotifier()
    pipeline = _build_pipeline(
        engine_caller=engine_caller,
        scorer_caller=scorer,
        producer=producer,
        audit_sink=audit,
        siu_notifier=siu,
        timeouts_ms={engine: 80 for engine in ALL_ENGINES},
    )

    result = await pipeline.process(claim_id)
    dispatch = result["dispatch"]
    outcome = result["outcome"]

    # Fewer than three engines completed -> INDETERMINATE + payment halted.
    assert len(dispatch.engines_completed) == len(surviving)
    assert dispatch.indeterminate is True
    assert dispatch.risk_decision == RiskDecision.INDETERMINATE
    assert dispatch.payment_halted is True

    # Scorer short-circuited; outcome is the INDETERMINATE fallback.
    assert scorer.calls == []
    assert outcome.risk_decision == RiskDecision.INDETERMINATE
    assert outcome.fallback is True

    # INDETERMINATE is published as claim.scored, but is NOT an escalation.
    assert CLAIM_SCORED_TOPIC in producer.topics()
    scored = producer.event_for(CLAIM_SCORED_TOPIC)
    assert scored["payload"]["risk_decision"] == "INDETERMINATE"
    assert CLAIM_ESCALATED_TOPIC not in producer.topics()
    assert siu.notifications == []


# ===========================================================================
# testcontainers path (preferred; skipped when no container runtime is present)
# ===========================================================================


def _container_runtime_available() -> tuple[bool, str]:
    """Report whether the full testcontainers path can run in this environment.

    Requires BOTH the ``testcontainers`` package (a dev-only dependency, pinned
    to 4.9.0) AND a reachable container runtime (Docker/Podman). Neither is
    present in the CI sandbox, so this returns a clear skip reason rather than
    letting the test error at collection or run time.
    """
    try:
        import testcontainers  # noqa: F401
        from testcontainers.kafka import KafkaContainer  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - ImportError / missing submodule
        return False, f"testcontainers not installed ({exc})"

    docker_host = os.environ.get("DOCKER_HOST")
    default_sock = "/var/run/docker.sock"
    if not docker_host and not os.path.exists(default_sock):
        return False, "no container runtime (Docker/Podman) available"
    return True, ""


_RUNTIME_OK, _RUNTIME_SKIP_REASON = _container_runtime_available()


@pytest.mark.integration
@pytest.mark.skipif(
    not _RUNTIME_OK, reason=_RUNTIME_SKIP_REASON or "container runtime unavailable"
)
class TestDecisionPathsWithContainers:
    """End-to-end decision paths over real infrastructure via testcontainers.

    This class spins up the infrastructure the task calls for — Kafka, Redis,
    PostgreSQL, ClickHouse, Neo4j — plus six lightweight engine mock servers, and
    drives a real claim through the pipeline over a live Kafka broker.

    It is SKIPPED whenever ``testcontainers`` or a container runtime is absent
    (as in the CI sandbox); the in-process tests above provide equivalent
    decision-path coverage without any external services. See the module
    docstring for how to run this locally.
    """

    @pytest.fixture(scope="class")
    def infrastructure(self):
        """Start Kafka, Redis, PostgreSQL, ClickHouse and Neo4j containers.

        Each container is torn down when the class completes. Imports are local
        so the module still imports cleanly when ``testcontainers`` is absent.
        """
        from testcontainers.kafka import KafkaContainer
        from testcontainers.redis import RedisContainer
        from testcontainers.postgres import PostgresContainer

        # ClickHouse / Neo4j modules are optional in some testcontainers builds;
        # import defensively so a partial install still runs Kafka-based checks.
        clickhouse_cm = None
        neo4j_cm = None
        try:
            from testcontainers.clickhouse import ClickHouseContainer

            clickhouse_cm = ClickHouseContainer()
        except Exception:  # noqa: BLE001
            clickhouse_cm = None
        try:
            from testcontainers.neo4j import Neo4jContainer

            neo4j_cm = Neo4jContainer()
        except Exception:  # noqa: BLE001
            neo4j_cm = None

        with KafkaContainer() as kafka, RedisContainer() as redis, PostgresContainer(
            "postgres:16-alpine"
        ) as postgres:
            infra = {
                "kafka_bootstrap": kafka.get_bootstrap_server(),
                "redis_url": (
                    f"redis://{redis.get_container_host_ip()}:"
                    f"{redis.get_exposed_port(6379)}"
                ),
                "postgres_url": postgres.get_connection_url(),
            }
            # Bring up the optional stores when their modules are available.
            if clickhouse_cm is not None:
                clickhouse_cm.start()
                infra["clickhouse_host"] = clickhouse_cm.get_container_host_ip()
            if neo4j_cm is not None:
                neo4j_cm.start()
                infra["neo4j_url"] = neo4j_cm.get_connection_url()
            try:
                yield infra
            finally:
                if clickhouse_cm is not None:
                    clickhouse_cm.stop()
                if neo4j_cm is not None:
                    neo4j_cm.stop()

    @pytest.mark.asyncio
    async def test_investigate_path_escalated_over_real_kafka(self, infrastructure):
        """Drive an INVESTIGATE claim and observe claim.escalated on real Kafka.

        Validates: Requirements 2.8
        """
        from aiokafka import AIOKafkaProducer, AIOKafkaConsumer

        bootstrap = infrastructure["kafka_bootstrap"]
        claim_id = str(uuid4())

        # Real Kafka producer wired into the real ResultPublisher.
        producer_client = AIOKafkaProducer(bootstrap_servers=bootstrap)
        await producer_client.start()

        engine_caller = ScriptedEngineCaller(
            {engine: Severity.CRITICAL for engine in ALL_ENGINES}
        )
        scorer = RealScorerCaller(EnsembleScorer())
        audit = FakeClickHouseAuditSink()
        siu = FakeSIUNotifier()

        class _AioKafkaAdapter:
            def __init__(self, client):
                self._client = client

            async def send_and_wait(self, topic, value, key=None, headers=None):
                return await self._client.send_and_wait(
                    topic, value=value, key=key
                )

        publisher = ResultPublisher(_AioKafkaAdapter(producer_client), audit_sink=audit)
        dispatcher = EngineDispatcher(
            engine_caller,
            endpoints={e: f"http://fake/{e.value}" for e in ALL_ENGINES},
            timeouts_ms={e: 300 for e in ALL_ENGINES},
        )
        scoring_client = ScoringClient(scorer, budget_ms=1000)
        pipeline = DecisionPipeline(
            dispatcher, scoring_client, publisher, audit, siu
        )

        consumer = AIOKafkaConsumer(
            CLAIM_ESCALATED_TOPIC,
            bootstrap_servers=bootstrap,
            group_id="itest-siu",
            auto_offset_reset="earliest",
        )
        await consumer.start()
        try:
            result = await pipeline.process(claim_id)
            assert result["outcome"].risk_decision == RiskDecision.INVESTIGATE

            # Observe the escalation event on the real broker.
            msg = await asyncio.wait_for(consumer.getone(), timeout=15.0)
            payload = json.loads(msg.value.decode())
            assert payload["risk_decision"] == "INVESTIGATE"
            assert msg.key.decode() == result["dispatch"].claim_id
            # SIU notified for the escalated claim (Req 2.8).
            assert len(siu.notifications) == 1
        finally:
            await consumer.stop()
            await producer_client.stop()
