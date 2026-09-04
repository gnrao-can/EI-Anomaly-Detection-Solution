"""
tests/unit/test_correlator.py
=============================
Unit tests for the Detection Pipeline Orchestrator event correlation (task 6.1).

Covers the two requirement bullets for 6.1:

  * 60-second correlation timeout using the pairing store's TTL, and
  * triggering parallel engine dispatch within 100 ms of the later event
    timestamp (via the consumer's dispatch callback).

All tests use the in-memory :class:`InMemoryPairingStore` (with an injectable
clock) so they run without a live Kafka broker or Redis instance.

Scenarios covered:
  1. claim.received then roe.received -> pairs.
  2. roe.received then claim.received -> pairs.
  3. Only one event -> no pairing (still waiting).
  4. A pairing returns both payloads and the later timestamp.
  5. The dispatch callback is invoked exactly once on a pairing.
  6. The 60-second timeout path is detectable (partial expires -> no pairing).
  7. Dispatch is triggered within the 100 ms budget of the later event.

Requirements: 2.1
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

# ---------------------------------------------------------------------------
# Load the pipeline-orchestrator service modules. Their sibling modules import
# each other by bare name, so the service directory must be on ``sys.path``.
# ---------------------------------------------------------------------------

_SERVICE_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "services", "pipeline-orchestrator"
    )
)


def _load_module(name: str, filename: str):
    added = False
    if os.path.isdir(_SERVICE_DIR) and _SERVICE_DIR not in sys.path:
        sys.path.insert(0, _SERVICE_DIR)
        added = True
    try:
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(_SERVICE_DIR, filename)
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if added and _SERVICE_DIR in sys.path:
            # Keep it on the path so consumer.py can import correlator by name.
            pass


_correlator = _load_module("orch_correlator", "correlator.py")
_consumer = _load_module("orch_consumer", "consumer.py")

EventCorrelator = _correlator.EventCorrelator
InMemoryPairingStore = _correlator.InMemoryPairingStore
PairResult = _correlator.PairResult
CLAIM_RECEIVED = _correlator.CLAIM_RECEIVED
ROE_RECEIVED = _correlator.ROE_RECEIVED
orchestrator_pairing_key = _correlator.orchestrator_pairing_key
OrchestratorConsumer = _consumer.OrchestratorConsumer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeClock:
    """A controllable monotonic clock for deterministic TTL testing."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _claim_payload(claim_id: str, ts: datetime) -> dict:
    return {
        "claim_id": claim_id,
        "sin_hash": "a" * 64,
        "employer_bn": "123456789RT0001",
        "received_at": ts.isoformat(),
        "status": "RECEIVED",
    }


def _roe_payload(claim_id: str, ts: datetime) -> dict:
    return {
        "claim_id": claim_id,
        "sin_hash": "a" * 64,
        "employer_bn": "123456789RT0001",
        "received_at": ts.isoformat(),
        "source": "roe-web",
    }


# ---------------------------------------------------------------------------
# 1 & 2: pairing in both orders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_then_roe_pairs():
    store = InMemoryPairingStore(clock=_FakeClock())
    correlator = EventCorrelator(store, ttl_seconds=60)
    claim_id = "11111111-1111-1111-1111-111111111111"
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    first = await correlator.on_event(
        CLAIM_RECEIVED, claim_id, _claim_payload(claim_id, t0)
    )
    assert first is None  # still waiting

    result = await correlator.on_event(
        ROE_RECEIVED, claim_id, _roe_payload(claim_id, t0 + timedelta(seconds=5))
    )
    assert isinstance(result, PairResult)
    assert result.claim_id == claim_id
    assert result.claim_payload["status"] == "RECEIVED"
    assert result.roe_payload["source"] == "roe-web"


@pytest.mark.asyncio
async def test_roe_then_claim_pairs():
    store = InMemoryPairingStore(clock=_FakeClock())
    correlator = EventCorrelator(store, ttl_seconds=60)
    claim_id = "22222222-2222-2222-2222-222222222222"
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    first = await correlator.on_event(
        ROE_RECEIVED, claim_id, _roe_payload(claim_id, t0)
    )
    assert first is None

    result = await correlator.on_event(
        CLAIM_RECEIVED, claim_id, _claim_payload(claim_id, t0 + timedelta(seconds=3))
    )
    assert isinstance(result, PairResult)
    # Regardless of arrival order, payloads land in the correct slots.
    assert result.claim_payload["status"] == "RECEIVED"
    assert result.roe_payload["source"] == "roe-web"


# ---------------------------------------------------------------------------
# 3: only one event -> no pairing (waiting)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_event_does_not_pair():
    store = InMemoryPairingStore(clock=_FakeClock())
    correlator = EventCorrelator(store, ttl_seconds=60)
    claim_id = "33333333-3333-3333-3333-333333333333"
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    result = await correlator.on_event(
        CLAIM_RECEIVED, claim_id, _claim_payload(claim_id, t0)
    )
    assert result is None
    # The partial entry is stored under the pairing key.
    assert store.has_key(orchestrator_pairing_key(claim_id))


@pytest.mark.asyncio
async def test_duplicate_event_still_waiting():
    store = InMemoryPairingStore(clock=_FakeClock())
    correlator = EventCorrelator(store, ttl_seconds=60)
    claim_id = "44444444-4444-4444-4444-444444444444"
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    assert await correlator.on_event(CLAIM_RECEIVED, claim_id, _claim_payload(claim_id, t0)) is None
    # A second claim.received (duplicate) must not falsely pair.
    assert (
        await correlator.on_event(
            CLAIM_RECEIVED, claim_id, _claim_payload(claim_id, t0 + timedelta(seconds=1))
        )
        is None
    )


# ---------------------------------------------------------------------------
# 4: pairing returns both payloads and the later timestamp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pairing_uses_later_timestamp():
    store = InMemoryPairingStore(clock=_FakeClock())
    correlator = EventCorrelator(store, ttl_seconds=60)
    claim_id = "55555555-5555-5555-5555-555555555555"
    t_claim = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    t_roe = t_claim + timedelta(seconds=12)

    await correlator.on_event(CLAIM_RECEIVED, claim_id, _claim_payload(claim_id, t_claim))
    result = await correlator.on_event(ROE_RECEIVED, claim_id, _roe_payload(claim_id, t_roe))

    assert result is not None
    assert result.later_timestamp == t_roe  # the later of the two

    # And when claim arrives second, the claim's later timestamp wins.
    claim_id2 = "66666666-6666-6666-6666-666666666666"
    t_roe2 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    t_claim2 = t_roe2 + timedelta(seconds=8)
    await correlator.on_event(ROE_RECEIVED, claim_id2, _roe_payload(claim_id2, t_roe2))
    result2 = await correlator.on_event(
        CLAIM_RECEIVED, claim_id2, _claim_payload(claim_id2, t_claim2)
    )
    assert result2 is not None
    assert result2.later_timestamp == t_claim2


# ---------------------------------------------------------------------------
# 5: dispatch callback invoked exactly once on pairing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_callback_invoked_once_on_pairing():
    store = InMemoryPairingStore(clock=_FakeClock())
    correlator = EventCorrelator(store, ttl_seconds=60)

    calls: list[PairResult] = []

    async def dispatch(result: PairResult) -> None:
        calls.append(result)

    consumer = OrchestratorConsumer(
        correlator, dispatch, dispatch_budget_ms=100, degrade_gracefully=True
    )

    claim_id = "77777777-7777-7777-7777-777777777777"
    now = datetime.now(timezone.utc)

    envelope_claim = {
        "event_type": CLAIM_RECEIVED,
        "claim_id": claim_id,
        "payload": _claim_payload(claim_id, now),
    }
    envelope_roe = {
        "event_type": ROE_RECEIVED,
        "claim_id": claim_id,
        "payload": _roe_payload(claim_id, now),
    }

    # First event: no dispatch yet.
    assert await consumer.handle_message(envelope_claim) is None
    assert calls == []

    # Second event completes the pair: dispatch fires exactly once.
    result = await consumer.handle_message(envelope_roe)
    assert result is not None
    assert len(calls) == 1
    assert calls[0].claim_id == claim_id


# ---------------------------------------------------------------------------
# 6: 60-second timeout path is detectable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_expires_partial_no_pairing():
    clock = _FakeClock()
    store = InMemoryPairingStore(clock=clock)
    correlator = EventCorrelator(store, ttl_seconds=60)
    claim_id = "88888888-8888-8888-8888-888888888888"
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    # Only the claim event arrives.
    assert await correlator.on_event(CLAIM_RECEIVED, claim_id, _claim_payload(claim_id, t0)) is None

    # Before the window elapses, a partial entry is still present (detectable).
    clock.advance(59)
    signal = await correlator.check_timeout(claim_id)
    assert signal is not None
    assert signal.present_event_type == CLAIM_RECEIVED
    assert signal.missing_event_type == ROE_RECEIVED

    # After 60 s the partial entry has expired: the complement now can't pair.
    clock.advance(2)  # total 61s > 60s TTL
    assert await correlator.check_timeout(claim_id) is None

    # A late roe.received now starts a fresh partial rather than pairing.
    late = await correlator.on_event(
        ROE_RECEIVED, claim_id, _roe_payload(claim_id, t0 + timedelta(seconds=61))
    )
    assert late is None


# ---------------------------------------------------------------------------
# 7: dispatch triggered within the 100 ms budget of the later event timestamp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_within_100ms_budget():
    store = InMemoryPairingStore(clock=_FakeClock())
    correlator = EventCorrelator(store, ttl_seconds=60)

    dispatch_times: list[datetime] = []

    async def dispatch(result: PairResult) -> None:
        dispatch_times.append(datetime.now(timezone.utc))

    consumer = OrchestratorConsumer(
        correlator, dispatch, dispatch_budget_ms=100, degrade_gracefully=True
    )

    claim_id = "99999999-9999-9999-9999-999999999999"
    # Use the current time as the later event timestamp so the budget is live.
    now = datetime.now(timezone.utc)

    await consumer.handle_message(
        {"event_type": CLAIM_RECEIVED, "claim_id": claim_id, "payload": _claim_payload(claim_id, now)}
    )
    later_ts = datetime.now(timezone.utc)
    await consumer.handle_message(
        {"event_type": ROE_RECEIVED, "claim_id": claim_id, "payload": _roe_payload(claim_id, later_ts)}
    )

    assert len(dispatch_times) == 1
    elapsed_ms = (dispatch_times[0] - later_ts).total_seconds() * 1000.0
    # Dispatch is triggered essentially immediately, well within 100 ms.
    assert elapsed_ms <= 100
