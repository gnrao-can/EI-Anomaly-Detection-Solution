"""
services/portal-backend/sse.py
==============================
In-process Server-Sent Events (SSE) fan-out for the flagged-claims stream.

``GET /portal/events/claims`` streams new/updated flagged claims to the
investigator dashboard (Req 22.1). The SPA (``useClaimEvents.ts``) opens an
``EventSource`` and parses each ``data:`` frame as the JSON of one flagged
claim.

This module provides a dependency-free broadcaster backed by per-subscriber
``asyncio.Queue``s. The disposition / ingest paths call
:meth:`ClaimEventBroadcaster.publish` to push a flagged-claim dict; every
connected stream receives it. A Kafka consumer can later call the same
``publish`` seam, but the in-process path works with no broker present, which is
the local-dev requirement.

Each SSE frame is ``data: <json>\\n\\n`` (SSE message framing). Periodic
heartbeat comments (``: keep-alive\\n\\n``) keep intermediaries from closing an
idle connection.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Optional

__all__ = ["ClaimEventBroadcaster", "format_sse", "HEARTBEAT_COMMENT"]

# Comment frame emitted on the heartbeat interval. Lines beginning with ':' are
# SSE comments the browser ignores, but they keep the socket warm.
HEARTBEAT_COMMENT = ": keep-alive\n\n"


def format_sse(data: dict[str, Any]) -> str:
    """Serialise *data* into a single SSE ``data:`` frame."""
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return f"data: {payload}\n\n"


class ClaimEventBroadcaster:
    """Fan-out hub delivering flagged-claim events to all SSE subscribers."""

    def __init__(self, *, queue_maxsize: int = 100) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._queue_maxsize = queue_maxsize
        self._lock = asyncio.Lock()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def subscribe(self) -> asyncio.Queue:
        """Register and return a new subscriber queue."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_maxsize)
        async with self._lock:
            self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Remove a subscriber queue."""
        async with self._lock:
            self._subscribers.discard(queue)

    async def publish(self, claim: dict[str, Any]) -> None:
        """Push a flagged-claim dict to every connected subscriber.

        A subscriber whose queue is full (a slow/stalled client) is skipped for
        this event rather than blocking the publisher — the initial REST fetch
        keeps that client eventually consistent.
        """
        async with self._lock:
            subscribers = list(self._subscribers)
        for queue in subscribers:
            try:
                queue.put_nowait(claim)
            except asyncio.QueueFull:  # pragma: no cover - defensive
                continue

    async def event_stream(
        self,
        queue: asyncio.Queue,
        *,
        heartbeat_seconds: float = 15.0,
    ) -> AsyncIterator[str]:
        """Yield SSE frames for *queue* until the consumer disconnects.

        Emits a heartbeat comment whenever no event arrives within
        ``heartbeat_seconds`` so proxies do not drop the idle connection.
        """
        try:
            while True:
                try:
                    claim = await asyncio.wait_for(
                        queue.get(), timeout=heartbeat_seconds
                    )
                except asyncio.TimeoutError:
                    yield HEARTBEAT_COMMENT
                    continue
                yield format_sse(claim)
        finally:
            await self.unsubscribe(queue)
