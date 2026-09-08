"""
services/claim-intake/kafka_producer.py
=======================================
Kafka publishing layer for the Claim Intake Service (task 4.3).

Provides:
  - ``ClaimEventProducer`` — a thin async wrapper around ``aiokafka``'s
    ``AIOKafkaProducer``. It publishes a ``claim.received`` event to the
    ``claim.received`` topic, using ``str(claim_id)`` as the partition key so
    that every event for a given claim lands on the same partition and is
    therefore totally ordered (design: "Partitioning key: ``claim_id``").
  - ``FakeClaimEventProducer`` — an in-memory test double implementing the same
    ``async publish_claim_received`` interface, able to simulate publish
    failures and slow (timeout-inducing) publishes for unit testing the
    two-phase commit / rollback path.

The publish is bounded by a hard 500 ms timeout (Requirement 1.5) via
``asyncio.wait_for``; a timeout or any underlying error is surfaced as a
``KafkaPublishError`` so the caller can roll the PostgreSQL write back.

The event envelope is a plain JSON object:

    {
        "event_type": "claim.received",
        "claim_id": "<uuid>",
        "payload": { ... claim.received fields ... },
        "timestamp": "<ISO-8601 UTC>"
    }

The raw SIN is never included in the envelope — only ``sin_hash`` — so no
Protected B secret is written to the event stream (Requirement 1.11).

Requirements: 1.4, 1.5, 1.11
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Optional

from shared.models import Claim

__all__ = [
    "CLAIM_RECEIVED_TOPIC",
    "CLAIM_RECEIVED_EVENT_TYPE",
    "PUBLISH_TIMEOUT_SECONDS",
    "KafkaPublishError",
    "build_claim_received_event",
    "ClaimEventProducer",
    "FakeClaimEventProducer",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLAIM_RECEIVED_TOPIC = "claim.received"
CLAIM_RECEIVED_EVENT_TYPE = "claim.received"

# Hard upper bound on a single publish. Requirement 1.5: roll back the PG write
# if the Kafka publish fails to complete within 500 ms.
PUBLISH_TIMEOUT_SECONDS = 0.5


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class KafkaPublishError(RuntimeError):
    """Raised when the ``claim.received`` publish fails or times out.

    The caller (the intake endpoint) treats any ``KafkaPublishError`` as the
    trigger to roll back the PostgreSQL write and return a structured error to
    the claimant.
    """


# ---------------------------------------------------------------------------
# Event envelope
# ---------------------------------------------------------------------------


def build_claim_received_event(claim: Claim) -> dict[str, Any]:
    """Build the ``claim.received`` JSON envelope for *claim*.

    Only the fields documented for the ``claim.received`` event (design:
    "Output Events → claim.received") plus a small set of routing fields are
    included. The raw SIN is never present — only ``sin_hash``.
    """
    payload = {
        "claim_id": str(claim.claim_id),
        "sin_hash": claim.sin_hash,
        "employer_bn": claim.employer_bn,
        # Detection engines consume these claim fields (via the orchestrator's
        # engine-request builder). They are Protected-B-safe (no raw SIN/name/
        # address) and let the downstream engines run without a second lookup:
        #   insurable_hours/earnings + separation/claim dates -> repetitive,
        #     history (tax_year), regional (hourly earnings, reference month);
        #   economic_region_code + noc_code -> regional analysis.
        "insurable_hours": str(claim.insurable_hours),
        "insurable_earnings": str(claim.insurable_earnings),
        "separation_date": claim.separation_date.isoformat(),
        "claim_start_date": claim.claim_start_date.isoformat(),
        "economic_region_code": claim.economic_region_code,
        "noc_code": claim.noc_code,
        "received_at": claim.received_at.isoformat(),
        "status": claim.status.value,
    }
    return {
        "event_type": CLAIM_RECEIVED_EVENT_TYPE,
        "claim_id": str(claim.claim_id),
        "payload": payload,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _serialize_event(event: dict[str, Any]) -> bytes:
    """Serialise an event envelope to UTF-8 JSON bytes."""
    return json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------------------
# aiokafka-backed producer
# ---------------------------------------------------------------------------


class ClaimEventProducer:
    """Publish ``claim.received`` events via an ``aiokafka`` producer.

    Parameters
    ----------
    producer:
        A started ``aiokafka.AIOKafkaProducer`` instance. The application
        lifespan handler owns the producer lifecycle (start/stop); this wrapper
        does not.
    topic:
        Target topic. Defaults to ``claim.received``.
    timeout_seconds:
        Per-publish timeout. Defaults to 500 ms (Requirement 1.5).
    """

    def __init__(
        self,
        producer: Any,
        *,
        topic: str = CLAIM_RECEIVED_TOPIC,
        timeout_seconds: float = PUBLISH_TIMEOUT_SECONDS,
    ) -> None:
        if producer is None:
            raise ValueError("ClaimEventProducer requires a non-null aiokafka producer.")
        self._producer = producer
        self._topic = topic
        self._timeout_seconds = timeout_seconds

    async def publish_claim_received(self, claim: Claim) -> None:
        """Publish a ``claim.received`` event keyed by ``str(claim_id)``.

        The publish (send + broker acknowledgement) must complete within the
        configured timeout. On timeout or any underlying error a
        ``KafkaPublishError`` is raised so the caller can roll back the PG write.
        """
        event = build_claim_received_event(claim)
        value = _serialize_event(event)
        key = str(claim.claim_id).encode("utf-8")

        try:
            # ``send_and_wait`` resolves only once the broker acknowledges the
            # record, so wrapping it in ``wait_for`` enforces a true end-to-end
            # publish deadline.
            await asyncio.wait_for(
                self._producer.send_and_wait(self._topic, value=value, key=key),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise KafkaPublishError(
                f"Publishing claim.received timed out after "
                f"{self._timeout_seconds * 1000:.0f} ms"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - normalise all publish failures
            raise KafkaPublishError(
                f"Failed to publish claim.received: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# In-memory fake for unit tests / local dev
# ---------------------------------------------------------------------------


class FakeClaimEventProducer:
    """In-memory ``ClaimEventProducer`` stand-in for tests and local dev.

    Records every published event in ``published`` and supports simulating:
      - an immediate publish failure (``fail_next_publish``), and
      - a slow publish that exceeds the timeout (``delay_seconds`` combined with
        ``timeout_seconds``), so the rollback-on-timeout path can be exercised
        without a real broker.
    """

    def __init__(
        self,
        *,
        topic: str = CLAIM_RECEIVED_TOPIC,
        timeout_seconds: float = PUBLISH_TIMEOUT_SECONDS,
        delay_seconds: float = 0.0,
    ) -> None:
        self._topic = topic
        self._timeout_seconds = timeout_seconds
        self._delay_seconds = delay_seconds
        self._fail_next: bool = False
        self.published: list[dict[str, Any]] = []

    def fail_next_publish(self) -> None:
        """Arm the producer to raise ``KafkaPublishError`` on the next publish."""
        self._fail_next = True

    def set_delay(self, delay_seconds: float) -> None:
        """Configure an artificial publish delay (to trigger timeouts)."""
        self._delay_seconds = delay_seconds

    async def publish_claim_received(self, claim: Claim) -> None:
        """Simulate publishing a ``claim.received`` event.

        Honours the armed failure flag and the configured delay/timeout so unit
        tests can drive both the failure and the timeout rollback branches.
        """
        if self._fail_next:
            self._fail_next = False
            raise KafkaPublishError("Simulated Kafka publish failure")

        async def _do_publish() -> None:
            if self._delay_seconds:
                await asyncio.sleep(self._delay_seconds)
            self.published.append(build_claim_received_event(claim))

        try:
            await asyncio.wait_for(_do_publish(), timeout=self._timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise KafkaPublishError(
                f"Publishing claim.received timed out after "
                f"{self._timeout_seconds * 1000:.0f} ms"
            ) from exc
