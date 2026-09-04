"""
tests/unit/test_history_inconsistency_engine_14_2.py
=====================================================
Unit tests for task 14.2 of the Employment History Inconsistency Engine:
NOC validation, concurrent-employment detection, the async LLM
narrative-contradiction check, and the 500 ms wall-clock budget / Kafka
publication.

Coverage
--------
  * NOC major-group derivation and mismatch -> HIGH Indicator (Requirement 6.5).
  * Concurrent employment overlap arithmetic and second-employer detection ->
    CRITICAL Indicator with BN, start/end dates, and day count (Requirement 6.8).
  * LLM contradiction -> CRITICAL Indicator (Requirement 6.6); LLM timeout ->
    NARRATIVE_ANALYSIS_TIMEOUT flag (Requirement 6.7).
  * Core analysis (NOC + concurrent) stays within the 500 ms budget, excluding
    the LLM wait (Requirement 6.9); Indicators are published to the
    ``indicators.history-inconsistency`` topic.
  * Endpoint integration: a claim with a NOC mismatch, a concurrent employer,
    and an LLM contradiction yields all three Indicators and publishes them.

The service directory is hyphenated and its modules import each other by bare
name, so we load them through the shared ``load_service_module`` helper from
``tests/conftest.py`` (namespaced keys, no collisions on full-suite runs).

Requirements: 6.5, 6.6, 6.7, 6.8, 6.9
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import pytest

from shared.models import EngineType, Severity
from tests.conftest import load_service_module

# ---------------------------------------------------------------------------
# Load the hyphenated history-inconsistency-engine service modules.
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

_noc = load_service_module(_SERVICE, "noc", siblings=_SIBLINGS)
_concurrent = load_service_module(_SERVICE, "concurrent_employment", siblings=_SIBLINGS)
_narrative = load_service_module(_SERVICE, "narrative", siblings=_SIBLINGS)
_kafka = load_service_module(_SERVICE, "kafka_publisher", siblings=_SIBLINGS)
_analysis = load_service_module(_SERVICE, "analysis", siblings=_SIBLINGS)
_client = load_service_module(_SERVICE, "client", siblings=_SIBLINGS)
_main = load_service_module(_SERVICE, "main", siblings=_SIBLINGS)

# Task 29.1: protected routes require the INVESTIGATOR role; inject a principal.
from shared.auth import get_current_user as _get_current_user  # noqa: E402

_main.app.dependency_overrides[_get_current_user] = lambda: {"roles": ["INVESTIGATOR"]}

check_noc_mismatch = _noc.check_noc_mismatch
noc_major_group = _noc.noc_major_group
EmploymentSpan = _concurrent.EmploymentSpan
detect_concurrent_employment = _concurrent.detect_concurrent_employment
compute_overlap = _concurrent.compute_overlap
NarrativeContradictionChecker = _narrative.NarrativeContradictionChecker
NARRATIVE_ANALYSIS_TIMEOUT = _narrative.NARRATIVE_ANALYSIS_TIMEOUT
IndicatorPublisher = _kafka.IndicatorPublisher
run_core_analysis = _analysis.run_core_analysis
run_narrative_check = _analysis.run_narrative_check
CRAAdapterClient = _client.CRAAdapterClient

try:
    from fastapi.testclient import TestClient

    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001
    _HAS_FASTAPI = False


def _run(coro):
    return asyncio.run(coro)


# ===========================================================================
# NOC validation (Requirement 6.5)
# ===========================================================================


class TestNocValidation:
    def test_major_group_is_first_two_digits(self):
        assert noc_major_group("72630") == "72"
        assert noc_major_group("7263") == "72"
        assert noc_major_group(" 21231 ") == "21"

    def test_single_digit_category_preserved(self):
        assert noc_major_group("7") == "7"

    def test_non_numeric_is_none(self):
        assert noc_major_group("abc") is None
        assert noc_major_group(None) is None

    def test_matching_major_groups_no_indicator(self):
        # Both resolve to major group 72.
        assert check_noc_mismatch("72630", "72101") == []

    def test_single_digit_category_matches_broad_category(self):
        # CRA category "7" vs a 7xxxx NOC share the broad category -> no flag.
        assert check_noc_mismatch("72630", "7") == []

    def test_mismatch_yields_high_indicator(self):
        indicators = check_noc_mismatch("72630", "21231", claim_id="claim-1")
        assert len(indicators) == 1
        ind = indicators[0]
        assert ind.engine == EngineType.HISTORY_INCONSISTENCY
        assert ind.severity == Severity.HIGH
        assert ind.algorithm == "noc_industry_validation"
        assert ind.payload["declared_noc_major_group"] == "72"
        assert ind.payload["cra_noc_major_group"] == "21"

    def test_missing_side_does_not_flag(self):
        assert check_noc_mismatch("72630", None) == []
        assert check_noc_mismatch(None, "21231") == []


# ===========================================================================
# Concurrent employment (Requirement 6.8)
# ===========================================================================


class TestConcurrentEmployment:
    def test_overlap_inclusive_days(self):
        span = EmploymentSpan("BN2", date(2024, 1, 10), date(2024, 1, 20))
        overlap = compute_overlap(span, date(2024, 1, 15), date(2024, 1, 25))
        assert overlap is not None
        assert overlap.start_date == date(2024, 1, 15)
        assert overlap.end_date == date(2024, 1, 20)
        assert overlap.days == 6  # 15..20 inclusive

    def test_single_shared_day_overlaps_by_one(self):
        span = EmploymentSpan("BN2", date(2024, 1, 1), date(2024, 1, 15))
        overlap = compute_overlap(span, date(2024, 1, 15), date(2024, 1, 31))
        assert overlap is not None
        assert overlap.days == 1

    def test_no_overlap_returns_none(self):
        span = EmploymentSpan("BN2", date(2024, 2, 1), date(2024, 2, 10))
        assert compute_overlap(span, date(2024, 1, 1), date(2024, 1, 31)) is None

    def test_open_ended_span_extends_to_claim_end(self):
        span = EmploymentSpan("BN2", date(2024, 1, 5), None)
        overlap = compute_overlap(span, date(2024, 1, 1), date(2024, 1, 31))
        assert overlap is not None
        assert overlap.end_date == date(2024, 1, 31)

    def test_second_employer_yields_critical_indicator(self):
        spans = [
            EmploymentSpan("BN_OWN", date(2024, 1, 1), date(2024, 1, 31)),
            EmploymentSpan("BN_SECOND", date(2024, 1, 10), date(2024, 1, 20)),
        ]
        indicators = detect_concurrent_employment(
            spans,
            date(2024, 1, 1),
            date(2024, 1, 31),
            claim_employer_bn="BN_OWN",
            claim_id="claim-1",
        )
        assert len(indicators) == 1
        ind = indicators[0]
        assert ind.severity == Severity.CRITICAL
        assert ind.algorithm == "concurrent_employment"
        assert ind.payload["overlapping_employer_bn"] == "BN_SECOND"
        assert ind.payload["overlap_start_date"] == "2024-01-10"
        assert ind.payload["overlap_end_date"] == "2024-01-20"
        assert ind.payload["overlap_days"] == 11

    def test_same_day_start_second_employer_flags(self):
        spans = [
            EmploymentSpan("BN_SECOND", date(2024, 1, 1), date(2024, 1, 5)),
        ]
        indicators = detect_concurrent_employment(
            spans,
            date(2024, 1, 1),
            date(2024, 1, 31),
            claim_employer_bn="BN_OWN",
        )
        assert len(indicators) == 1
        assert indicators[0].severity == Severity.CRITICAL

    def test_own_employer_ignored(self):
        spans = [EmploymentSpan("BN_OWN", date(2024, 1, 1), date(2024, 1, 31))]
        indicators = detect_concurrent_employment(
            spans, date(2024, 1, 1), date(2024, 1, 31), claim_employer_bn="BN_OWN"
        )
        assert indicators == []


# ===========================================================================
# LLM narrative contradiction (Requirements 6.6, 6.7)
# ===========================================================================


class TestNarrativeCheck:
    def test_contradiction_yields_critical(self):
        async def fake_llm(narrative: str, evidence: str) -> dict[str, Any]:
            return {"contradicted": True, "evidence": "still on payroll"}

        checker = NarrativeContradictionChecker(fake_llm, timeout_seconds=10.0)
        indicators = _run(checker.check("laid off", "T4 shows income", claim_id="c1"))
        assert len(indicators) == 1
        ind = indicators[0]
        assert ind.severity == Severity.CRITICAL
        assert ind.algorithm == "llm_narrative_contradiction"
        assert ind.payload["contradicted"] is True
        assert ind.payload["evidence"] == "still on payroll"

    def test_no_contradiction_no_indicator(self):
        async def fake_llm(narrative: str, evidence: str) -> dict[str, Any]:
            return {"contradicted": False}

        checker = NarrativeContradictionChecker(fake_llm)
        assert _run(checker.check("laid off", "evidence")) == []

    def test_timeout_yields_flag(self):
        async def slow_llm(narrative: str, evidence: str) -> dict[str, Any]:
            await asyncio.sleep(1.0)
            return {"contradicted": True}

        # A very short timeout forces the timeout path deterministically.
        checker = NarrativeContradictionChecker(slow_llm, timeout_seconds=0.01)
        indicators = _run(checker.check("laid off", "evidence", claim_id="c1"))
        assert len(indicators) == 1
        ind = indicators[0]
        assert ind.severity == Severity.UNRESOLVED
        assert ind.payload["flag"] == NARRATIVE_ANALYSIS_TIMEOUT

    def test_llm_error_is_flagged_not_raised(self):
        async def boom_llm(narrative: str, evidence: str) -> dict[str, Any]:
            raise RuntimeError("llm down")

        checker = NarrativeContradictionChecker(boom_llm)
        indicators = _run(checker.check("laid off", "evidence"))
        assert len(indicators) == 1
        assert indicators[0].payload["flag"] == NARRATIVE_ANALYSIS_TIMEOUT


# ===========================================================================
# Core analysis budget (Requirement 6.9) + Kafka publication
# ===========================================================================


class _FakeCRAClient:
    """Minimal CRA client exposing the two 14.2 query methods used by analysis."""

    def __init__(self, noc_category=None, spans=None):
        self._noc = noc_category
        self._spans = spans or []

    async def fetch_employer_noc_category(self, sin_hash, tax_year, employer_bn):
        return self._noc

    async def fetch_concurrent_employment(self, sin_hash, start, end):
        return self._spans


class _FakeProducer:
    def __init__(self):
        self.sent: list[tuple[str, bytes, bytes]] = []

    async def send_and_wait(self, topic, value, key=None):
        self.sent.append((topic, value, key))


class TestCoreAnalysisAndPublish:
    def test_core_analysis_produces_noc_and_concurrent_within_budget(self):
        cra = _FakeCRAClient(
            noc_category="21231",
            spans=[
                {
                    "employer_bn": "BN_SECOND",
                    "start_date": "2024-01-10",
                    "end_date": "2024-01-20",
                }
            ],
        )
        result = _run(
            run_core_analysis(
                cra,
                sin_hash="sin-1",
                tax_year=2024,
                employer_bn="BN_OWN",
                declared_noc="72630",
                claim_start=date(2024, 1, 1),
                claim_end=date(2024, 1, 31),
                claim_id="claim-1",
            )
        )
        severities = sorted(i.severity.value for i in result.indicators)
        assert severities == [Severity.CRITICAL.value, Severity.HIGH.value]
        assert result.within_budget is True
        assert result.elapsed_ms <= 500

    def test_publish_sends_to_topic_keyed_by_claim(self):
        producer = _FakeProducer()
        publisher = IndicatorPublisher(producer)
        indicators = check_noc_mismatch("72630", "21231", claim_id="claim-1")
        result = _run(publisher.publish("claim-1", indicators))
        assert result.published_count == 1
        assert result.topic == "indicators.history-inconsistency"
        topic, _value, key = producer.sent[0]
        assert topic == "indicators.history-inconsistency"
        assert key == b"claim-1"

    def test_publish_no_producer_is_noop(self):
        publisher = IndicatorPublisher(None)
        result = _run(publisher.publish("claim-1", []))
        assert result.published_count == 0
        assert result.within_budget is True

    def test_narrative_skipped_without_evidence(self):
        async def fake_llm(n, e):
            return {"contradicted": True}

        checker = NarrativeContradictionChecker(fake_llm)
        # No CRA evidence -> LLM is not consulted, no Indicator.
        assert _run(
            run_narrative_check(
                checker, narrative="laid off", cra_evidence=None, claim_id="c1"
            )
        ) == []


# ===========================================================================
# Client query methods (NOC category + concurrent spans)
# ===========================================================================


class TestClientQueries:
    def _client(self, transport):
        return CRAAdapterClient(
            "http://cra-adapter:8011", timeout_seconds=5.0, transport=transport
        )

    def test_fetch_noc_category_from_t4(self):
        async def t(url, timeout):
            return {"status": "OK", "record": {
                "employment_income": "50000", "noc_category": "72101"}}

        cat = _run(self._client(t).fetch_employer_noc_category("s", 2024, "BN1"))
        assert cat == "72101"

    def test_fetch_concurrent_spans(self):
        async def t(url, timeout):
            return {
                "status": "OK",
                "employment_spans": [
                    {"employer_bn": "BN2", "start_date": "2024-01-10",
                     "end_date": "2024-01-20"},
                    {"employer_bn": "BN3", "start_date": "2024-02-01"},
                ],
            }

        spans = _run(
            self._client(t).fetch_concurrent_employment("s", "2024-01-01", "2024-01-31")
        )
        assert len(spans) == 2
        assert spans[0]["employer_bn"] == "BN2"
        assert spans[1]["end_date"] is None

    def test_fetch_concurrent_unavailable_returns_empty(self):
        async def t(url, timeout):
            raise RuntimeError("boom")

        spans = _run(
            self._client(t).fetch_concurrent_employment("s", "2024-01-01", "2024-01-31")
        )
        assert spans == []


# ===========================================================================
# Endpoint integration (6.5, 6.6, 6.8, 6.9)
# ===========================================================================


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not importable")
class TestAnalyzeEndpoint14_2:
    def _ok_transport(self, record, spans):
        async def t(url, timeout):
            if "/cra/employment/" in url:
                return {"status": "OK", "employment_spans": spans}
            return {"status": "OK", "record": record}

        return t

    def _make_client(self, transport):
        return CRAAdapterClient(
            "http://cra-adapter:8011", timeout_seconds=5.0, transport=transport
        )

    def test_full_analysis_produces_all_indicators_and_publishes(self):
        record = {"employment_income": "5000.00", "noc_category": "21231"}
        spans = [
            {"employer_bn": "BN_SECOND", "start_date": "2024-01-10",
             "end_date": "2024-01-20"}
        ]
        producer = _FakeProducer()

        async def fake_llm(narrative, evidence):
            return {"contradicted": True, "evidence": "payroll continued"}

        _main.app.state.settings = _main.get_settings()
        _main.app.state.cra_client = self._make_client(
            self._ok_transport(record, spans)
        )
        _main.app.state.narrative_checker = NarrativeContradictionChecker(fake_llm)
        _main.app.state.indicator_publisher = IndicatorPublisher(producer)

        client = TestClient(_main.app)
        resp = client.post(
            "/engine/analyze",
            json={
                "sin_hash": "sin-abc",
                "tax_year": 2024,
                "employer_bn": "BN_OWN",
                "roe_earnings": "5000.00",  # equal to T4 -> no discrepancy
                "claim_id": "11111111-1111-1111-1111-111111111111",
                "declared_noc": "72630",
                "claim_start_date": "2024-01-01",
                "claim_end_date": "2024-01-31",
                "separation_narrative": "I was laid off",
                "cra_continued_employment_evidence": "T4 shows continued income",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "OK"
        severities = sorted(i["severity"] for i in body["indicators"])
        # HIGH (NOC) + CRITICAL (concurrent) + CRITICAL (LLM) = 3 indicators
        assert severities == ["CRITICAL", "CRITICAL", "HIGH"]
        assert body["within_budget"] is True
        assert body["published_count"] == 3
        assert len(producer.sent) == 3

    def test_no_claim_period_still_runs_noc_only(self):
        record = {"employment_income": "5000.00", "noc_category": "21231"}
        _main.app.state.settings = _main.get_settings()
        _main.app.state.cra_client = self._make_client(
            self._ok_transport(record, [])
        )
        _main.app.state.narrative_checker = None
        _main.app.state.indicator_publisher = IndicatorPublisher(None)

        client = TestClient(_main.app)
        resp = client.post(
            "/engine/analyze",
            json={
                "sin_hash": "sin-abc",
                "tax_year": 2024,
                "employer_bn": "BN_OWN",
                "roe_earnings": "5000.00",
                "declared_noc": "72630",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["indicators"]) == 1
        assert body["indicators"][0]["severity"] == "HIGH"
