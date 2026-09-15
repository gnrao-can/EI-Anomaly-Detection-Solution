"""
services/portal-backend/consumer.py
===================================
Kafka consumer that feeds the investigator dashboard's live SSE stream (Req 22.1).

The Ensemble Scorer persists each RiskAssessment to PostgreSQL and the
orchestrator publishes a ``claim.scored`` event to Kafka. The dashboard's
``GET /portal/events/claims`` SSE endpoint fans out flagged claims to connected
investigators — but nothing pushed to that broadcaster on a new score, so the
queue only updated on the initial REST fetch (a page reload). This consumer
closes that gap: it subscribes to ``claim.scored`` and, for every claim whose
decision is FLAG_FOR_REVIEW / INVESTIGATE, reads the claim's display row and
calls :meth:`ClaimEventBroadcaster.publish` so all open dashboards get it live.

Design notes
------------
* **Self-healing connect** via ``shared.retry.connect_with_retry`` — the broker
  is often not ready at portal-backend startup (compose brings services up
  concurrently, ``depends_on`` intentionally removed), so we retry with backoff
  rather than degrading permanently.
* **Degrade gracefully** — if aiokafka is unavailable or the broker never comes
  up, the consumer stays inert; the dashboard still works via the REST fetch
  (just not live). Never crashes the service.
* **Publishes the UNMASKED row** — the SSE endpoint masks per subscriber by role
  (Req 22.6), so this consumer must not pre-mask.
* **Row-read retry** — the scorer persists the ``risk_assessments`` row before
  the orchestrator publishes ``claim.scored``, but we retry the read a few times
  to tolerate any small replication/commit lag.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional

try:  # sibling modules imported by bare name in the service directory
    from repository import FLAGGED_DECISIONS  # type: ignore
except ImportError:  # pragma: no cover - package-style import
    from .repository import FLAGGED_DECISIONS  # type: ignore

from shared.retry import connect_with_retry

logger = logging.getLogger("portal-backend.consumer")

#: Topic carrying the pipeline's scoring decisions.
CLAIM_SCORED_TOPIC = "claim.scored"

#: How many times (and how long apart) to re-read the flagged row before giving
#: up on a scored event — covers the small window between the scorer's DB commit
#: and the orchestrator's publish.
_ROW_READ_ATTEMPTS = 5
_ROW_READ_DELAY_S = 0.5


def extract_scored_event(raw_value: bytes | str | dict[str, Any]) -> dict[str, Any]:
    """Normalise a Kafka ``claim.scored`` message value to a dict (or ``{}``)."""
    try:
        if isinstance(raw_value, (bytes, bytearray)):
            return json.loads(raw_value.decode("utf-8"))
        if isinstance(raw_value, str):
            return json.loads(raw_value)
        if isinstance(raw_value, dict):
            return dict(raw_value)
    except Exception:  # noqa: BLE001 - malformed frame -> ignore
        return {}
    return {}


class PortalScoredConsumer:
    """Consume ``claim.scored`` and push flagged claims to the SSE broadcaster.

    Parameters
    ----------
    broadcaster:
        The :class:`ClaimEventBroadcaster` whose ``publish`` fans a flagged-claim
        dict out to every connected SSE subscriber.
    repository_getter:
        A zero-arg callable returning the current repository (so a repository
        swapped onto ``app.state`` after start is still honoured). The repository
        must expose ``async get_flagged_claim(claim_id) -> dict | None``.
    bootstrap_servers:
        Kafka bootstrap servers string.
    topic / group_id:
        The scored topic and this consumer's group id.
    degrade_gracefully:
        When ``True`` (default) a broker that never connects leaves the consumer
        inert instead of raising.
    """

    def __init__(
        self,
        broadcaster: Any,
        repository_getter: Callable[[], Any],
        *,
        bootstrap_servers: str = "localhost:9092",
        topic: str = CLAIM_SCORED_TOPIC,
        group_id: str = "portal-backend-sse",
        degrade_gracefully: bool = True,
    ) -> None:
        self._broadcaster = broadcaster
        self._repository_getter = repository_getter
        self._bootstrap_servers = bootstrap_servers
        self._topic = topic
        self._group_id = group_id
        self._degrade_gracefully = degrade_gracefully

        self._consumer: Any = None
        self._task: Optional[asyncio.Task[None]] = None
        self._running = False

    async def start(self) -> bool:
        """Schedule a BACKGROUND connect + consume loop; never blocks startup.

        The Kafka connect is retried with backoff in a background task, so a
        broker that is slow/absent at startup does NOT stall the app lifespan
        (a plain-HTTP dashboard still works via the REST fetch). Returns ``True``
        when the background connect task was scheduled, ``False`` when aiokafka
        is unavailable and graceful degradation is enabled.
        """
        try:
            import aiokafka  # noqa: F401 - presence check only
        except Exception as exc:  # noqa: BLE001 - aiokafka import failure
            if self._degrade_gracefully:
                logger.warning("aiokafka unavailable, SSE live-feed off: %s", exc)
                return False
            raise

        self._running = True
        self._task = asyncio.create_task(self._connect_and_run())
        logger.info("portal claim.scored consumer starting (background) on %s", self._topic)
        return True

    async def _connect_and_run(self) -> None:
        """Connect with retry (in the background), then run the consume loop."""
        from aiokafka import AIOKafkaConsumer

        async def _connect() -> Any:
            consumer = AIOKafkaConsumer(
                self._topic,
                bootstrap_servers=self._bootstrap_servers,
                group_id=self._group_id,
                enable_auto_commit=True,
                auto_offset_reset="latest",
            )
            await consumer.start()
            return consumer

        self._consumer = await connect_with_retry(
            _connect, description="portal-backend claim.scored consumer"
        )
        if self._consumer is None:
            logger.warning("claim.scored consumer could not connect; SSE live-feed off")
            return
        logger.info("portal claim.scored consumer connected on %s", self._topic)
        await self._run()

    async def stop(self) -> None:
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

    @property
    def is_connected(self) -> bool:
        return self._consumer is not None

    async def _run(self) -> None:
        assert self._consumer is not None
        try:
            async for message in self._consumer:
                if not self._running:
                    break
                try:
                    await self._handle(message.value)
                except Exception as exc:  # noqa: BLE001 - one bad msg must not kill the loop
                    logger.warning("claim.scored handler error: %s", exc)
        except asyncio.CancelledError:  # pragma: no cover - normal shutdown
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("claim.scored consume loop ended: %s", exc)

    async def _handle(self, raw_value: Any) -> None:
        event = extract_scored_event(raw_value)
        claim_id = event.get("claim_id")
        decision = event.get("risk_decision")
        if not claim_id:
            return
        # Only flagged decisions belong in the investigator queue (Req 22.1);
        # AUTO_APPROVE / INDETERMINATE are not surfaced there.
        if decision is not None and decision not in FLAGGED_DECISIONS:
            return

        repository = self._repository_getter()
        if repository is None:
            return

        row = await self._read_flagged_row(repository, str(claim_id))
        if row is None:
            # Not flagged after all (or not yet persisted) — nothing to push.
            return
        await self._broadcaster.publish(row)

    async def _read_flagged_row(
        self, repository: Any, claim_id: str
    ) -> Optional[dict[str, Any]]:
        """Read the flagged display row, retrying briefly for commit lag."""
        for attempt in range(_ROW_READ_ATTEMPTS):
            try:
                row = await repository.get_flagged_claim(claim_id)
            except Exception as exc:  # noqa: BLE001 - repo error -> skip
                logger.warning("get_flagged_claim(%s) failed: %s", claim_id, exc)
                return None
            if row is not None:
                return row
            if attempt < _ROW_READ_ATTEMPTS - 1:
                await asyncio.sleep(_ROW_READ_DELAY_S)
        return None


__all__ = ["PortalScoredConsumer", "extract_scored_event", "CLAIM_SCORED_TOPIC"]
