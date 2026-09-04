"""
services/document-forgery-engine/kafka_publisher.py
====================================================
Indicator publication + 700 ms budget for the Document Forgery Engine
(task 15.3).

Design step 8 of the Document Forgery Engine and Requirements 7.10 / 7.11:

  * 7.10 — the engine SHALL complete its analysis and publish all Indicators to
    the Kafka topic ``indicators.document-forgery`` within **700 ms**, measured
    from the ``claim.received`` receipt timestamp to the final Indicator
    published timestamp.
  * 7.11 — IF the engine does not complete within 700 ms, THE Detection_Pipeline
    SHALL emit a ``PROCESSING_TIMEOUT`` Indicator for this engine.

The Kafka producer is injectable via a minimal :class:`KafkaProducerLike`
Protocol so the whole module is unit-testable with a fake — no live broker is
required. In production the producer is ``aiokafka.AIOKafkaProducer`` (see
:class:`AIOKafkaProducerAdapter`), imported lazily and started on first use.
This mirrors ``services/history-inconsistency-engine/kafka_publisher.py``.

Every Indicator is published as its own event, keyed by ``claim_id`` so all
Indicators for a given claim land on the same partition and stay ordered. A
publish failure is logged and swallowed so a broker hiccup never crashes the
engine.

The 700 ms budget covers the *core* forensic analysis + publication. The async
LLM style check (``style_analysis.py``) runs off this critical path per the
design ("LLM style analysis (async)").

Requirements: 7.10, 7.11
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from shared.models import EngineType, Indicator, Severity

logger = logging.getLogger("document-forgery-engine.kafka")

#: Kafka topic for this engine's Indicators.
INDICATORS_TOPIC: str = "indicators.document-forgery"

#: Wall-clock budget (ms) within which the core analysis + Indicators must be
#: published (Requirement 7.10).
ANALYSIS_BUDGET_MS: int = 700

#: Flag recorded on the PROCESSING_TIMEOUT Indicator (Requirement 7.11).
PROCESSING_TIMEOUT = "PROCESSING_TIMEOUT"


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
        ``True`` when publication completed within the 700 ms budget.
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
    """Publishes the engine's Indicators to ``indicators.document-forgery``.

    Parameters
    ----------
    producer:
        The Kafka producer used to publish events. Tests inject a fake; may be
        ``None`` to disable publishing entirely (the engine then simply returns
        its Indicators in the HTTP response).
    topic:
        Override the destination topic (defaults to :data:`INDICATORS_TOPIC`).
    """

    def __init__(
        self,
        producer: Optional[KafkaProducerLike],
        *,
        topic: str = INDICATORS_TOPIC,
    ) -> None:
        self._producer = producer
        self._topic = topic

    async def publish(
        self, claim_id: Optional[str], indicators: list[Indicator]
    ) -> PublishResult:
        """Publish each Indicator as its own event, keyed by ``claim_id``.

        Returns a :class:`PublishResult` describing how many events were sent
        and the wall-clock elapsed time. When no producer is configured the call
        is a no-op that still returns a result so the engine is unaffected.
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
        return result


def make_processing_timeout_indicator(
    elapsed_ms: float,
    *,
    claim_id: Optional[str] = None,
    budget_ms: int = ANALYSIS_BUDGET_MS,
) -> Indicator:
    """Build the ``PROCESSING_TIMEOUT`` Indicator for a budget breach (Req 7.11).

    Emitted when the engine's core analysis + publication exceeds the 700 ms
    budget. The Indicator records the elapsed time and the budget so the audit
    trail captures the breach.
    """
    return Indicator(
        indicator_id=uuid.uuid4(),
        claim_id=_coerce_uuid(claim_id),
        engine=EngineType.DOCUMENT_FORGERY,
        severity=Severity.UNRESOLVED,
        algorithm="processing_budget",
        title="Document Forgery Engine exceeded its 700 ms processing budget",
        explanation_en=(
            f"The Document Forgery Engine did not complete its analysis within "
            f"the {budget_ms} ms budget (elapsed {elapsed_ms:.1f} ms); a "
            f"PROCESSING_TIMEOUT Indicator was emitted for this engine."
        ),
        explanation_fr=(
            f"Le moteur de detection de falsification de documents n'a pas "
            f"termine son analyse dans le delai de {budget_ms} ms (temps ecoule "
            f"{elapsed_ms:.1f} ms); un indicateur PROCESSING_TIMEOUT a ete emis "
            f"pour ce moteur."
        ),
        payload={
            "flag": PROCESSING_TIMEOUT,
            "elapsed_ms": round(elapsed_ms, 4),
            "budget_ms": budget_ms,
        },
        produced_at=datetime.now(timezone.utc),
    )


def _coerce_uuid(claim_id: Optional[str]) -> uuid.UUID:
    """Coerce a claim id string to a UUID, tolerating missing / non-UUID ids."""
    if isinstance(claim_id, uuid.UUID):
        return claim_id
    if claim_id:
        try:
            return uuid.UUID(str(claim_id))
        except (ValueError, AttributeError, TypeError):
            return uuid.uuid5(uuid.NAMESPACE_OID, str(claim_id))
    return uuid.uuid4()


__all__ = [
    "INDICATORS_TOPIC",
    "ANALYSIS_BUDGET_MS",
    "PROCESSING_TIMEOUT",
    "KafkaProducerLike",
    "PublishResult",
    "AIOKafkaProducerAdapter",
    "IndicatorPublisher",
    "make_processing_timeout_indicator",
]
