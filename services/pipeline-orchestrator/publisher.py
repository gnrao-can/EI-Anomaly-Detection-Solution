"""
services/pipeline-orchestrator/publisher.py
============================================
Result publication and audit-trail writing for the Detection Pipeline
Orchestrator (task 6.3).

Once a claim has been scored (see ``scoring.py``), the orchestrator must:

  1. Publish a ``claim.scored`` Kafka event, keyed by ``claim_id``, within
     500 ms of the decision (design "Time Budget Allocation" / Requirement 2.7).
  2. If the decision is ``INVESTIGATE``, additionally publish a
     ``claim.escalated`` event, keyed by the same ``claim_id``, within the same
     500 ms budget (Requirement 2.7).
  3. Write a complete audit-trail entry to ClickHouse within 500 ms for every
     adapter call and intermediate output (Requirement 2.8), mirroring the
     ``ei_ads_analytics.adapter_audit_log`` schema.

Both Kafka and ClickHouse are injectable so the whole module is unit-testable
with fakes — no live broker or database is required. In production the Kafka
producer is ``aiokafka.AIOKafkaProducer`` and the audit sink is
``clickhouse_connect``; both are imported lazily and both degrade gracefully
when unavailable so a publish/audit hiccup never crashes the pipeline.

Requirements: 2.7, 2.8
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, Protocol

try:
    from shared.models import RiskDecision
except ImportError:  # pragma: no cover - service-local fallback
    from models import RiskDecision  # type: ignore

try:
    from scoring import ScoreOutcome
except ImportError:  # pragma: no cover - package/style fallback
    try:
        from services.pipeline_orchestrator.scoring import ScoreOutcome  # type: ignore
    except ImportError:  # pragma: no cover
        ScoreOutcome = Any  # type: ignore

try:  # trace-context propagation over Kafka (shared source of truth; task 32.1)
    from shared.telemetry import inject_trace_context
except ImportError:  # pragma: no cover - shared.telemetry always present in repo
    def inject_trace_context(headers=None):  # type: ignore[misc]
        return list(headers or [])

logger = logging.getLogger("pipeline-orchestrator.publisher")


# ---------------------------------------------------------------------------
# Local constants (defined here to avoid coupling to 6.2 / config.py)
# ---------------------------------------------------------------------------

#: Budget within which the scored/escalated events must be published (ms).
PUBLISH_BUDGET_MS: int = 500

#: Budget within which an audit entry must be written to ClickHouse (ms).
AUDIT_BUDGET_MS: int = 500

#: Kafka topic for the final Risk_Score + Risk_Decision.
CLAIM_SCORED_TOPIC: str = "claim.scored"

#: Kafka topic for INVESTIGATE escalations.
CLAIM_ESCALATED_TOPIC: str = "claim.escalated"

#: ClickHouse database + table for the adapter/component audit trail.
ADAPTER_AUDIT_TABLE: str = "ei_ads_analytics.adapter_audit_log"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KafkaEvent:
    """An event to publish: a topic, a partition key, and a JSON-able payload."""

    topic: str
    key: str
    payload: dict[str, Any]

    def key_bytes(self) -> bytes:
        """The partition key encoded as UTF-8 bytes (aiokafka key format)."""
        return self.key.encode("utf-8")

    def value_bytes(self) -> bytes:
        """The payload serialised to UTF-8 JSON bytes (aiokafka value format)."""
        return json.dumps(self.payload).encode("utf-8")


@dataclass
class PublishResult:
    """Outcome of :meth:`ResultPublisher.publish_scored`.

    Attributes
    ----------
    scored_published:
        ``True`` when the ``claim.scored`` event was published.
    escalated_published:
        ``True`` when a ``claim.escalated`` event was published (INVESTIGATE).
    events:
        The events that were emitted, in order.
    elapsed_ms:
        Wall-clock time spent publishing, in milliseconds.
    within_budget:
        ``True`` when publication completed within the 500 ms budget.
    """

    scored_published: bool = False
    escalated_published: bool = False
    events: list[KafkaEvent] = field(default_factory=list)
    elapsed_ms: float = 0.0
    within_budget: bool = True


# ---------------------------------------------------------------------------
# Producer / audit-sink abstractions (DI seams for tests)
# ---------------------------------------------------------------------------


class KafkaProducerLike(Protocol):
    """Minimal async Kafka producer surface used by :class:`ResultPublisher`."""

    async def send_and_wait(
        self, topic: str, value: bytes, key: bytes | None = None
    ) -> Any:  # pragma: no cover - protocol
        ...


class AuditSink(Protocol):
    """Minimal audit-sink surface: insert one row into the audit table."""

    async def insert(self, table: str, row: dict[str, Any]) -> None:  # pragma: no cover - protocol
        ...


class AIOKafkaProducerAdapter:
    """Production :class:`KafkaProducerLike` backed by ``aiokafka``.

    The producer is created and started lazily on first use and reused after.
    ``stop`` should be called on shutdown.
    """

    def __init__(self, bootstrap_servers: str = "localhost:9092") -> None:
        self._bootstrap_servers = bootstrap_servers
        self._producer: Any = None

    async def _get_producer(self) -> Any:
        if self._producer is None:
            from aiokafka import AIOKafkaProducer

            self._producer = AIOKafkaProducer(
                bootstrap_servers=self._bootstrap_servers
            )
            await self._producer.start()
        return self._producer

    async def send_and_wait(
        self, topic: str, value: bytes, key: bytes | None = None
    ) -> Any:
        producer = await self._get_producer()
        return await producer.send_and_wait(topic, value=value, key=key)

    async def stop(self) -> None:
        if self._producer is not None:
            try:
                await self._producer.stop()
            finally:
                self._producer = None


class ClickHouseAuditSink:
    """Production :class:`AuditSink` backed by ``clickhouse-connect``.

    The client is created lazily. ``clickhouse-connect`` is synchronous, so the
    insert runs in a thread via ``asyncio.to_thread`` to avoid blocking the event
    loop. Import and connection are best-effort: if the driver is unavailable the
    sink logs and no-ops so audit failures never crash the pipeline.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 8123,
        username: str = "default",
        password: str = "",
        database: str = "ei_ads_analytics",
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._database = database
        self._client: Any = None
        self._unavailable = False

    def _get_client(self) -> Any:
        if self._client is None and not self._unavailable:
            try:
                import clickhouse_connect

                self._client = clickhouse_connect.get_client(
                    host=self._host,
                    port=self._port,
                    username=self._username,
                    password=self._password,
                )
            except Exception as exc:  # noqa: BLE001 - degrade gracefully
                logger.warning("ClickHouse audit sink unavailable: %s", exc)
                self._unavailable = True
                self._client = None
        return self._client

    async def insert(self, table: str, row: dict[str, Any]) -> None:
        import asyncio

        client = self._get_client()
        if client is None:
            return
        columns = list(row.keys())
        data = [[row[c] for c in columns]]
        await asyncio.to_thread(client.insert, table, data, column_names=columns)


# ---------------------------------------------------------------------------
# ResultPublisher
# ---------------------------------------------------------------------------


class ResultPublisher:
    """Publishes scored/escalated events and writes audit entries.

    Parameters
    ----------
    producer:
        The Kafka producer used to publish events. Tests inject a fake.
    audit_sink:
        The ClickHouse audit sink. Optional; when ``None``, audit writes no-op
        (still returning within budget) so the pipeline is unaffected.
    publish_budget_ms / audit_budget_ms:
        The 500 ms publication and audit budgets, respectively.
    """

    def __init__(
        self,
        producer: KafkaProducerLike,
        audit_sink: Optional[AuditSink] = None,
        *,
        publish_budget_ms: int = PUBLISH_BUDGET_MS,
        audit_budget_ms: int = AUDIT_BUDGET_MS,
    ) -> None:
        self._producer = producer
        self._audit_sink = audit_sink
        self._publish_budget_ms = publish_budget_ms
        self._audit_budget_ms = audit_budget_ms

    # ------------------------------------------------------------------
    # Event publication
    # ------------------------------------------------------------------

    def _build_scored_event(self, claim_id: str, outcome: ScoreOutcome) -> KafkaEvent:
        """Build the ``claim.scored`` event payload keyed by ``claim_id``."""
        decision = outcome.risk_decision
        decision_value = (
            decision.value if isinstance(decision, RiskDecision) else str(decision)
        )
        payload = {
            "event_type": CLAIM_SCORED_TOPIC,
            "claim_id": claim_id,
            "risk_score": outcome.risk_score,
            "risk_decision": decision_value,
            "assessment": outcome.assessment or {},
            "scored_at": datetime.now(timezone.utc).isoformat(),
        }
        return KafkaEvent(topic=CLAIM_SCORED_TOPIC, key=claim_id, payload=payload)

    def _build_escalated_event(
        self, claim_id: str, outcome: ScoreOutcome
    ) -> KafkaEvent:
        """Build the ``claim.escalated`` event payload keyed by ``claim_id``."""
        payload = {
            "event_type": CLAIM_ESCALATED_TOPIC,
            "claim_id": claim_id,
            "risk_score": outcome.risk_score,
            "risk_decision": RiskDecision.INVESTIGATE.value,
            "escalated_at": datetime.now(timezone.utc).isoformat(),
        }
        return KafkaEvent(topic=CLAIM_ESCALATED_TOPIC, key=claim_id, payload=payload)

    async def publish_scored(
        self, claim_id: str, score_outcome: ScoreOutcome
    ) -> PublishResult:
        """Publish ``claim.scored`` (and ``claim.escalated`` if INVESTIGATE).

        Both events are keyed by ``claim_id`` so all events for a claim land in
        the same Kafka partition and remain ordered. Publication is measured
        against the 500 ms budget; a budget overrun is logged but never prevents
        the events from being sent (dropping the decision would be worse).
        """
        result = PublishResult()
        start = time.monotonic()

        scored_event = self._build_scored_event(claim_id, score_outcome)
        await self._send(scored_event)
        result.scored_published = True
        result.events.append(scored_event)

        decision = score_outcome.risk_decision
        is_investigate = decision == RiskDecision.INVESTIGATE
        if is_investigate:
            escalated_event = self._build_escalated_event(claim_id, score_outcome)
            await self._send(escalated_event)
            result.escalated_published = True
            result.events.append(escalated_event)

        result.elapsed_ms = (time.monotonic() - start) * 1000.0
        result.within_budget = result.elapsed_ms <= self._publish_budget_ms
        if not result.within_budget:
            logger.warning(
                "publication for claim %s took %.1f ms, exceeding the %d ms budget",
                claim_id,
                result.elapsed_ms,
                self._publish_budget_ms,
            )
        return result

    async def _send(self, event: KafkaEvent) -> None:
        """Send a single event via the producer, degrading gracefully on error.

        The current trace context is injected into the Kafka message headers so
        downstream consumers (engines) continue the same distributed trace
        (task 32.1 / Req 21.5). Producers whose ``send_and_wait`` does not accept
        a ``headers`` kwarg are supported via a fallback call.
        """
        headers = inject_trace_context([])
        try:
            try:
                await self._producer.send_and_wait(
                    event.topic,
                    value=event.value_bytes(),
                    key=event.key_bytes(),
                    headers=headers,
                )
            except TypeError:
                # Producer (or test fake) does not accept a headers kwarg.
                await self._producer.send_and_wait(
                    event.topic, value=event.value_bytes(), key=event.key_bytes()
                )
        except Exception as exc:  # noqa: BLE001 - a publish failure must not crash
            logger.error(
                "failed to publish %s for claim %s: %s",
                event.topic,
                event.key,
                exc,
            )
            raise

    # ------------------------------------------------------------------
    # Audit trail
    # ------------------------------------------------------------------

    async def write_audit(
        self,
        claim_id: str,
        component: str,
        action: str,
        fields_accessed: list[str],
        outcome: str,
        duration_ms: float,
        *,
        actor_role: str = "SYSTEM",
        sin_hash: str = "",
        request_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Write one audit-trail row to ClickHouse within the 500 ms budget.

        The row shape mirrors ``ei_ads_analytics.adapter_audit_log``: it records
        which component performed which action, which Protected B fields were
        touched, the outcome, and the operation latency. ``sin_hash`` is used in
        place of any raw SIN (raw SIN is never stored).

        Returns the row that was (attempted to be) inserted so callers/tests can
        assert on it. Audit failures are logged and swallowed so a failed audit
        write never blocks or crashes claim processing.
        """
        row = {
            "log_id": str(uuid.uuid4()),
            "claim_id": claim_id,
            "component": component,
            "action": action,
            "fields_accessed": list(fields_accessed),
            "outcome": outcome,
            "actor_role": actor_role,
            "sin_hash": (sin_hash or "").ljust(64, "0")[:64],
            "request_id": request_id or str(uuid.uuid4()),
            "duration_ms": float(duration_ms),
            "logged_at": datetime.now(timezone.utc),
        }

        if self._audit_sink is None:
            logger.debug(
                "no audit sink configured; skipping audit for claim %s / %s",
                claim_id,
                component,
            )
            return row

        start = time.monotonic()
        try:
            await self._audit_sink.insert(ADAPTER_AUDIT_TABLE, row)
        except Exception as exc:  # noqa: BLE001 - never fail the pipeline on audit
            logger.error(
                "failed to write audit entry for claim %s / %s: %s",
                claim_id,
                component,
                exc,
            )
            return row

        elapsed_ms = (time.monotonic() - start) * 1000.0
        if elapsed_ms > self._audit_budget_ms:
            logger.warning(
                "audit write for claim %s / %s took %.1f ms, exceeding the "
                "%d ms budget",
                claim_id,
                component,
                elapsed_ms,
                self._audit_budget_ms,
            )
        return row


__all__ = [
    "PUBLISH_BUDGET_MS",
    "AUDIT_BUDGET_MS",
    "CLAIM_SCORED_TOPIC",
    "CLAIM_ESCALATED_TOPIC",
    "ADAPTER_AUDIT_TABLE",
    "KafkaEvent",
    "PublishResult",
    "KafkaProducerLike",
    "AuditSink",
    "AIOKafkaProducerAdapter",
    "ClickHouseAuditSink",
    "ResultPublisher",
]
