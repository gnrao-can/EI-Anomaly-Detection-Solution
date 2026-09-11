"""
services/pipeline-orchestrator/consumer.py
==========================================
Kafka consumer loop for the Detection Pipeline Orchestrator (task 6.1).

Subscribes to the two intake topics — ``claim.received`` and ``roe.received`` —
and, for every message, hands the event to an :class:`EventCorrelator`. When the
correlator returns a :class:`PairResult` (both events for a claim have arrived),
the consumer invokes a *dispatch callback* within the configured dispatch budget
(100 ms of the later event's timestamp). The dispatch callback is the seam for
task 6.2 (parallel engine dispatch); here we only define and honour the hook.

Design notes
------------
* Events for a given ``claim_id`` are published with ``claim_id`` as the Kafka
  partition key, so they are totally ordered within a partition. Correlation
  itself, however, does not rely on ordering — either event may arrive first.
* The loop degrades gracefully when Kafka is unavailable in development
  (``kafka_degrade_gracefully``): construction/start failures are logged and the
  consumer becomes a no-op rather than crashing the service.

Requirements: 2.1
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

try:  # local package layout when run from the service directory
    from correlator import EventCorrelator, PairResult
except ImportError:  # pragma: no cover - package-style import
    from services.pipeline_orchestrator.correlator import (  # type: ignore
        EventCorrelator,
        PairResult,
    )

try:  # OpenTelemetry span helpers (shared source of truth; task 32.1 / Req 21.5)
    from shared.telemetry import root_claim_span
except ImportError:  # pragma: no cover - shared.telemetry always present in repo
    from contextlib import contextmanager as _contextmanager

    @_contextmanager
    def root_claim_span(*_args, **_kwargs):  # type: ignore[misc]
        yield None

logger = logging.getLogger("pipeline-orchestrator.consumer")

#: Initial Kafka-connect retry parameters. On a compose bring-up the broker is
#: often not ready for a few seconds after the orchestrator starts; retrying
#: with exponential backoff lets the consumer attach itself once Kafka is up,
#: so no manual restart is needed. ~10 attempts (1s,2s,4s,8s,10s,...) covers a
#: typical broker start_period without blocking startup indefinitely.
_KAFKA_CONNECT_MAX_ATTEMPTS: int = 10
_KAFKA_CONNECT_INITIAL_DELAY_S: float = 1.0
_KAFKA_CONNECT_MAX_DELAY_S: float = 10.0

#: A dispatch callback receives the completed pairing and triggers engine work.
DispatchCallback = Callable[[PairResult], Awaitable[None]]

__all__ = ["DispatchCallback", "OrchestratorConsumer", "extract_event"]


def extract_event(raw_value: bytes | str | dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """Normalise a Kafka message value to ``(event_type, claim_id, payload)``.

    Accepts raw bytes/str JSON or an already-decoded dict. The envelope shape
    matches what the Claim Intake Service and ROE Adapter publish::

        {"event_type": "claim.received", "claim_id": "...", "payload": {...}}

    The returned ``payload`` always carries a ``received_at`` timestamp: if the
    envelope payload omits one, the envelope-level ``received_at`` / ``timestamp``
    is copied in so the correlator can compute the later timestamp.
    """
    if isinstance(raw_value, (bytes, bytearray)):
        envelope = json.loads(raw_value.decode("utf-8"))
    elif isinstance(raw_value, str):
        envelope = json.loads(raw_value)
    else:
        envelope = dict(raw_value)

    event_type = envelope.get("event_type", "")
    claim_id = envelope.get("claim_id") or envelope.get("payload", {}).get("claim_id", "")
    payload = dict(envelope.get("payload") or {})

    if "received_at" not in payload:
        envelope_ts = envelope.get("received_at") or envelope.get("timestamp")
        if envelope_ts is not None:
            payload["received_at"] = envelope_ts

    return event_type, str(claim_id), payload


class OrchestratorConsumer:
    """aiokafka consumer that correlates intake events and triggers dispatch.

    Parameters
    ----------
    correlator:
        The :class:`EventCorrelator` used to pair events by ``claim_id``.
    dispatch_callback:
        Async callback invoked once per completed pairing. This is the hook for
        parallel engine dispatch (task 6.2). It is scheduled so that it starts
        within ``dispatch_budget_ms`` of the later event timestamp.
    bootstrap_servers:
        Kafka bootstrap servers string.
    topics:
        The topics to subscribe to (defaults to the two intake topics).
    group_id:
        The consumer group id.
    dispatch_budget_ms:
        Budget (ms) within which dispatch must be triggered after the later
        event timestamp. Defaults to 100 ms.
    degrade_gracefully:
        When ``True`` (dev default), a Kafka connection failure is logged and the
        consumer becomes inert rather than raising.
    """

    def __init__(
        self,
        correlator: EventCorrelator,
        dispatch_callback: DispatchCallback,
        *,
        bootstrap_servers: str = "localhost:9092",
        topics: tuple[str, ...] = ("claim.received", "roe.received"),
        group_id: str = "pipeline-orchestrator",
        dispatch_budget_ms: int = 100,
        degrade_gracefully: bool = True,
    ) -> None:
        self._correlator = correlator
        self._dispatch_callback = dispatch_callback
        self._bootstrap_servers = bootstrap_servers
        self._topics = topics
        self._group_id = group_id
        self._dispatch_budget_ms = dispatch_budget_ms
        self._degrade_gracefully = degrade_gracefully

        self._consumer: Any = None
        self._task: Optional[asyncio.Task[None]] = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> bool:
        """Create/start the aiokafka consumer and begin the consume loop.

        Retries the initial Kafka connection with exponential backoff so a broker
        that is not yet ready at startup (a common compose bring-up race) is
        awaited rather than causing permanent, silent degradation. Only after
        :data:`_KAFKA_CONNECT_MAX_ATTEMPTS` failed attempts does graceful
        degradation kick in (the consumer is left inert). This removes the need
        to manually restart the orchestrator after Kafka comes up.

        Returns ``True`` when the consumer started; ``False`` when Kafka was
        unavailable after all retries and graceful degradation is enabled.
        """
        try:
            from aiokafka import AIOKafkaConsumer
        except Exception as exc:  # noqa: BLE001 - aiokafka import failure
            if self._degrade_gracefully:
                logger.warning("aiokafka unavailable, degrading gracefully: %s", exc)
                self._consumer = None
                return False
            raise

        delay = _KAFKA_CONNECT_INITIAL_DELAY_S
        last_exc: Optional[Exception] = None
        for attempt in range(1, _KAFKA_CONNECT_MAX_ATTEMPTS + 1):
            try:
                self._consumer = AIOKafkaConsumer(
                    *self._topics,
                    bootstrap_servers=self._bootstrap_servers,
                    group_id=self._group_id,
                    enable_auto_commit=True,
                    auto_offset_reset="earliest",
                )
                await self._consumer.start()
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001 - any connection failure
                last_exc = exc
                # Best-effort cleanup of the half-built consumer before retrying.
                try:
                    if self._consumer is not None:
                        await self._consumer.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._consumer = None
                if attempt < _KAFKA_CONNECT_MAX_ATTEMPTS:
                    logger.warning(
                        "Kafka consumer connect attempt %d/%d failed (%s); "
                        "retrying in %.1fs",
                        attempt,
                        _KAFKA_CONNECT_MAX_ATTEMPTS,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, _KAFKA_CONNECT_MAX_DELAY_S)

        if last_exc is not None:
            if self._degrade_gracefully:
                logger.warning(
                    "Kafka consumer unavailable after %d attempts, degrading "
                    "gracefully: %s",
                    _KAFKA_CONNECT_MAX_ATTEMPTS,
                    last_exc,
                )
                self._consumer = None
                return False
            raise last_exc

        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("orchestrator consumer started on topics %s", self._topics)
        return True

    async def stop(self) -> None:
        """Stop the consume loop and the underlying consumer."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self._consumer is not None:
            try:
                await self._consumer.stop()
            finally:
                self._consumer = None
        logger.info("orchestrator consumer stopped")

    @property
    def is_connected(self) -> bool:
        """``True`` when a live Kafka consumer is attached."""
        return self._consumer is not None

    # ------------------------------------------------------------------
    # Consume loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        """Iterate Kafka messages and process each until stopped."""
        assert self._consumer is not None
        try:
            async for message in self._consumer:
                if not self._running:
                    break
                await self.handle_message(message.value)
        except asyncio.CancelledError:  # pragma: no cover - normal shutdown
            raise
        except Exception as exc:  # noqa: BLE001 - keep the loop resilient
            logger.error("orchestrator consume loop error: %s", exc)

    async def handle_message(self, raw_value: bytes | str | dict[str, Any]) -> Optional[PairResult]:
        """Process a single message value: correlate and dispatch on a pairing.

        This method is public so it can be unit-tested directly without a live
        Kafka broker. Returns the :class:`PairResult` when the message completed
        a pairing (and dispatch was triggered), else ``None``.
        """
        try:
            event_type, claim_id, payload = extract_event(raw_value)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("skipping malformed message: %s", exc)
            return None

        if not event_type or not claim_id:
            logger.warning("skipping message missing event_type/claim_id")
            return None

        try:
            result = await self._correlator.on_event(event_type, claim_id, payload)
        except ValueError as exc:
            logger.warning("ignoring unknown event: %s", exc)
            return None

        if result is None:
            return None

        # Root ``claim.received`` span for the whole pipeline (design trace
        # structure). Only claim_id / sin_hash are attached — never raw PII.
        # No-op when OpenTelemetry is unavailable. Child spans (engine.*,
        # adapter.*, shap.*, llm.*) attach to this via HTTP/Kafka propagation.
        sin_hash = str(payload.get("sin_hash", "") or "")
        with root_claim_span(claim_id, sin_hash):
            await self._trigger_dispatch(result)
        return result

    async def _trigger_dispatch(self, result: PairResult) -> None:
        """Invoke the dispatch callback within the dispatch budget.

        The budget is measured from ``result.later_timestamp``: dispatch must be
        triggered within ``dispatch_budget_ms`` of the later of the two event
        timestamps (design: 100 ms pipeline initiation budget). We compute the
        remaining budget and, if any remains, allow it; the callback is then
        awaited. If we are already past budget we log and dispatch immediately so
        the claim is never dropped.
        """
        now = datetime.now(timezone.utc)
        elapsed_ms = (now - result.later_timestamp).total_seconds() * 1000.0
        if elapsed_ms > self._dispatch_budget_ms:
            logger.warning(
                "dispatch for claim %s triggered %0.1f ms after later event "
                "(budget %d ms)",
                result.claim_id,
                elapsed_ms,
                self._dispatch_budget_ms,
            )
        try:
            await self._dispatch_callback(result)
        except Exception as exc:  # noqa: BLE001 - a dispatch failure must not kill the loop
            logger.error("dispatch callback failed for claim %s: %s", result.claim_id, exc)
