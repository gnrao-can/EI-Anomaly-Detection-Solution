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

The dispatch callback here is a placeholder hook: parallel engine dispatch is
task 6.2. For 6.1 it simply records that a pairing was detected so the
correlation-and-trigger path is observable end to end.

Requirements: 2.1
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
except ImportError:  # pragma: no cover - package-style import
    from services.pipeline_orchestrator.config import get_settings  # type: ignore
    from services.pipeline_orchestrator.correlator import (  # type: ignore
        EventCorrelator,
        InMemoryPairingStore,
        PairResult,
        RedisPairingStore,
    )
    from services.pipeline_orchestrator.consumer import OrchestratorConsumer  # type: ignore

logger = logging.getLogger("pipeline-orchestrator")


async def _default_dispatch(result: PairResult) -> None:
    """Placeholder dispatch hook (task 6.2 replaces this with engine dispatch).

    For 6.1 we only log that both events were correlated for the claim so the
    correlation -> trigger path is observable.
    """
    logger.info(
        "claim %s paired; ready for parallel engine dispatch (later_ts=%s)",
        result.claim_id,
        result.later_timestamp.isoformat(),
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

    # ── Kafka consumer ──────────────────────────────────────────
    consumer = OrchestratorConsumer(
        correlator,
        _default_dispatch,
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


__all__ = ["app", "lifespan"]
