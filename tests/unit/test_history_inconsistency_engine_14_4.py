"""
tests/unit/test_history_inconsistency_engine_14_4.py
=====================================================
Task 14.4 unit tests for the Employment History Inconsistency Engine.

This file covers the three specific boundary/failure scenarios called out by
task 14.4, exercising the task-14.1 (CRA T4 retrieval, earnings discrepancy)
and task-14.2 (concurrent employment) code that already exists:

  1. Earnings discrepancy boundary (Requirements 6.2, 6.4):
       - a difference of exactly $2,000.00 does NOT flag,
       - a difference of $2,000.01 produces one CRITICAL Indicator.
  2. CRA Adapter timeout (Requirement 6.1): a transport that times out within
     the 5-second budget maps to a single UNRESOLVED-severity Indicator and no
     further analysis runs.
  3. Concurrent employment with a same-day start (Requirement 6.8): a second
     employer whose span starts on the very first day of the claim period
     produces a CRITICAL Indicator identifying the overlapping BN and period.

Each scenario is asserted at BOTH levels where practical: directly against the
pure detection functions (`check_earnings_discrepancy`,
`detect_concurrent_employment`) and end-to-end through the FastAPI
`/engine/analyze` endpoint with a mocked CRA Adapter transport (no live
network). This is a distinct file from ``test_history_inconsistency_engine.py``
(14.1) and ``test_history_inconsistency_engine_14_2.py`` (14.2) and from the
concurrent 14.3 property test, so there are no write conflicts.

The service directory is hyphenated and its modules import each other by bare
name, so modules are loaded through the shared ``load_service_module`` helper
from ``tests/conftest.py`` (namespaced keys, collision-free on full-suite runs).

Requirements: 6.1, 6.2, 6.4, 6.8
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from shared.models import EngineType, Severity
from tests.conftest import load_service_module

# ---------------------------------------------------------------------------
# Load the hyphenated history-inconsistency-engine service modules through the
# shared, collision-free loader.
# ---------------------------------------------------------------------------

_SERVICE = "history-inconsistency-engine"
_SIBLINGS = [
    "config",
    "client",
    "discrepancy",
    "noc",
    "concurrent_employment",
    "narrative",
    "kafka_publisher",
    "analysis",
    "main",
]

_discrepancy = load_service_module(_SERVICE, "discrepancy", siblings=_SIBLINGS)
_concurrent = load_service_module(_SERVICE, "concurrent_employment", siblings=_SIBLINGS)
_client = load_service_module(_SERVICE, "client", siblings=_SIBLINGS)
_main = load_service_module(_SERVICE, "main", siblings=_SIBLINGS)

# Task 29.1: protected routes require the INVESTIGATOR role; inject a principal.
from shared.auth import get_current_user as _get_current_user  # noqa: E402

_main.app.dependency_overrides[_get_current_user] = lambda: {"roles": ["INVESTIGATOR"]}

check_earnings_discrepancy = _discrepancy.check_earnings_discrepancy
EmploymentSpan = _concurrent.EmploymentSpan
detect_concurrent_employment = _concurrent.detect_concurrent_employment
CRAAdapterClient = _client.CRAAdapterClient
CRAAdapterUnavailable = _client.CRAAdapterUnavailable

try:
    from fastapi.testclient import TestClient

    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001
    _HAS_FASTAPI = False


# ---------------------------------------------------------------------------
# Small helpers: async runner + mocked CRA transports (no live network).
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _t4_transport(record: dict[str, Any]):
    """A transport that returns an OK T4 record for the T4 endpoint and an
    empty employment-span list for the concurrent-employment endpoint."""

    async def _t(url: str, timeout: float) -> dict[str, Any]:
        if "/cra/employment/" in url:
            return {"status": "OK", "employment_spans": []}
        return {"status": "OK", "record": record}

    return _t


def _timeout_transport():
    """A transport that models a CRA Adapter timeout by raising within the
    5-second budget. The client maps any raise to an UNRESOLVED lookup."""

    async def _t(url: str, timeout: float) -> dict[str, Any]:
        # Simulate the adapter not answering within the timeout window: raise
        # the same signal the httpx-backed transport raises on timeout.
        raise CRAAdapterUnavailable(f"timed out after {timeout}s")

    return _t


def _t4_and_spans_transport(record: dict[str, Any], spans: list[dict[str, Any]]):
    """A transport serving a T4 record and a list of concurrent employment
    spans for the concurrent-employment endpoint."""

    async def _t(url: str, timeout: float) -> dict[str, Any]:
        if "/cra/employment/" in url:
            return {"status": "OK", "employment_spans": spans}
        return {"status": "OK", "record": record}

    return _t


def _make_client(transport) -> "CRAAdapterClient":
    return CRAAdapterClient(
        "http://cra-adapter:8011", timeout_seconds=5.0, transport=transport
    )


# ===========================================================================
# 1. Earnings discrepancy boundary: $2,000 (no flag) vs $2,000.01 (flag)
#    Requirements 6.2, 6.4
# ===========================================================================


class TestEarningsDiscrepancyBoundary14_4:
    def test_exactly_2000_difference_does_not_flag(self):
        """|ROE - T4| == $2,000.00 is on the threshold and must NOT flag."""
        assert (
            check_earnings_discrepancy(Decimal("7000.00"), Decimal("5000.00")) == []
        )

    def test_2000_01_difference_flags_single_critical(self):
        """|ROE - T4| == $2,000.01 exceeds the threshold -> one CRITICAL."""
        indicators = check_earnings_discrepancy(
            Decimal("7000.01"), Decimal("5000.00"), claim_id="claim-14-4"
        )
        assert len(indicators) == 1
        ind = indicators[0]
        assert ind.engine == EngineType.HISTORY_INCONSISTENCY
        assert ind.severity == Severity.CRITICAL
        assert ind.algorithm == "earnings_discrepancy"
        assert ind.payload["discrepancy_cad"] == "2000.01"

    def test_boundary_is_symmetric_when_t4_exceeds_roe(self):
        """The strict-greater boundary holds regardless of which side is larger."""
        # Exactly $2,000 with T4 > ROE: no flag.
        assert (
            check_earnings_discrepancy(Decimal("5000.00"), Decimal("7000.00")) == []
        )
        # $2,000.01 with T4 > ROE: one CRITICAL flag.
        flagged = check_earnings_discrepancy(
            Decimal("5000.00"), Decimal("7000.01")
        )
        assert len(flagged) == 1
        assert flagged[0].severity == Severity.CRITICAL


# ===========================================================================
# 2. CRA Adapter timeout -> UNRESOLVED Indicator (Requirement 6.1)
# ===========================================================================


class TestCRATimeoutUnresolved14_4:
    def test_client_maps_timeout_to_unresolved(self):
        """A timing-out transport yields an unresolved T4 lookup (CRA_UNAVAILABLE)."""
        client = _make_client(_timeout_transport())
        lookup = _run(client.fetch_t4("sin-1", 2024, "BN1"))
        assert lookup.unresolved is True
        assert lookup.t4_earnings is None
        assert lookup.reason == _client.REASON_UNAVAILABLE


# ===========================================================================
# 3. Concurrent employment with a same-day start -> CRITICAL (Requirement 6.8)
# ===========================================================================


class TestConcurrentSameDayStart14_4:
    def test_second_employer_starting_on_claim_start_flags_critical(self):
        """A second employer whose span begins on the claim's first day overlaps
        and must produce a CRITICAL Indicator naming the BN and period."""
        claim_start = date(2024, 3, 1)
        claim_end = date(2024, 3, 31)
        spans = [
            EmploymentSpan("BN_SECOND", claim_start, date(2024, 3, 10)),
        ]
        indicators = detect_concurrent_employment(
            spans,
            claim_start,
            claim_end,
            claim_employer_bn="BN_OWN",
            claim_id="claim-14-4",
        )
        assert len(indicators) == 1
        ind = indicators[0]
        assert ind.severity == Severity.CRITICAL
        assert ind.algorithm == "concurrent_employment"
        assert ind.payload["overlapping_employer_bn"] == "BN_SECOND"
        assert ind.payload["overlap_start_date"] == "2024-03-01"
        assert ind.payload["overlap_end_date"] == "2024-03-10"
        assert ind.payload["overlap_days"] == 10  # Mar 1..10 inclusive

    def test_own_employer_same_day_start_is_not_flagged(self):
        """The claimant's own employer span (same BN) is expected and ignored,
        even when it starts on the claim's first day."""
        claim_start = date(2024, 3, 1)
        claim_end = date(2024, 3, 31)
        spans = [EmploymentSpan("BN_OWN", claim_start, claim_end)]
        assert (
            detect_concurrent_employment(
                spans, claim_start, claim_end, claim_employer_bn="BN_OWN"
            )
            == []
        )


# ===========================================================================
# End-to-end through the FastAPI /engine/analyze endpoint (mocked CRA transport)
# ===========================================================================


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not importable")
class TestAnalyzeEndpoint14_4:
    def _wire(self, transport):
        _main.app.state.settings = _main.get_settings()
        _main.app.state.cra_client = _make_client(transport)
        _main.app.state.narrative_checker = None
        _main.app.state.indicator_publisher = _main.IndicatorPublisher(None)
        return TestClient(_main.app)

    def _payload(self, **overrides):
        payload = {
            "sin_hash": "sin-abc",
            "tax_year": 2024,
            "employer_bn": "BN_OWN",
            "roe_earnings": "7000.00",
        }
        payload.update(overrides)
        return payload

    def test_endpoint_boundary_exactly_2000_no_indicator(self):
        client = self._wire(_t4_transport({"employment_income": "5000.00"}))
        resp = client.post("/engine/analyze", json=self._payload(roe_earnings="7000.00"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "OK"
        assert body["indicators"] == []

    def test_endpoint_boundary_2000_01_returns_critical(self):
        client = self._wire(_t4_transport({"employment_income": "5000.00"}))
        resp = client.post(
            "/engine/analyze", json=self._payload(roe_earnings="7000.01")
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "OK"
        assert len(body["indicators"]) == 1
        ind = body["indicators"][0]
        assert ind["severity"] == Severity.CRITICAL.value
        assert ind["payload"]["discrepancy_cad"] == "2000.01"

    def test_endpoint_cra_timeout_returns_single_unresolved(self):
        client = self._wire(_timeout_transport())
        resp = client.post("/engine/analyze", json=self._payload())
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "UNRESOLVED"
        assert len(body["indicators"]) == 1
        ind = body["indicators"][0]
        assert ind["severity"] == Severity.UNRESOLVED.value
        assert ind["engine"] == EngineType.HISTORY_INCONSISTENCY.value
        assert ind["algorithm"] == "cra_t4_retrieval"
        assert ind["payload"]["reason"] == _client.REASON_UNAVAILABLE

    def test_endpoint_concurrent_same_day_start_returns_critical(self):
        record = {"employment_income": "5000.00", "noc_category": "72101"}
        spans = [
            {
                "employer_bn": "BN_SECOND",
                "start_date": "2024-03-01",  # same day as the claim start
                "end_date": "2024-03-10",
            }
        ]
        client = self._wire(_t4_and_spans_transport(record, spans))
        resp = client.post(
            "/engine/analyze",
            json=self._payload(
                roe_earnings="5000.00",  # equal to T4 -> no earnings discrepancy
                declared_noc="72630",  # same major group 72 -> no NOC mismatch
                claim_start_date="2024-03-01",
                claim_end_date="2024-03-31",
            ),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "OK"
        assert len(body["indicators"]) == 1
        ind = body["indicators"][0]
        assert ind["severity"] == Severity.CRITICAL.value
        assert ind["algorithm"] == "concurrent_employment"
        assert ind["payload"]["overlapping_employer_bn"] == "BN_SECOND"
        assert ind["payload"]["overlap_start_date"] == "2024-03-01"
        assert ind["payload"]["overlap_days"] == 10
