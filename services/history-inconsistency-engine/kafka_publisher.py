"""
services/history-inconsistency-engine/kafka_publisher.py
=========================================================
Indicator publication for the Employment History Inconsistency Engine
(task 14.2).

Design ("Employment History Inconsistency Engine", step 6) and Requirement 6.9
require the engine to publish all produced Indicators to the Kafka topic
``indicators.history-inconsistency`` within a 500 ms wall-clock budget
(excluding LLM wait time).

The Kafka producer is injectable via a minimal :class:`KafkaProducerLike`
Protocol so the whole module is unit-testable with a fake — no live broker is
required. In production the producer is ``aiokafka.AIOKafkaProducer`` (see
:class:`AIOKafkaProducerAdapter`), imported lazily and started on first use,
mirroring ``services/pipeline-orchestrator/publisher.py``.

Every Indicator is published as its own event, keyed by ``claim_id`` so all
Indicators for a given claim land on the same partition and stay ordered. A
publish failure is logged and swallowed so a broker hiccup never crashes the
engine (Requirement 6.9 emphasises completing/publishing within budget, not
failing the claim).

Requirements: 6.9
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from shared.models import Indicator

logger = logging.getLogger("history-inconsistency-engine.kafka")

#: Kafka topic for this engine's Indicators.
INDICATORS_TOPIC: str = "indicators.history-inconsistency"

#: Wall-clock budget (ms) within which Indicators must be published (Req 6.9).
PUBLISH_BUDGET_MS: int = 500


class KafkaProducerLike(Protocol):
    """Minimal async Kafka producer surface used by :class:`IndicatorPublisher`."""

    async def send_and_wait(
        self, topic: str, value: bytes, key: bytes | None = None
    ) -> Any:  # pragma: no cover - protocol
        ...


@dataclass
class PublishResult:
    """Outcome of :meth:`IndicatorPublisher.publish`.

    Attributes
    ----------
    published_count:
        Number of Indicator events successfully published.
    topic:
        The topic the Indicators were published to.
    elapsed_ms:
        Wall-clock time spent publishing, in milliseconds.
    within_budget:
        ``True`` when publication completed within the 500 ms budget.
    """

    published_count: int = 0
    topic: str = INDICATORS_TOPIC
    elapsed_ms: float = 0.0
    within_budget: bool = True


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


class IndicatorPublisher:
    """Publishes the engine's Indicators to ``indicators.history-inconsistency``.

    Parameters
    ----------
    producer:
        The Kafka producer used to publish events. Tests inject a fake; may be
        ``None`` to disable publishing entirely (the engine then simply returns
        its Indicators in the HTTP response).
    topic:
        Override the destination topic (defaults to :data:`INDICATORS_TOPIC`).
    publish_budget_ms:
        The 500 ms publication budget (Requirement 6.9).
    """

    def __init__(
        self,
        producer: Optional[KafkaProducerLike],
        *,
        topic: str = INDICATORS_TOPIC,
        publish_budget_ms: int = PUBLISH_BUDGET_MS,
    ) -> None:
        self._producer = producer
        self._topic = topic
        self._publish_budget_ms = publish_budget_ms

    async def publish(
        self, claim_id: str, indicators: list[Indicator]
    ) -> PublishResult:
        """Publish each Indicator as its own event, keyed by ``claim_id``.

        Returns a :class:`PublishResult` describing how many events were sent
        and whether publication stayed within the 500 ms budget. When no
        producer is configured the call is a no-op that still returns within
        budget so the engine is unaffected.
        """
        result = PublishResult(topic=self._topic)
        if self._producer is None:
            logger.debug("no kafka producer configured; skipping indicator publish")
            return result

        start = time.monotonic()
        key_bytes = str(claim_id).encode("utf-8")
        for indicator in indicators:
            value_bytes = json.dumps(indicator.to_dict()).encode("utf-8")
            try:
                await self._producer.send_and_wait(
                    self._topic, value=value_bytes, key=key_bytes
                )
                result.published_count += 1
            except Exception as exc:  # noqa: BLE001 - a publish failure must not crash
                logger.error(
                    "failed to publish indicator %s for claim %s: %s",
                    getattr(indicator, "indicator_id", "?"),
                    claim_id,
                    exc,
                )

        result.elapsed_ms = (time.monotonic() - start) * 1000.0
        result.within_budget = result.elapsed_ms <= self._publish_budget_ms
        if not result.within_budget:
            logger.warning(
                "indicator publication for claim %s took %.1f ms, exceeding the "
                "%d ms budget",
                claim_id,
                result.elapsed_ms,
                self._publish_budget_ms,
            )
        return result


__all__ = [
    "INDICATORS_TOPIC",
    "PUBLISH_BUDGET_MS",
    "KafkaProducerLike",
    "PublishResult",
    "AIOKafkaProducerAdapter",
    "IndicatorPublisher",
]
