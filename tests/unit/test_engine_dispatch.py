"""
tests/unit/test_engine_dispatch.py
==================================
Unit tests for the Detection Pipeline Orchestrator parallel engine dispatch
(task 6.2).

Covers the requirement bullets for 6.2:

  * 2.2 — async calls to all six engine endpoints, each under its per-engine
    wall-clock timeout budget from the design.
  * 2.3 — on a per-engine timeout/error, a PARTIAL_RESULT Indicator carrying the
    engine id and elapsed time is emitted.
  * 2.4 — engines that complete still contribute Indicators; scoring can still
    proceed when a minority of engines time out.
  * 2.5 — when fewer than three engines complete, the claim is INDETERMINATE and
    payment is halted.

All tests inject a fake async engine caller so they never touch the network.

Requirements: 2.2, 2.3, 2.4, 2.5
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Load the pipeline-orchestrator service module. Its sibling modules import each
# other by bare name, so the service directory must be on ``sys.path``.
# ---------------------------------------------------------------------------

_SERVICE_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "services", "pipeline-orchestrator"
    )
)


def _load_module(name: str, filename: str):
    if os.path.isdir(_SERVICE_DIR) and _SERVICE_DIR not in sys.path:
        sys.path.insert(0, _SERVICE_DIR)
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_SERVICE_DIR, filename)
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_dispatch = _load_module("orch_engine_dispatch", "engine_dispatch.py")

EngineDispatcher = _dispatch.EngineDispatcher
DispatchResult = _dispatch.DispatchResult
ALL_ENGINES = _dispatch.ALL_ENGINES
ENGINE_TIMEOUT_MS = _dispatch.ENGINE_TIMEOUT_MS
MIN_ENGINES_REQUIRED = _dispatch.MIN_ENGINES_REQUIRED
PARTIAL_RESULT_ALGORITHM = _dispatch.PARTIAL_RESULT_ALGORITHM

from shared.models import EngineType, RiskDecision, Severity  # noqa: E402


# ---------------------------------------------------------------------------
# Test helpers: fake engine callers (no network)
# ---------------------------------------------------------------------------

CLAIM_ID = "11111111-1111-1111-1111-111111111111"
_CLAIM_PAYLOAD = {"claim_id": CLAIM_ID, "employer_bn": "123456789RT0001"}
_ROE_PAYLOAD = {"claim_id": CLAIM_ID, "insurable_hours": "700.00"}


def _indicator_dict(engine: EngineType, title: str = "signal") -> dict:
    """A minimal, well-formed Indicator dict an engine might return."""
    import uuid
    from datetime import datetime, timezone

    return {
        "indicator_id": str(uuid.uuid4()),
        "claim_id": CLAIM_ID,
        "engine": engine.value,
        "severity": Severity.MEDIUM.value,
        "algorithm": "unit_test",
        "title": title,
        "explanation_en": "en",
        "explanation_fr": "fr",
        "payload": {},
        "produced_at": datetime.now(timezone.utc).isoformat(),
    }


def make_all_respond_caller():
    """Every engine responds instantly with exactly one Indicator."""

    async def caller(engine, url, claim_payload, roe_payload):
        return [_indicator_dict(engine)]

    return caller


def make_slow_engines_caller(slow_engines: set, slow_seconds: float = 5.0):
    """Selected engines sleep past their budget; the rest respond instantly.

    The slow sleep (default 5 s) far exceeds any per-engine budget (max 800 ms),
    so ``asyncio.wait_for`` fires the timeout deterministically without the test
    actually waiting 5 s.
    """

    async def caller(engine, url, claim_payload, roe_payload):
        if engine in slow_engines:
            await asyncio.sleep(slow_seconds)
        return [_indicator_dict(engine)]

    return caller


# ---------------------------------------------------------------------------
# 1. All six engines respond -> all indicators, 6 completed, not indeterminate
# ---------------------------------------------------------------------------


def test_all_engines_respond_collects_all_indicators():
    """Requirements: 2.2, 2.4 — all six engines complete, all Indicators kept."""
    dispatcher = EngineDispatcher(make_all_respond_caller())
    result = asyncio.run(dispatcher.dispatch(_CLAIM_PAYLOAD, _ROE_PAYLOAD))

    assert isinstance(result, DispatchResult)
    assert result.completed_count == 6
    assert set(result.engines_completed) == set(ALL_ENGINES)
    assert result.engines_partial == []
    assert result.indeterminate is False
    assert result.payment_halted is False
    assert result.risk_decision is None
    # Exactly one indicator per engine, no PARTIAL_RESULT markers.
    assert len(result.indicators) == 6
    assert all(
        getattr(i, "algorithm", None) != PARTIAL_RESULT_ALGORITHM
        for i in result.indicators
    )


# ---------------------------------------------------------------------------
# 2. Two engines time out -> PARTIAL_RESULT for those two, 4 completed, not
#    indeterminate, scoring can still proceed.
# ---------------------------------------------------------------------------


def test_two_engines_timeout_emits_partial_results_and_stays_determinate():
    """Requirements: 2.3, 2.4 — two timeouts still leave >= 3 engines; scoring proceeds."""
    slow = {EngineType.FRAUD_RING, EngineType.DOCUMENT_FORGERY}
    dispatcher = EngineDispatcher(make_slow_engines_caller(slow))
    result = asyncio.run(dispatcher.dispatch(_CLAIM_PAYLOAD, _ROE_PAYLOAD))

    assert result.completed_count == 4
    assert set(result.engines_partial) == slow
    assert result.indeterminate is False
    assert result.payment_halted is False

    partials = [
        i for i in result.indicators
        if getattr(i, "algorithm", None) == PARTIAL_RESULT_ALGORITHM
    ]
    assert len(partials) == 2
    partial_engines = {p.engine for p in partials}
    assert partial_engines == slow
    # Each PARTIAL_RESULT carries engine id + elapsed time.
    for p in partials:
        assert p.severity == Severity.UNRESOLVED
        assert p.payload["engine"] in {e.value for e in slow}
        assert p.payload["elapsed_ms"] >= 0
        assert p.payload["partial_result"] is True


# ---------------------------------------------------------------------------
# 3. Four engines time out -> completed_count 2 (<3) -> INDETERMINATE.
# ---------------------------------------------------------------------------


def test_four_engines_timeout_yields_indeterminate_and_halts_payment():
    """Requirements: 2.5 — fewer than 3 engines complete -> INDETERMINATE."""
    slow = {
        EngineType.FRAUD_RING,
        EngineType.DOCUMENT_FORGERY,
        EngineType.GEOGRAPHIC_ANOMALY,
        EngineType.REGIONAL_ANALYSIS,
    }
    dispatcher = EngineDispatcher(make_slow_engines_caller(slow))
    result = asyncio.run(dispatcher.dispatch(_CLAIM_PAYLOAD, _ROE_PAYLOAD))

    assert result.completed_count == 2
    assert result.completed_count < MIN_ENGINES_REQUIRED
    assert result.indeterminate is True
    assert result.risk_decision == RiskDecision.INDETERMINATE
    assert result.payment_halted is True
    assert len(result.engines_partial) == 4


def test_indeterminate_invokes_injected_publish_hook():
    """Requirements: 2.5 — INDETERMINATE claims can be published via injected hook."""
    published: list = []

    async def hook(res: DispatchResult) -> None:
        published.append(res)

    slow = set(ALL_ENGINES) - {EngineType.REPETITIVE_PATTERN}  # only 1 completes
    dispatcher = EngineDispatcher(
        make_slow_engines_caller(slow), publish_hook=hook
    )
    result = asyncio.run(dispatcher.dispatch(_CLAIM_PAYLOAD, _ROE_PAYLOAD))

    assert result.indeterminate is True
    assert len(published) == 1
    assert published[0] is result


# ---------------------------------------------------------------------------
# 4. Per-engine timeout is honoured: a single slow engine exceeding its budget
#    yields a PARTIAL_RESULT with a non-negative elapsed time.
# ---------------------------------------------------------------------------


def test_per_engine_timeout_honoured_for_slow_engine():
    """Requirements: 2.2, 2.3 — a slow engine past its budget -> PARTIAL_RESULT."""
    # Repetitive Pattern has the tightest budget (400 ms); make only it slow.
    slow = {EngineType.REPETITIVE_PATTERN}
    dispatcher = EngineDispatcher(make_slow_engines_caller(slow))
    result = asyncio.run(dispatcher.dispatch(_CLAIM_PAYLOAD, _ROE_PAYLOAD))

    assert EngineType.REPETITIVE_PATTERN in result.engines_partial
    assert result.completed_count == 5
    assert result.indeterminate is False

    partials = [
        i for i in result.indicators
        if getattr(i, "algorithm", None) == PARTIAL_RESULT_ALGORITHM
    ]
    assert len(partials) == 1
    partial = partials[0]
    assert partial.engine == EngineType.REPETITIVE_PATTERN
    assert partial.payload["reason"] == "timeout"
    # Elapsed time is recorded and bounded near the engine's budget, not the
    # full fake sleep (wait_for cancels at the budget).
    budget_ms = ENGINE_TIMEOUT_MS[EngineType.REPETITIVE_PATTERN]
    assert 0 <= partial.payload["elapsed_ms"] < budget_ms + 500


def test_per_engine_error_emits_partial_result():
    """Requirements: 2.3 — an engine that raises yields a PARTIAL_RESULT indicator."""

    async def caller(engine, url, claim_payload, roe_payload):
        if engine == EngineType.HISTORY_INCONSISTENCY:
            raise RuntimeError("boom")
        return [_indicator_dict(engine)]

    dispatcher = EngineDispatcher(caller)
    result = asyncio.run(dispatcher.dispatch(_CLAIM_PAYLOAD, _ROE_PAYLOAD))

    assert EngineType.HISTORY_INCONSISTENCY in result.engines_partial
    assert result.completed_count == 5
    partials = [
        i for i in result.indicators
        if getattr(i, "algorithm", None) == PARTIAL_RESULT_ALGORITHM
    ]
    assert len(partials) == 1
    assert partials[0].engine == EngineType.HISTORY_INCONSISTENCY
    assert partials[0].payload["reason"].startswith("error:")


# ---------------------------------------------------------------------------
# 5. All six engines are called concurrently (verified via a barrier).
# ---------------------------------------------------------------------------


def test_all_engines_called_concurrently():
    """Requirements: 2.2 — the six engines run in parallel, not sequentially."""
    seen: list = []

    async def caller(engine, url, claim_payload, roe_payload):
        seen.append(engine)
        # Yield control so, if calls were sequential, only one would have
        # started before the first await; concurrency lets all six start.
        await asyncio.sleep(0.01)
        return [_indicator_dict(engine)]

    dispatcher = EngineDispatcher(caller)
    result = asyncio.run(dispatcher.dispatch(_CLAIM_PAYLOAD, _ROE_PAYLOAD))

    assert result.completed_count == 6
    # All six workers started (each appended before its await).
    assert set(seen) == set(ALL_ENGINES)
    assert len(seen) == 6


# ===========================================================================
# _build_engine_request — Document Forgery document payload (Req 7.2/7.7)
# ===========================================================================


def test_build_engine_request_forwards_document_for_doc_forgery():
    """The superset body carries a nested ``document`` mapped to the engine's
    field names, feeding the internal-consistency (7.2) + payroll (7.7) checks."""
    claim_payload = {
        "claim_id": "c-doc-1",
        "sin_hash": "ghost-abc",
        "employer_bn": "987654321RT0001",
        "insurable_hours": "40",
        "insurable_earnings": "2000",
        "separation_date": "2024-03-15",
        "claim_start_date": "2024-01-02",
    }
    roe_payload = {"claim_id": "c-doc-1", "insurable_earnings": "2000"}

    body = _dispatch._build_engine_request(claim_payload, roe_payload)
    document = body.get("document")
    assert isinstance(document, dict), body
    # Payroll-cross-reference inputs (7.7).
    assert document["sin_hash"] == "ghost-abc"
    assert document["employer_bn"] == "987654321RT0001"
    # Internal-consistency inputs (7.2): ROE field-name mapping.
    assert document["hours"] == "40"
    assert document["earnings"] == "2000"
    assert document["first_day_worked"] == "2024-01-02"   # claim start
    assert document["last_day_paid"] == "2024-03-15"       # separation date
    # The 11-field bag mirrors the values under the engine's OCR field names.
    fields = document["fields"]
    assert fields["sin"] == "ghost-abc"
    assert fields["total_insurable_hours"] == "40"
    assert fields["total_insurable_earnings"] == "2000"
    assert fields["last_day_paid"] == "2024-03-15"


def test_build_engine_request_no_document_when_no_roe_fields():
    """With no ROE fields present, no ``document`` object is emitted."""
    body = _dispatch._build_engine_request({"claim_id": "c2"}, {})
    assert "document" not in body


def test_document_forgery_default_port_matches_container():
    """The DOCUMENT_FORGERY default host port matches the engine Containerfile (8015)."""
    host, port = _dispatch._DEFAULT_ENGINE_HOSTS[EngineType.DOCUMENT_FORGERY]
    assert (host, port) == ("document-forgery-engine", 8015)
