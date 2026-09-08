"""
services/pipeline-orchestrator/main.py
======================================
Detection Pipeline Orchestrator FastAPI application (task 6.1).

Wires together the pieces of the orchestrator:

  * A lifespan handler that opens a Redis connection (the event-pairing store),
    builds the :class:`EventCorrelator`, and starts the Kafka
    :class:`OrchestratorConsumer` subscribed to ``claim.received`` and
    ``roe.received``. All of these degrade gracefully in local development when
    the backing systems are unavailable.
  * ``GET /healthz`` — reports Kafka and Redis connectivity (200 when healthy,
    503 when degraded).

On each correlated pair the consumer invokes :class:`OrchestratorPipeline`,
which runs the real chain — parallel engine dispatch (:class:`EngineDispatcher`)
-> Ensemble Scorer (:class:`ScoringClient`) -> publish ``claim.scored`` /
``claim.escalated`` (:class:`ResultPublisher`) — matching the verified Slice 1
integration flow. (This replaced an earlier logging-only placeholder that paired
events but produced no decision.)

Requirements: 2.1, 2.2, 2.3, 2.5, 2.6, 2.7
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse

try:  # sibling modules imported by bare name in the service directory
    from config import get_settings  # type: ignore
    from correlator import (  # type: ignore
        EventCorrelator,
        InMemoryPairingStore,
        PairResult,
        RedisPairingStore,
    )
    from consumer import OrchestratorConsumer  # type: ignore
    from engine_dispatch import EngineDispatcher  # type: ignore
    from scoring import ScoringClient  # type: ignore
    from publisher import ResultPublisher, AIOKafkaProducerAdapter  # type: ignore
except ImportError:  # pragma: no cover - package-style import
    from services.pipeline_orchestrator.config import get_settings  # type: ignore
    from services.pipeline_orchestrator.correlator import (  # type: ignore
        EventCorrelator,
        InMemoryPairingStore,
        PairResult,
        RedisPairingStore,
    )
    from services.pipeline_orchestrator.consumer import OrchestratorConsumer  # type: ignore
    from services.pipeline_orchestrator.engine_dispatch import EngineDispatcher  # type: ignore
    from services.pipeline_orchestrator.scoring import ScoringClient  # type: ignore
    from services.pipeline_orchestrator.publisher import (  # type: ignore
        ResultPublisher,
        AIOKafkaProducerAdapter,
    )

logger = logging.getLogger("pipeline-orchestrator")


class OrchestratorPipeline:
    """The real correlation -> dispatch -> score -> publish chain (Req 2.1-2.7).

    Holds the three cooperating components built once at startup and drives them
    per paired claim, exactly as the verified Slice 1 integration test wires
    them (tests/integration/test_slice1_end_to_end.py):

      * ``EngineDispatcher`` -- POSTs all six engines concurrently over HTTP
        (endpoints + per-engine budgets from env / design defaults), collecting
        Indicators and applying the >= 3-engine minimum (INDETERMINATE otherwise).
      * ``ScoringClient`` -- POSTs the collected Indicators to the Ensemble
        Scorer within the 300 ms budget (skipped on the INDETERMINATE path).
      * ``ResultPublisher`` -- publishes ``claim.scored`` (and ``claim.escalated``
        for INVESTIGATE) to Kafka, keyed by ``claim_id``.

    This is the service-level wiring that the previous ``_default_dispatch``
    placeholder omitted; without it the deployed orchestrator paired events but
    never produced a decision.
    """

    def __init__(
        self,
        dispatcher: EngineDispatcher,
        scoring_client: ScoringClient,
        publisher: ResultPublisher,
    ) -> None:
        self._dispatcher = dispatcher
        self._scoring = scoring_client
        self._publisher = publisher

    async def dispatch(self, pair: PairResult) -> None:
        """Run the full chain for one paired claim (Req 2.2-2.7)."""
        result = await self._dispatcher.dispatch(pair.claim_payload, pair.roe_payload)
        outcome = await self._scoring.score(
            result.indicators,
            result.claim_id,
            result.engines_completed,
            result.engines_partial,
            indeterminate=result.indeterminate,
        )
        await self._publisher.publish_scored(result.claim_id, outcome)
        logger.info(
            "claim %s scored: decision=%s (completed=%d, partial=%d)",
            result.claim_id,
            getattr(outcome.risk_decision, "value", outcome.risk_decision),
            len(result.engines_completed),
            len(result.engines_partial),
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open Redis, build the correlator, and start the Kafka consumer.

    Each dependency is best-effort in development: a failure to connect leaves
    the correlator using an in-memory pairing store and/or leaves the consumer
    inert, surfaced later via ``/healthz`` rather than blocking startup.
    """
    settings = get_settings()

    app.state.settings = settings
    app.state.redis_cache = None
    app.state.correlator = None
    app.state.consumer = None
    app.state.kafka_producer = None
    app.state.pipeline = None

    # ── Redis pairing store ─────────────────────────────────────
    pairing_store: Any = None
    try:
        from shared.cache import RedisCache

        cache = RedisCache(
            url=settings.redis_url,
            cluster_mode=settings.redis_cluster_mode,
            password=settings.redis_password,
        )
        await cache.connect()
        app.state.redis_cache = cache
        pairing_store = RedisPairingStore(cache)
    except Exception as exc:  # noqa: BLE001 - degrade to in-memory in dev
        logger.warning("Redis unavailable, using in-memory pairing store: %s", exc)
        pairing_store = InMemoryPairingStore()

    correlator = EventCorrelator(
        pairing_store, ttl_seconds=settings.correlation_timeout_seconds
    )
    app.state.correlator = correlator

    # ── Real dispatch chain: engines -> scorer -> publish (Req 2.2-2.7) ──
    # EngineDispatcher/ScoringClient default their endpoints from env
    # (ORCHESTRATOR_ENGINE_<ENGINE>_URL, ORCHESTRATOR_ENSEMBLE_SCORER_URL) and
    # present the ORCHESTRATOR_SERVICE_TOKEN Bearer on internal calls (Req 19.7).
    # The Kafka producer is created here and reused for every published event.
    kafka_producer = AIOKafkaProducerAdapter(
        bootstrap_servers=settings.kafka_bootstrap_servers
    )
    app.state.kafka_producer = kafka_producer
    pipeline = OrchestratorPipeline(
        dispatcher=EngineDispatcher(),
        scoring_client=ScoringClient(),
        publisher=ResultPublisher(kafka_producer),
    )
    app.state.pipeline = pipeline

    # ── Kafka consumer ──────────────────────────────────────────
    consumer = OrchestratorConsumer(
        correlator,
        pipeline.dispatch,
        bootstrap_servers=settings.kafka_bootstrap_servers,
        topics=(settings.kafka_claim_topic, settings.kafka_roe_topic),
        group_id=settings.kafka_consumer_group,
        dispatch_budget_ms=settings.dispatch_budget_ms,
        degrade_gracefully=settings.kafka_degrade_gracefully,
    )
    await consumer.start()
    app.state.consumer = consumer

    try:
        yield
    finally:
        if app.state.consumer is not None:
            try:
                await app.state.consumer.stop()
            except Exception:  # noqa: BLE001
                pass
        producer = getattr(app.state, "kafka_producer", None)
        if producer is not None:
            try:
                await producer.stop()
            except Exception:  # noqa: BLE001
                pass
        if app.state.redis_cache is not None:
            try:
                await app.state.redis_cache.close()
            except Exception:  # noqa: BLE001
                pass


app = FastAPI(
    title="EI-ADS Detection Pipeline Orchestrator",
    version="0.1.0",
    lifespan=lifespan,
)

# ── OpenTelemetry distributed tracing (shared.telemetry, task 32.1 / Req 21.5) ──
# Single, self-contained hook: all tracer setup and span helpers live in
# shared/telemetry.py. The orchestrator opens the root ``claim.received`` span
# (shared.telemetry.root_claim_span) and propagates trace context to the engines
# via Kafka headers (shared.telemetry.inject_trace_context). No-op when the
# OpenTelemetry packages are absent.
from shared.telemetry import instrument_tracing  # noqa: E402

instrument_tracing(app, service_name="pipeline-orchestrator")
# ── end OpenTelemetry tracing ──

# --- Prometheus metrics + /metrics endpoint (task 31.1) ---
from shared.metrics import instrument_app  # noqa: E402

instrument_app(app, kind="service")
# --- end metrics wiring ---


async def _check_redis(app_: FastAPI) -> bool:
    cache = getattr(app_.state, "redis_cache", None)
    if cache is None:
        return False
    try:
        client = await cache._get_client()  # noqa: SLF001 - internal ping
        return bool(await client.ping())
    except Exception:  # noqa: BLE001
        return False


def _check_kafka(app_: FastAPI) -> bool:
    consumer = getattr(app_.state, "consumer", None)
    return bool(consumer is not None and consumer.is_connected)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Report Kafka / Redis connectivity for the orchestrator.

    Returns 200 when both are connected, otherwise 503. In local development
    where dependencies are intentionally absent, the endpoint reports each
    dependency's status.
    """
    checks = {
        "kafka": _check_kafka(app),
        "redis": await _check_redis(app),
    }
    healthy = all(checks.values())
    code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(
        status_code=code,
        content={"status": "ok" if healthy else "degraded", "checks": checks},
    )


__all__ = ["app", "lifespan", "OrchestratorPipeline"]
