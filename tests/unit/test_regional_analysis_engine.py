"""
tests/unit/test_regional_analysis_engine.py
===========================================
Unit tests for the Regional Analysis Engine (task 16.1).

Coverage:
  * Rate-deviation boundary: exactly -1.5 pp => NO Indicator; -1.5001 pp =>
    MEDIUM Indicator (Requirements 8.1, 8.2).
  * Equal regional/national rates => deviation exactly ``Decimal("0")`` and no
    Indicator (P13 groundwork, task 16.3).
  * Active-postings boundary: exactly 200 => NO Indicator; 201 => HIGH
    Indicator (Requirements 8.3, 8.4).
  * Median-wage boundary: exactly +15% => NO Indicator; +15.01% => HIGH
    Indicator (Requirements 8.4, 8.6).
  * Stats Canada Adapter unavailable => the analyze response is annotated
    ``DATA_UNAVAILABLE`` and the rate-dependent analysis is skipped
    (Requirement 8.5).

Exact ``Decimal`` arithmetic is used throughout. All adapter access is via
dependency-injected fakes; there is no live network.

Isolation note: the service directory is hyphenated
(``services/regional-analysis-engine``) and its modules import each other by
BARE name (``from analysis import ...``, ``from client import ...``,
``from config import ...``). Loading them under the bare names would collide
with the ``config`` / ``client`` / ``main`` modules of the other engines and
adapters across the suite. To stay isolated we load each module under a UNIQUE
namespaced key (``regional_analysis``, ``regional_client``, ...) while
registering the bare name only transiently so intra-package imports resolve,
then restore the pre-existing bare-name modules. This mirrors
``tests/unit/test_job_bank_adapter.py``.

Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any

import pytest

from tests.conftest import load_service_module

# ---------------------------------------------------------------------------
# Load the hyphenated regional-analysis-engine service modules under
# collision-free namespaced keys. ``main.py`` now imports several siblings by
# bare name (config, analysis, client, forecast, seasonality, providers,
# kafka_producer), so all of them must be provided to the loader; otherwise a
# bare name like ``kafka_producer`` would resolve to a DIFFERENT service's
# module when the whole unit suite runs in one process.
# ---------------------------------------------------------------------------

_SIBLINGS = [
    "config",
    "analysis",
    "client",
    "forecast",
    "seasonality",
    "providers",
    "kafka_producer",
    "main",
]

_analysis = load_service_module(
    "regional-analysis-engine", "analysis", siblings=_SIBLINGS
)
_client = load_service_module(
    "regional-analysis-engine", "client", siblings=_SIBLINGS
)
_main = load_service_module("regional-analysis-engine", "main", siblings=_SIBLINGS)

# Task 29.1: protected routes require the INVESTIGATOR role; inject a principal.
from shared.auth import get_current_user as _get_current_user  # noqa: E402

_main.app.dependency_overrides[_get_current_user] = lambda: {"roles": ["INVESTIGATOR"]}

rate_deviation = _analysis.rate_deviation
check_rate_deviation = _analysis.check_rate_deviation
check_job_postings = _analysis.check_job_postings

StatsCanadaUnavailableError = _client.StatsCanadaUnavailableError
JobBankUnavailableError = _client.JobBankUnavailableError

try:
    from fastapi.testclient import TestClient

    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001
    _HAS_FASTAPI = False


def _run(coro):
    return asyncio.run(coro)


# ===========================================================================
# Rate deviation boundary (8.1, 8.2)
# ===========================================================================


class TestRateDeviation:
    def test_deviation_is_exact_decimal(self):
        dev = rate_deviation(Decimal("5.0"), Decimal("6.5"))
        assert dev == Decimal("-1.5")
        assert isinstance(dev, Decimal)

    def test_exactly_minus_1_5_no_flag(self):
        # regional 5.0, national 6.5 => deviation exactly -1.5 pp => NO flag.
        indicators = check_rate_deviation(Decimal("5.0"), Decimal("6.5"))
        assert indicators == []

    def test_minus_1_5001_flags_medium(self):
        # deviation -1.5001 pp (strictly below -1.5) => MEDIUM Indicator.
        indicators = check_rate_deviation(Decimal("4.9999"), Decimal("6.5"))
        assert len(indicators) == 1
        ind = indicators[0]
        assert ind.severity.value == "MEDIUM"
        assert ind.algorithm == "regional_rate_deviation"
        # Evidence carries both rates and the deviation.
        assert ind.payload["regional_rate"] == "4.9999"
        assert ind.payload["national_rate"] == "6.5"
        assert Decimal(ind.payload["deviation"]) == Decimal("-1.5001")

    def test_equal_rates_zero_deviation_no_flag(self):
        # P13 groundwork: equal rates => deviation exactly 0, no Indicator.
        for rate in (Decimal("0.0000"), Decimal("5.4321"), Decimal("12.9999")):
            assert rate_deviation(rate, rate) == Decimal("0")
            assert check_rate_deviation(rate, rate) == []

    def test_positive_deviation_no_flag(self):
        # Regional higher than national never flags (deviation positive).
        assert check_rate_deviation(Decimal("8.0"), Decimal("6.0")) == []


# ===========================================================================
# Job postings boundary (8.3, 8.4, 8.6)
# ===========================================================================


class TestJobPostings:
    def test_exactly_200_postings_no_flag(self):
        indicators = check_job_postings(
            active_count=200,
            median_wage=Decimal("20.00"),
            hourly_earnings=Decimal("20.00"),
        )
        # 200 is not strictly greater than 200 => no postings Indicator.
        assert not any(i.algorithm == "job_bank_active_postings" for i in indicators)

    def test_201_postings_flags_high(self):
        indicators = check_job_postings(
            active_count=201,
            median_wage=Decimal("20.00"),
            hourly_earnings=Decimal("20.00"),
        )
        postings = [i for i in indicators if i.algorithm == "job_bank_active_postings"]
        assert len(postings) == 1
        assert postings[0].severity.value == "HIGH"
        assert postings[0].payload["active_posting_count"] == 201

    def test_median_wage_exactly_15_percent_no_flag(self):
        # hourly 20.00 * 1.15 == 23.00 exactly; median 23.00 is NOT strictly
        # greater => no wage Indicator.
        indicators = check_job_postings(
            active_count=10,
            median_wage=Decimal("23.00"),
            hourly_earnings=Decimal("20.00"),
        )
        assert not any(i.algorithm == "job_bank_median_wage" for i in indicators)

    def test_median_wage_15_01_percent_flags_high(self):
        # 20.00 * 1.15 == 23.00; median 23.01 is strictly greater => HIGH.
        indicators = check_job_postings(
            active_count=10,
            median_wage=Decimal("23.01"),
            hourly_earnings=Decimal("20.00"),
        )
        wage = [i for i in indicators if i.algorithm == "job_bank_median_wage"]
        assert len(wage) == 1
        assert wage[0].severity.value == "HIGH"
        assert wage[0].payload["median_wage"] == "23.01"

    def test_both_thresholds_exceeded_produce_two_indicators(self):
        indicators = check_job_postings(
            active_count=500,
            median_wage=Decimal("30.00"),
            hourly_earnings=Decimal("20.00"),
        )
        algos = {i.algorithm for i in indicators}
        assert algos == {"job_bank_active_postings", "job_bank_median_wage"}

    def test_all_indicators_are_regional_engine(self):
        indicators = check_job_postings(
            active_count=201,
            median_wage=Decimal("30.00"),
            hourly_earnings=Decimal("20.00"),
        )
        assert indicators
        assert all(i.engine.value == "REGIONAL_ANALYSIS" for i in indicators)


# ===========================================================================
# Async client parsing / unavailability
# ===========================================================================


class _FakeResponse:
    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        return self._body


class _FakeClient:
    """Duck-typed async HTTP client returning a preset response or raising."""

    def __init__(self, response: Any = None, raise_exc: Exception | None = None):
        self._response = response
        self._raise = raise_exc

    async def get(self, url: str, *a, **k):
        if self._raise is not None:
            raise self._raise
        return self._response


class TestStatsCanadaClient:
    def test_parses_rates(self):
        body = {
            "flag": "OK",
            "records": [
                {"unemployment_rate": "5.4321", "national_unemployment_rate": "6.1024"}
            ],
        }
        client = _client.StatsCanadaClient(
            base_url="http://x", client=_FakeClient(_FakeResponse(200, body))
        )
        rates = _run(client.get_rates("35", "2024-05"))
        assert rates.regional_rate == Decimal("5.4321")
        assert rates.national_rate == Decimal("6.1024")

    def test_data_unavailable_body_raises(self):
        body = {"flag": "DATA_UNAVAILABLE", "records": []}
        client = _client.StatsCanadaClient(
            base_url="http://x", client=_FakeClient(_FakeResponse(503, body))
        )
        with pytest.raises(StatsCanadaUnavailableError):
            _run(client.get_rates("35", "2024-05"))

    def test_transport_error_raises(self):
        client = _client.StatsCanadaClient(
            base_url="http://x",
            client=_FakeClient(raise_exc=RuntimeError("connection reset")),
        )
        with pytest.raises(StatsCanadaUnavailableError):
            _run(client.get_rates("35", "2024-05"))


class TestJobBankClient:
    def test_parses_postings(self):
        body = {
            "flag": "OK",
            "active_posting_count": 187,
            "median_hourly_wage": "42.7550",
        }
        client = _client.JobBankClient(
            base_url="http://x", client=_FakeClient(_FakeResponse(200, body))
        )
        postings = _run(client.get_postings("21234", "35", "2024-05"))
        assert postings.active_posting_count == 187
        assert postings.median_hourly_wage == Decimal("42.7550")

    def test_5xx_raises_unavailable(self):
        client = _client.JobBankClient(
            base_url="http://x", client=_FakeClient(_FakeResponse(503, {}))
        )
        with pytest.raises(JobBankUnavailableError):
            _run(client.get_postings("21234", "35", "2024-05"))


# ===========================================================================
# Endpoint: DATA_UNAVAILABLE annotation when Stats Canada is down (8.5)
# ===========================================================================


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not importable")
class TestAnalyzeEndpoint:
    def _client_with(self, statscan_client, job_bank_client):
        class _Settings:
            service_name = "regional-analysis-engine"
            statscan_adapter_url = "http://unused"
            job_bank_adapter_url = "http://unused"
            adapter_timeout_seconds = 5.0
            rate_deviation_threshold_pp = Decimal("-1.5")
            max_active_postings = 200
            wage_ratio_threshold = Decimal("1.15")

        _main.app.state.settings = _Settings()
        _main.app.state.statscan_client = statscan_client
        _main.app.state.job_bank_client = job_bank_client
        return TestClient(_main.app)

    class _StubStatsCanada:
        def __init__(self, rates=None, raise_exc=None):
            self._rates = rates
            self._raise = raise_exc

        async def get_rates(self, region, ym):
            if self._raise is not None:
                raise self._raise
            return self._rates

    class _StubJobBank:
        def __init__(self, postings=None, raise_exc=None):
            self._postings = postings
            self._raise = raise_exc

        async def get_postings(self, noc, region, ym):
            if self._raise is not None:
                raise self._raise
            return self._postings

    def _payload(self, **overrides):
        base = {
            "economic_region_code": "35",
            "noc_code": "21234",
            "reference_month": "2024-05",
            "insurable_hourly_earnings": "20.00",
        }
        base.update(overrides)
        return base

    def test_statscan_unavailable_annotates_data_unavailable(self):
        statscan = self._StubStatsCanada(
            raise_exc=StatsCanadaUnavailableError("down")
        )
        job_bank = self._StubJobBank(
            postings=_client.JobPostings(10, Decimal("20.00"))
        )
        resp = self._client_with(statscan, job_bank).post(
            "/engine/analyze", json=self._payload()
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["flag"] == "DATA_UNAVAILABLE"
        assert body["data_unavailable"] is True
        # There is a DATA_UNAVAILABLE annotation Indicator for Stats Canada.
        annotations = [
            i for i in body["indicators"] if i["algorithm"] == "data_unavailable"
        ]
        assert len(annotations) == 1
        assert annotations[0]["payload"]["source"] == "Stats Canada"
        # No rate-deviation Indicator was produced (rate analysis skipped).
        assert not any(
            i["algorithm"] == "regional_rate_deviation" for i in body["indicators"]
        )

    def test_happy_path_produces_expected_indicators(self):
        statscan = self._StubStatsCanada(
            rates=_client.RegionalRates(Decimal("4.9999"), Decimal("6.5"))
        )
        job_bank = self._StubJobBank(
            postings=_client.JobPostings(201, Decimal("30.00"))
        )
        resp = self._client_with(statscan, job_bank).post(
            "/engine/analyze", json=self._payload()
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["flag"] == "OK"
        assert body["data_unavailable"] is False
        algos = {i["algorithm"] for i in body["indicators"]}
        assert "regional_rate_deviation" in algos
        assert "job_bank_active_postings" in algos
        assert "job_bank_median_wage" in algos

    def test_healthz_ok(self):
        client = self._client_with(
            self._StubStatsCanada(raise_exc=StatsCanadaUnavailableError("x")),
            self._StubJobBank(raise_exc=JobBankUnavailableError("x")),
        )
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
