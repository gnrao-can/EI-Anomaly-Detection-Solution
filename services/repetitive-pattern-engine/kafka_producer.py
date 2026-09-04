"""
services/repetitive-pattern-engine/kafka_producer.py
====================================================
Kafka publishing layer for the Repetitive Pattern Engine (task 11.2).

Provides:
  - ``IndicatorProducer`` — a thin async wrapper around ``aiokafka``'s
    ``AIOKafkaProducer``. It publishes each :class:`~shared.models.Indicator`
    to the ``indicators.repetitive-pattern`` topic, using ``str(claim_id)`` as
    the partition key so every Indicator for a given claim lands on the same
    partition and is therefore totally ordered.
  - ``FakeIndicatorProducer`` — an in-memory test double implementing the same
    async ``publish_indicators`` interface, able to record published events and
    simulate publish failures for unit testing without a live broker.

The event envelope is a plain JSON object:

    {
        "event_type": "indicator.repetitive-pattern",
        "claim_id": "<uuid>",
        "indicator": { ... Indicator.to_dict() ... },
        "timestamp": "<ISO-8601 UTC>"
    }

Only the structured Indicator (which never contains a raw SIN) is published, so
no Protected B secret is written to the event stream.

Requirements: 3.2, 3.7
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from shared.models import Indicator

logger = logging.getLogger("repetitive-pattern-engine.kafka")

__all__ = [
    "INDICATOR_TOPIC",
    "INDICATOR_EVENT_TYPE",
    "IndicatorPublishError",
    "build_indicator_event",
    "IndicatorProducer",
    "FakeIndicatorProducer",
]


INDICATOR_TOPIC = "indicators.repetitive-pattern"
INDICATOR_EVENT_TYPE = "indicator.repetitive-pattern"


class IndicatorPublishError(RuntimeError):
    """Raised when publishing an Indicator to Kafka fails."""


def build_indicator_event(indicator: Indicator) -> dict[str, Any]:
    """Build the JSON envelope for a single Indicator."""
    return {
        "event_type": INDICATOR_EVENT_TYPE,
        "claim_id": str(indicator.claim_id),
        "indicator": indicator.to_dict(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _serialize_event(event: dict[str, Any]) -> bytes:
    """Serialise an event envelope to UTF-8 JSON bytes."""
    return json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class IndicatorProducer:
    """Publish Indicators via an ``aiokafka`` producer.

    Parameters
    ----------
    producer:
        A started ``aiokafka.AIOKafkaProducer`` instance. The application
        lifespan handler owns the producer lifecycle (start/stop); this wrapper
        does not.
    topic:
        Target topic. Defaults to ``indicators.repetitive-pattern``.
    """

    def __init__(self, producer: Any, *, topic: str = INDICATOR_TOPIC) -> None:
        if producer is None:
            raise ValueError("IndicatorProducer requires a non-null aiokafka producer.")
        self._producer = producer
        self._topic = topic

    async def publish_indicators(self, indicators: Iterable[Indicator]) -> int:
        """Publish each Indicator keyed by ``str(claim_id)``.

        Returns the number of Indicators published. On any underlying error an
        :class:`IndicatorPublishError` is raised.
        """
        count = 0
        try:
            for indicator in indicators:
                event = build_indicator_event(indicator)
                value = _serialize_event(event)
                key = str(indicator.claim_id).encode("utf-8")
                await self._producer.send_and_wait(self._topic, value=value, key=key)
                count += 1
        except Exception as exc:  # noqa: BLE001 - normalise all publish failures
            raise IndicatorPublishError(
                f"Failed to publish repetitive-pattern Indicator: {exc}"
            ) from exc
        return count


class FakeIndicatorProducer:
    """In-memory ``IndicatorProducer`` stand-in for tests and local dev.

    Records every published event in ``published`` and supports simulating an
    immediate publish failure via ``fail_next_publish``.
    """

    def __init__(self, *, topic: str = INDICATOR_TOPIC) -> None:
        self._topic = topic
        self._fail_next: bool = False
        self.published: list[dict[str, Any]] = []

    def fail_next_publish(self) -> None:
        """Arm the producer to raise ``IndicatorPublishError`` on next publish."""
        self._fail_next = True

    async def publish_indicators(self, indicators: Iterable[Indicator]) -> int:
        if self._fail_next:
            self._fail_next = False
            raise IndicatorPublishError("Simulated Kafka publish failure")
        count = 0
        for indicator in indicators:
            self.published.append(build_indicator_event(indicator))
            count += 1
        return count
