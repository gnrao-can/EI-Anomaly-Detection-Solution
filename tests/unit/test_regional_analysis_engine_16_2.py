"""
tests/unit/test_regional_analysis_engine_16_2.py
================================================
Unit + property tests for the Regional Analysis Engine, task 16.2:
seasonality annotation, recurring-claim detection, the Prophet forecast
boundary, and the 500 ms budget / Kafka publish path.

Coverage:
  * Seasonal peak boundary: index exactly ``avg * 1.10`` => NOT seasonal;
    ``avg * 1.10 + epsilon`` => seasonal; annotation merged into EVERY produced
    Indicator's payload (Requirement 8.7).
  * Recurring seasonal claim cycle: same calendar quarter in >= 2 of the 3
    prior years => MEDIUM Indicator naming the matching years and quarter; 1 of
    3 => no Indicator (Requirement 8.8).
  * Prophet forecast boundary: with Prophet unavailable the fallback estimator
    returns a 95% interval and never triggers an Indicator; a constant series is
    contained in its interval (Requirement 8.11 groundwork; P14 is task 16.4).
  * Endpoint: all produced Indicators are published to the fake Kafka producer;
    the response carries elapsed_ms/budget metadata; a slow analysis is flagged
    ``BUDGET_EXCEEDED`` (Requirement 8.9).

All seasonal-pattern DB and claim-history queries are served by in-memory fakes
(no live database), and the Kafka producer is the in-memory
``FakeIndicatorEventProducer`` (no live broker).

Isolation: the service directory is hyphenated and its modules import each other
by BARE name, so we load them via the shared ``load_service_module`` helper in
``tests/conftest.py``, which namespaces ``sys.modules`` keys and restores
bare-name modules afterwards so the full unit suite stays collision-free.

Requirements: 8.7, 8.8, 8.9, 8.11
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.conftest import load_service_module

# ---------------------------------------------------------------------------
# Load the hyphenated service modules under collision-free namespaced keys.
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

_seasonality = load_service_module(
    "regional-analysis-engine", "seasonality", siblings=_SIBLINGS
)
_forecast = load_service_module(
    "regional-analysis-engine", "forecast", siblings=_SIBLINGS
)
_providers = load_service_module(
    "regional-analysis-engine", "providers", siblings=_SIBLINGS
)
_kafka = load_service_module(
    "regional-analysis-engine", "kafka_producer", siblings=_SIBLINGS
)
_main = load_service_module("regional-analysis-engine", "main", siblings=_SIBLINGS)

# Task 29.1: protected routes require the INVESTIGATOR role; inject a principal.
from shared.auth import get_current_user as _get_current_user  # noqa: E402

_main.app.dependency_overrides[_get_current_user] = lambda: {"roles": ["INVESTIGATOR"]}

is_seasonal_peak = _seasonality.is_seasonal_peak
seasonal_annotation = _seasonality.seasonal_annotation
annotate_indicators = _seasonality.annotate_indicators
check_recurring_claims = _seasonality.check_recurring_claims
matching_recurring_quarters = _seasonality.matching_recurring_quarters
quarter_of = _seasonality.quarter_of

fit_forecast = _forecast.fit_forecast

InMemorySeasonalPatternProvider = _providers.InMemorySeasonalPatternProvider
InMemoryClaimHistoryProvider = _providers.InMemoryClaimHistoryProvider

FakeIndicatorEventProducer = _kafka.FakeIndicatorEventProducer
INDICATORS_TOPIC = _kafka.INDICATORS_TOPIC

try:
    from fastapi.testclient import TestClient

    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001
    _HAS_FASTAPI = False


def _run(coro):
    return asyncio.run(coro)


# ===========================================================================
# Seasonality annotation (Requirement 8.7)
# ===========================================================================


class TestSeasonality:
    def test_exactly_at_threshold_not_seasonal(self):
        # 100 * 1.10 == 110 exactly; index 110 is NOT strictly greater => no peak.
        assert is_seasonal_peak(Decimal("110"), Decimal("100")) is False
        assert seasonal_annotation(Decimal("110"), Decimal("100")) is None

    def test_just_above_threshold_is_seasonal(self):
        # 110.01 is strictly greater than 110 => seasonal peak.
        assert is_seasonal_peak(Decimal("110.01"), Decimal("100")) is True
        ann = seasonal_annotation(Decimal("110.01"), Decimal("100"))
        assert ann is not None
        assert ann["seasonal"] is True
        assert ann["seasonal_threshold"] == "110.00"

    def test_below_threshold_not_seasonal(self):
        assert is_seasonal_peak(Decimal("105"), Decimal("100")) is False

    def test_non_positive_rolling_avg_not_seasonal(self):
        assert is_seasonal_peak(Decimal("50"), Decimal("0")) is False

    def test_annotation_merged_into_every_indicator(self):
        # Two recurring-claim Indicators annotated seasonally get the flag each.
        indicators = check_recurring_claims(
            date(2024, 3, 15),
            [date(2021, 2, 1), date(2022, 3, 1), date(2023, 1, 1)],
        )
        assert len(indicators) == 1  # single recurring Indicator
        ann = seasonal_annotation(Decimal("120"), Decimal("100"))
        annotate_indicators(indicators, ann)
        assert all(i.payload.get("seasonal") is True for i in indicators)

    def test_none_annotation_leaves_indicators_untouched(self):
        indicators = check_recurring_claims(
            date(2024, 3, 15),
            [date(2021, 2, 1), date(2022, 3, 1), date(2023, 1, 1)],
        )
        annotate_indicators(indicators, None)
        assert all("seasonal" not in i.payload for i in indicators)


# ===========================================================================
# Recurring seasonal claim detection (Requirement 8.8)
# ===========================================================================


class TestRecurringClaims:
    def test_two_of_three_prior_years_flags_medium(self):
        # Reference 2024 Q1 (March). Prior claims in 2023 Q1 and 2022 Q1 => 2/3.
        indicators = check_recurring_claims(
            date(2024, 3, 10),
            [date(2023, 1, 20), date(2022, 2, 5), date(2020, 3, 1)],
        )
        assert len(indicators) == 1
        ind = indicators[0]
        assert ind.severity.value == "MEDIUM"
        assert ind.algorithm == "recurring_seasonal_claim"
        assert ind.payload["quarter"] == 1
        assert ind.payload["matching_years"] == [2022, 2023]

    def test_one_of_three_prior_years_no_flag(self):
        indicators = check_recurring_claims(
            date(2024, 3, 10),
            [date(2023, 1, 20), date(2022, 8, 5), date(2021, 11, 1)],
        )
        assert indicators == []

    def test_three_of_three_prior_years_flags(self):
        indicators = check_recurring_claims(
            date(2024, 6, 10),  # Q2
            [date(2023, 4, 1), date(2022, 5, 1), date(2021, 6, 1)],
        )
        assert len(indicators) == 1
        assert indicators[0].payload["matching_years"] == [2021, 2022, 2023]

    def test_reference_year_and_out_of_window_excluded(self):
        # Claims in the reference year (2024) and 4 years prior (2020) do not
        # count toward the 3-prior-year window.
        years = matching_recurring_quarters(
            date(2024, 2, 1),
            [date(2024, 1, 1), date(2020, 3, 1), date(2023, 2, 1)],
        )
        assert years == [2023]

    def test_quarter_of_boundaries(self):
        assert quarter_of(1) == 1 and quarter_of(3) == 1
        assert quarter_of(4) == 2 and quarter_of(6) == 2
        assert quarter_of(7) == 3 and quarter_of(9) == 3
        assert quarter_of(10) == 4 and quarter_of(12) == 4


# ===========================================================================
# Prophet forecast boundary (Requirement 8.11 groundwork)
# ===========================================================================


class TestForecast:
    def test_fallback_used_when_prophet_unavailable(self):
        result = fit_forecast([5.0, 5.2, 5.1, 5.3, 5.0, 5.4], force_fallback=True)
        assert result.model == "fallback"
        assert result.yhat_lower <= result.yhat <= result.yhat_upper
        assert result.interval_width == 0.95

    def test_constant_series_contained_in_interval(self):
        result = fit_forecast([5.0] * 12, force_fallback=True)
        assert result.contains(5.0)

    def test_too_few_observations_raises(self):
        with pytest.raises(ValueError):
            fit_forecast([5.0])

    def test_forecast_is_monitoring_only_not_an_indicator(self):
        # fit_forecast returns a ForecastResult, never an Indicator.
        result = fit_forecast([4.0, 4.5, 5.0, 4.8], force_fallback=True)
        assert not hasattr(result, "severity")
        assert result.to_dict()["model"] == "fallback"

    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    @given(
        values=st.lists(
            st.floats(min_value=0.5, max_value=25.0, allow_nan=False, allow_infinity=False),
            min_size=12,
            max_size=36,
        )
    )
    def test_forecast_interval_is_well_ordered(self, values):
        # For any history, the fallback interval is well-ordered and the point
        # forecast lies within it. (P14 calibration proper is task 16.4.)
        result = fit_forecast(values, force_fallback=True)
        assert result.yhat_lower <= result.yhat <= result.yhat_upper


# ===========================================================================
# Providers (mocked seasonal DB + claim history)
# ===========================================================================


class TestProviders:
    def test_seasonal_provider_hit_and_miss(self):
        provider = InMemorySeasonalPatternProvider()
        provider.set("SEC1", "2024-05", Decimal("130"), Decimal("100"))
        hit = _run(provider.get_sector_index("SEC1", "2024-05"))
        assert hit is not None and hit.sector_index == Decimal("130")
        miss = _run(provider.get_sector_index("SEC2", "2024-05"))
        assert miss is None

    def test_claim_history_provider(self):
        provider = InMemoryClaimHistoryProvider()
        provider.set("hash-abc", [date(2023, 1, 1), date(2022, 1, 1)])
        dates = _run(provider.get_prior_claim_dates("hash-abc"))
        assert dates == [date(2023, 1, 1), date(2022, 1, 1)]
        assert _run(provider.get_prior_claim_dates("unknown")) == []


# ===========================================================================
# Endpoint: publish + budget (Requirement 8.9)
# ===========================================================================


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not importable")
class TestAnalyzeEndpoint16_2:
    class _Settings:
        service_name = "regional-analysis-engine"
        statscan_adapter_url = "http://unused"
        job_bank_adapter_url = "http://unused"
        adapter_timeout_seconds = 5.0
        rate_deviation_threshold_pp = Decimal("-1.5")
        max_active_postings = 200
        wage_ratio_threshold = Decimal("1.15")
        seasonal_multiplier = Decimal("1.10")
        engine_budget_ms = 500
        indicators_topic = INDICATORS_TOPIC
        kafka_bootstrap_servers = "unused"

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

    def _client_with(
        self,
        *,
        statscan,
        job_bank,
        seasonal=None,
        history=None,
        producer=None,
        budget_ms=500,
    ):
        settings = self._Settings()
        settings.engine_budget_ms = budget_ms
        _main.app.state.settings = settings
        _main.app.state.statscan_client = statscan
        _main.app.state.job_bank_client = job_bank
        _main.app.state.seasonal_provider = (
            seasonal or InMemorySeasonalPatternProvider()
        )
        _main.app.state.claim_history_provider = (
            history or InMemoryClaimHistoryProvider()
        )
        _main.app.state.indicator_producer = producer or FakeIndicatorEventProducer()
        return TestClient(_main.app)

    def _payload(self, **overrides):
        base = {
            "economic_region_code": "35",
            "noc_code": "21234",
            "reference_month": "2024-05",
            "insurable_hourly_earnings": "20.00",
        }
        base.update(overrides)
        return base

    def test_indicators_published_to_kafka(self):
        client_mod = load_service_module(
            "regional-analysis-engine", "client", siblings=_SIBLINGS
        )
        stat = self._StubStatsCanada(
            rates=client_mod.RegionalRates(Decimal("4.9999"), Decimal("6.5"))
        )
        job = self._StubJobBank(
            postings=client_mod.JobPostings(201, Decimal("30.00"))
        )
        producer = FakeIndicatorEventProducer()
        resp = self._client_with(
            statscan=stat, job_bank=job, producer=producer
        ).post("/engine/analyze", json=self._payload())
        assert resp.status_code == 200
        body = resp.json()
        # Every produced Indicator was published to the topic.
        assert body["published_count"] == body["count"]
        assert len(producer.published) == body["count"]
        assert all(
            e["topic"] == INDICATORS_TOPIC for e in producer.published
        )
        assert body["publish_error"] is None

    def test_seasonal_annotation_applied_to_all_indicators(self):
        client_mod = load_service_module(
            "regional-analysis-engine", "client", siblings=_SIBLINGS
        )
        stat = self._StubStatsCanada(
            rates=client_mod.RegionalRates(Decimal("4.9999"), Decimal("6.5"))
        )
        job = self._StubJobBank(
            postings=client_mod.JobPostings(201, Decimal("30.00"))
        )
        seasonal = InMemorySeasonalPatternProvider()
        seasonal.set("SEC1", "2024-05", Decimal("130"), Decimal("100"))
        resp = self._client_with(
            statscan=stat, job_bank=job, seasonal=seasonal
        ).post("/engine/analyze", json=self._payload(sector_code="SEC1"))
        body = resp.json()
        assert body["seasonal"] is True
        assert body["count"] >= 1
        assert all(i["payload"].get("seasonal") is True for i in body["indicators"])

    def test_recurring_claim_indicator_via_history_provider(self):
        client_mod = load_service_module(
            "regional-analysis-engine", "client", siblings=_SIBLINGS
        )
        stat = self._StubStatsCanada(
            rates=client_mod.RegionalRates(Decimal("6.0"), Decimal("6.0"))  # no dev flag
        )
        job = self._StubJobBank(postings=client_mod.JobPostings(10, Decimal("20.00")))
        history = InMemoryClaimHistoryProvider()
        history.set("hash-xyz", [date(2023, 5, 1), date(2022, 4, 1)])  # 2/3 in Q2
        resp = self._client_with(
            statscan=stat, job_bank=job, history=history
        ).post(
            "/engine/analyze",
            json=self._payload(reference_month="2024-05", claim_key="hash-xyz"),
        )
        body = resp.json()
        algos = {i["algorithm"] for i in body["indicators"]}
        assert "recurring_seasonal_claim" in algos

    def test_forecast_recorded_but_not_an_indicator(self):
        client_mod = load_service_module(
            "regional-analysis-engine", "client", siblings=_SIBLINGS
        )
        stat = self._StubStatsCanada(
            rates=client_mod.RegionalRates(Decimal("6.0"), Decimal("6.0"))
        )
        job = self._StubJobBank(postings=client_mod.JobPostings(10, Decimal("20.00")))
        resp = self._client_with(statscan=stat, job_bank=job).post(
            "/engine/analyze",
            json=self._payload(
                regional_unemployment_history=[5.0, 5.2, 5.1, 5.3, 5.0, 5.4]
            ),
        )
        body = resp.json()
        assert body["forecast"] is not None
        assert body["forecast"]["model"] in {"prophet", "fallback"}
        # The forecast never produces an Indicator with the forecast algorithm.
        assert all(
            i["algorithm"] != "regional_forecast" for i in body["indicators"]
        )

    def test_budget_exceeded_flag_when_over_budget(self):
        client_mod = load_service_module(
            "regional-analysis-engine", "client", siblings=_SIBLINGS
        )
        stat = self._StubStatsCanada(
            rates=client_mod.RegionalRates(Decimal("6.0"), Decimal("6.0"))
        )
        job = self._StubJobBank(postings=client_mod.JobPostings(10, Decimal("20.00")))
        # A zero-millisecond budget forces the BUDGET_EXCEEDED flag.
        resp = self._client_with(
            statscan=stat, job_bank=job, budget_ms=0
        ).post("/engine/analyze", json=self._payload())
        body = resp.json()
        assert body["budget_exceeded"] is True
        assert body["flag"] == "BUDGET_EXCEEDED"
        assert body["elapsed_ms"] >= 0.0
