"""
services/regional-analysis-engine/kafka_producer.py
===================================================
Kafka publishing layer for the Regional Analysis Engine (task 16.2).

Provides:
  * :class:`IndicatorEventProducer` — a thin async wrapper around ``aiokafka``'s
    ``AIOKafkaProducer`` that publishes every produced Indicator to the
    ``indicators.regional-analysis`` topic, keyed by ``str(claim_id)`` so all
    Indicators for a claim land on the same partition and stay ordered.
  * :class:`FakeIndicatorEventProducer` — an in-memory test double implementing
    the same ``async publish_indicators`` interface, recording published
    envelopes and able to simulate publish failures for unit testing.

Each Indicator is published as a JSON envelope:

    {
        "event_type": "indicator.produced",
        "topic": "indicators.regional-analysis",
        "claim_id": "<uuid>",
        "indicator": { ... Indicator.to_dict() ... },
        "timestamp": "<ISO-8601 UTC>"
    }

The engine's overall 500 ms wall-clock budget (Requirement 8.9) is enforced by
``main.py`` around the analyse + publish sequence; this producer additionally
bounds each individual publish with an ``asyncio.wait_for`` deadline.

Requirements: 8.9
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Iterable

from shared.models import Indicator

__all__ = [
    "INDICATORS_TOPIC",
    "INDICATOR_EVENT_TYPE",
    "PUBLISH_TIMEOUT_SECONDS",
    "IndicatorPublishError",
    "build_indicator_event",
    "IndicatorEventProducer",
    "FakeIndicatorEventProducer",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INDICATORS_TOPIC = "indicators.regional-analysis"
INDICATOR_EVENT_TYPE = "indicator.produced"

#: Per-publish upper bound. The engine as a whole must publish all Indicators
#: within 500 ms (Requirement 8.9); a single publish is bounded well below that.
PUBLISH_TIMEOUT_SECONDS = 0.5


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class IndicatorPublishError(RuntimeError):
    """Raised when publishing an Indicator to Kafka fails or times out."""


# ---------------------------------------------------------------------------
# Event envelope
# ---------------------------------------------------------------------------


def build_indicator_event(indicator: Indicator) -> dict[str, Any]:
    """Build the ``indicator.produced`` JSON envelope for *indicator*."""
    return {
        "event_type": INDICATOR_EVENT_TYPE,
        "topic": INDICATORS_TOPIC,
        "claim_id": str(indicator.claim_id),
        "indicator": indicator.to_dict(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _serialize_event(event: dict[str, Any]) -> bytes:
    """Serialise an event envelope to UTF-8 JSON bytes."""
    return json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------------------
# aiokafka-backed producer
# ---------------------------------------------------------------------------


class IndicatorEventProducer:
    """Publish Indicators to ``indicators.regional-analysis`` via ``aiokafka``.

    The application lifespan handler owns the underlying producer's lifecycle
    (start/stop); this wrapper does not.
    """

    def __init__(
        self,
        producer: Any,
        *,
        topic: str = INDICATORS_TOPIC,
        timeout_seconds: float = PUBLISH_TIMEOUT_SECONDS,
    ) -> None:
        if producer is None:
            raise ValueError(
                "IndicatorEventProducer requires a non-null aiokafka producer."
            )
        self._producer = producer
        self._topic = topic
        self._timeout_seconds = timeout_seconds

    async def publish_indicators(self, indicators: Iterable[Indicator]) -> int:
        """Publish every Indicator, keyed by ``str(claim_id)``.

        Returns the number of Indicators published. On timeout or any underlying
        error an :class:`IndicatorPublishError` is raised.
        """
        published = 0
        for indicator in indicators:
            event = build_indicator_event(indicator)
            value = _serialize_event(event)
            key = str(indicator.claim_id).encode("utf-8")
            try:
                await asyncio.wait_for(
                    self._producer.send_and_wait(self._topic, value=value, key=key),
                    timeout=self._timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                raise IndicatorPublishError(
                    f"Publishing Indicator timed out after "
                    f"{self._timeout_seconds * 1000:.0f} ms"
                ) from exc
            except Exception as exc:  # noqa: BLE001 - normalise publish failures
                raise IndicatorPublishError(
                    f"Failed to publish Indicator: {exc}"
                ) from exc
            published += 1
        return published


# ---------------------------------------------------------------------------
# In-memory fake for unit tests / local dev
# ---------------------------------------------------------------------------


class FakeIndicatorEventProducer:
    """In-memory ``IndicatorEventProducer`` stand-in for tests and local dev."""

    def __init__(self, *, topic: str = INDICATORS_TOPIC) -> None:
        self._topic = topic
        self._fail_next: bool = False
        self.published: list[dict[str, Any]] = []

    def fail_next_publish(self) -> None:
        """Arm the producer to raise on the next publish call."""
        self._fail_next = True

    async def publish_indicators(self, indicators: Iterable[Indicator]) -> int:
        """Record published Indicators, honouring the armed failure flag."""
        if self._fail_next:
            self._fail_next = False
            raise IndicatorPublishError("Simulated Indicator publish failure")
        count = 0
        for indicator in indicators:
            self.published.append(build_indicator_event(indicator))
            count += 1
        return count
