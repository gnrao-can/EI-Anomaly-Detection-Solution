"""
services/pipeline-orchestrator/correlator.py
=============================================
Event correlation for the Detection Pipeline Orchestrator (task 6.1).

The orchestrator consumes two independent event streams:

  * ``claim.received`` — published by the Claim Intake Service.
  * ``roe.received``   — published by the ROE Adapter.

Both carry the same ``claim_id``. A claim can only be scored once *both* events
have arrived. This module implements the correlation logic:

  * :class:`EventCorrelator` records each arriving event in a pairing store
    keyed by ``claim_id`` with a 60-second TTL (``ORCHESTRATOR_PAIRING_TTL_SECONDS``
    from ``shared/cache_keys.py``). When the complementary event is already
    present, it returns a :class:`PairResult` carrying both payloads and the
    *later* of the two event timestamps; otherwise it stores the event and
    returns ``None`` (still waiting).

  * The 60-second correlation timeout is enforced by the pairing store's TTL:
    if only one event of a pair arrives within the window, the stored partial
    entry expires and no pairing is ever produced. :meth:`EventCorrelator.check_timeout`
    surfaces this as a :class:`TimeoutSignal` so the caller can proceed with a
    partial / INDETERMINATE decision later in the pipeline.

Two pairing-store backends are provided so tests can run without a live Redis:

  * :class:`RedisPairingStore` — wraps ``shared.cache.RedisCache``.
  * :class:`InMemoryPairingStore` — a dict-backed fake honouring the same TTL
    semantics (with a monotonic clock hook so tests can advance time
    deterministically).

Requirements: 2.1
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, Protocol

try:  # canonical TTL / key builder live in the shared package
    from shared.cache_keys import (
        ORCHESTRATOR_PAIRING_TTL_SECONDS,
        orchestrator_pairing_key,
    )
except ImportError:  # pragma: no cover - defensive fallback if shared is unavailable
    ORCHESTRATOR_PAIRING_TTL_SECONDS = 60

    def orchestrator_pairing_key(claim_id: str) -> str:  # type: ignore[misc]
        return f"orchestrator:pairing:{claim_id}"


__all__ = [
    "CLAIM_RECEIVED",
    "ROE_RECEIVED",
    "PairResult",
    "TimeoutSignal",
    "PairingStore",
    "InMemoryPairingStore",
    "RedisPairingStore",
    "EventCorrelator",
]


# ---------------------------------------------------------------------------
# Event-type constants
# ---------------------------------------------------------------------------

CLAIM_RECEIVED = "claim.received"
ROE_RECEIVED = "roe.received"

#: The two event types that must be paired before dispatch.
_COMPLEMENT = {CLAIM_RECEIVED: ROE_RECEIVED, ROE_RECEIVED: CLAIM_RECEIVED}


# ---------------------------------------------------------------------------
# Result / signal value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PairResult:
    """The outcome of a successful correlation of both events for a claim.

    Attributes
    ----------
    claim_id:
        The claim both events refer to.
    claim_payload:
        The payload delivered with the ``claim.received`` event.
    roe_payload:
        The payload delivered with the ``roe.received`` event.
    later_timestamp:
        The later of the two event timestamps. Engine dispatch must be triggered
        within the dispatch budget (100 ms) of this instant.
    """

    claim_id: str
    claim_payload: dict[str, Any]
    roe_payload: dict[str, Any]
    later_timestamp: datetime


@dataclass(frozen=True)
class TimeoutSignal:
    """Emitted when only one event of a pair arrived within the 60-second window.

    Attributes
    ----------
    claim_id:
        The claim whose pairing expired.
    present_event_type:
        The event type that *did* arrive (``claim.received`` or ``roe.received``).
    present_payload:
        The payload of the event that arrived.
    missing_event_type:
        The complementary event type that never arrived within the window.
    """

    claim_id: str
    present_event_type: str
    present_payload: dict[str, Any]
    missing_event_type: str


# ---------------------------------------------------------------------------
# Pairing store abstraction
# ---------------------------------------------------------------------------


class PairingStore(Protocol):
    """Minimal async key/value store with per-key TTL used for pairing.

    A stored value is the JSON-serialisable dict recording the first-arriving
    event of a pair. The store MUST expire the key after ``ttl_seconds``.
    """

    async def get(self, key: str) -> Optional[dict[str, Any]]:  # pragma: no cover - protocol
        ...

    async def set_with_ttl(
        self, key: str, value: dict[str, Any], ttl_seconds: int
    ) -> None:  # pragma: no cover - protocol
        ...

    async def invalidate(self, key: str) -> None:  # pragma: no cover - protocol
        ...


@dataclass
class _Entry:
    """An in-memory pairing entry with its absolute expiry time."""

    value: dict[str, Any]
    expires_at: float


class InMemoryPairingStore:
    """A dict-backed :class:`PairingStore` for tests and local development.

    TTL semantics mirror Redis: a key read after its TTL has elapsed behaves as
    a miss and the entry is removed. Time is read through an injectable
    ``clock`` (defaulting to :func:`time.monotonic`) so tests can advance the
    clock deterministically to exercise the 60-second timeout path without
    real sleeps.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._store: dict[str, _Entry] = {}

    async def get(self, key: str) -> Optional[dict[str, Any]]:
        entry = self._store.get(key)
        if entry is None:
            return None
        if self._clock() >= entry.expires_at:
            # Expired: behave as a miss and evict.
            self._store.pop(key, None)
            return None
        return dict(entry.value)

    async def set_with_ttl(
        self, key: str, value: dict[str, Any], ttl_seconds: int
    ) -> None:
        self._store[key] = _Entry(
            value=dict(value), expires_at=self._clock() + ttl_seconds
        )

    async def invalidate(self, key: str) -> None:
        self._store.pop(key, None)

    # -- test conveniences -------------------------------------------------

    def has_key(self, key: str) -> bool:
        """Return ``True`` when ``key`` is present and unexpired (test helper)."""
        entry = self._store.get(key)
        return entry is not None and self._clock() < entry.expires_at


class RedisPairingStore:
    """A :class:`PairingStore` backed by ``shared.cache.RedisCache``.

    Delegates to the cache's JSON helpers so the stored pairing entry survives a
    process restart and is shared across orchestrator replicas.
    """

    def __init__(self, cache: Any) -> None:
        self._cache = cache

    async def get(self, key: str) -> Optional[dict[str, Any]]:
        return await self._cache.get_json(key)

    async def set_with_ttl(
        self, key: str, value: dict[str, Any], ttl_seconds: int
    ) -> None:
        await self._cache.set_json_with_ttl(key, value, ttl_seconds)

    async def invalidate(self, key: str) -> None:
        await self._cache.invalidate(key)


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def _parse_timestamp(payload: dict[str, Any]) -> datetime:
    """Extract an event timestamp from a payload, defaulting to *now* (UTC).

    Looks for ``received_at`` first (the field carried by both ``claim.received``
    and ``roe.received`` payloads), then ``timestamp``. Naive datetimes are
    treated as UTC so comparisons are always timezone-aware.
    """
    raw = payload.get("received_at") or payload.get("timestamp")
    if raw is None:
        return datetime.now(timezone.utc)
    if isinstance(raw, datetime):
        dt = raw
    else:
        try:
            dt = datetime.fromisoformat(str(raw))
        except ValueError:
            return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# EventCorrelator
# ---------------------------------------------------------------------------


class EventCorrelator:
    """Correlate ``claim.received`` and ``roe.received`` events by ``claim_id``.

    The correlator is deliberately stateless beyond its pairing store: all
    partial state lives in the store keyed by ``orchestrator:pairing:{claim_id}``
    with a 60-second TTL, so multiple orchestrator replicas can share the work
    and a crash never leaves a permanent half-pair.

    Parameters
    ----------
    store:
        The pairing store backend (Redis in production, in-memory in tests).
    ttl_seconds:
        Correlation window. Defaults to ``ORCHESTRATOR_PAIRING_TTL_SECONDS``
        (60 s). The store expires an unpaired entry after this window, which is
        exactly the 60-second correlation timeout.
    """

    def __init__(
        self,
        store: PairingStore,
        *,
        ttl_seconds: int = ORCHESTRATOR_PAIRING_TTL_SECONDS,
    ) -> None:
        self._store = store
        self._ttl_seconds = ttl_seconds

    @property
    def ttl_seconds(self) -> int:
        """The correlation timeout window in seconds (60 by default)."""
        return self._ttl_seconds

    async def on_event(
        self, event_type: str, claim_id: str, payload: dict[str, Any]
    ) -> Optional[PairResult]:
        """Record an arriving event and pair it with its complement if present.

        Parameters
        ----------
        event_type:
            Either ``claim.received`` or ``roe.received``.
        claim_id:
            The claim identifier shared by both events.
        payload:
            The event payload (must include a ``received_at`` timestamp for the
            later-timestamp calculation; if absent, *now* is used).

        Returns
        -------
        PairResult | None
            A :class:`PairResult` when this event completes a pair (both
            payloads + the later timestamp); ``None`` when this is the first
            event of the pair and the correlator is still waiting.

        Raises
        ------
        ValueError
            If ``event_type`` is not one of the two recognised event types.
        """
        if event_type not in _COMPLEMENT:
            raise ValueError(
                f"Unknown event_type {event_type!r}; "
                f"expected one of {sorted(_COMPLEMENT)}."
            )

        key = orchestrator_pairing_key(claim_id)
        existing = await self._store.get(key)

        # First event of the pair (or the previous partial expired): store it.
        if existing is None:
            await self._store.set_with_ttl(
                key,
                {
                    "event_type": event_type,
                    "claim_id": claim_id,
                    "payload": payload,
                    "received_at": _parse_timestamp(payload).isoformat(),
                },
                self._ttl_seconds,
            )
            return None

        stored_event_type = existing.get("event_type")

        # Duplicate of the same event type: refresh the stored copy, keep waiting.
        if stored_event_type == event_type:
            await self._store.set_with_ttl(
                key,
                {
                    "event_type": event_type,
                    "claim_id": claim_id,
                    "payload": payload,
                    "received_at": _parse_timestamp(payload).isoformat(),
                },
                self._ttl_seconds,
            )
            return None

        # Complementary event present -> pair complete. Clear the partial entry.
        await self._store.invalidate(key)

        stored_payload = existing.get("payload", {})
        stored_ts = _parse_timestamp(existing)
        arriving_ts = _parse_timestamp(payload)
        later_ts = max(stored_ts, arriving_ts)

        if event_type == CLAIM_RECEIVED:
            claim_payload, roe_payload = payload, stored_payload
        else:
            claim_payload, roe_payload = stored_payload, payload

        return PairResult(
            claim_id=claim_id,
            claim_payload=claim_payload,
            roe_payload=roe_payload,
            later_timestamp=later_ts,
        )

    async def check_timeout(self, claim_id: str) -> Optional[TimeoutSignal]:
        """Detect whether a claim's pairing has timed out (partial only).

        Because the pairing store enforces the 60-second TTL, a lookup that
        misses means either the pair completed (entry invalidated) or the window
        elapsed with only one event. This method reports a :class:`TimeoutSignal`
        only when a *partial* entry is still present but its window has, from the
        store's perspective, expired — i.e. a subsequent ``get`` returns ``None``
        while the caller believed an event had arrived.

        In practice the caller schedules :meth:`check_timeout` ~60 s after the
        first event; if the store now reports a miss, the pairing never
        completed. Callers that want the surviving payload should capture it when
        the first event arrives; this helper returns ``None`` once the entry has
        expired, signalling "no live partial remains".

        Returns
        -------
        TimeoutSignal | None
            ``None`` when no partial entry remains (paired or fully expired). A
            :class:`TimeoutSignal` when a partial entry is still live at check
            time (the complement has not yet arrived).
        """
        key = orchestrator_pairing_key(claim_id)
        existing = await self._store.get(key)
        if existing is None:
            return None

        present_type = existing.get("event_type", "")
        missing_type = _COMPLEMENT.get(present_type, "")
        return TimeoutSignal(
            claim_id=claim_id,
            present_event_type=present_type,
            present_payload=existing.get("payload", {}),
            missing_event_type=missing_type,
        )
