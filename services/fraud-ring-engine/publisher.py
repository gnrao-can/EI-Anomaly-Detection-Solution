"""
services/fraud-ring-engine/publisher.py
=========================================
Indicator publishing for the Fraud Ring Engine (task 13.2).

Requirement 5.8 requires the engine to publish all Indicators to the Kafka topic
``indicators.fraud-ring``. This module defines a small ``IndicatorPublisher``
Protocol plus two implementations:

  * ``AioKafkaIndicatorPublisher`` — production publisher backed by an injected
    ``aiokafka``-style producer. The producer is dependency-injected so the
    engine does not hard-depend on a live broker being importable at module load
    time (``aiokafka`` may be absent in the sandbox).
  * ``InMemoryIndicatorPublisher`` — test/dev publisher that records published
    Indicators in a list; used by unit tests and by the engine when no broker is
    wired (graceful no-op capture).

Each published record is the Indicator's ``to_dict()`` JSON, keyed by
``claim_id`` so all Indicators for a claim land on the same partition (ordered
per claim).

Requirements: 5.8
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from shared.models import Indicator

#: The Kafka topic all Fraud Ring Indicators are published to (design.md).
FRAUD_RING_TOPIC = "indicators.fraud-ring"


class IndicatorPublisher(Protocol):
    """Publishes Fraud Ring Indicators to the message bus."""

    async def publish(self, indicators: list[Indicator]) -> int:
        """Publish ``indicators``; return the count acknowledged."""
        ...


@dataclass
class InMemoryIndicatorPublisher:
    """In-memory ``IndicatorPublisher`` for tests / no-broker environments.

    Captures every published Indicator (as the original object) plus its
    serialised record so tests can assert on both. Set ``fail`` to simulate a
    broker failure (``publish`` raises).
    """

    topic: str = FRAUD_RING_TOPIC
    published: list[Indicator] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)
    fail: bool = False

    async def publish(self, indicators: list[Indicator]) -> int:
        if self.fail:
            raise RuntimeError("kafka broker unavailable")
        for ind in indicators:
            self.published.append(ind)
            self.records.append(ind.to_dict())
        return len(indicators)


class AioKafkaIndicatorPublisher:
    """Production ``IndicatorPublisher`` backed by an injected aiokafka producer.

    The producer must expose ``async send_and_wait(topic, value=..., key=...)``
    (the ``aiokafka.AIOKafkaProducer`` surface). Values are UTF-8 JSON bytes;
    keys are the claim id bytes so per-claim ordering is preserved.
    """

    def __init__(self, producer: Any, *, topic: str = FRAUD_RING_TOPIC) -> None:
        self._producer = producer
        self._topic = topic

    async def publish(self, indicators: list[Indicator]) -> int:
        count = 0
        for ind in indicators:
            record = ind.to_dict()
            value = json.dumps(record, separators=(",", ":")).encode("utf-8")
            key = str(record.get("claim_id") or "").encode("utf-8")
            await self._producer.send_and_wait(self._topic, value=value, key=key)
            count += 1
        return count


__all__ = [
    "FRAUD_RING_TOPIC",
    "IndicatorPublisher",
    "InMemoryIndicatorPublisher",
    "AioKafkaIndicatorPublisher",
]
