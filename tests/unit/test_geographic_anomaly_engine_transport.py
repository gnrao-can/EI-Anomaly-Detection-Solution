"""
tests/unit/test_geographic_anomaly_engine_transport.py
=======================================================
Unit and property tests for the Geographic Anomaly Engine's distance-dependent
analysis (task 12.2): transport implausibility (MVR + transit), economic
implausibility, CBSA cross-border absence, prior-employer pattern, the 600 ms
wall-clock budget, and Kafka publication.

All external adapters (MVR, CBSA, GeoNames) are replaced with injectable in-memory
fakes; Kafka is replaced with a ``FakeIndicatorProducer``. No live network.

Service modules live in the hyphenated directory
``services/geographic-anomaly-engine`` and import each other by bare name, so we
load them through the shared ``load_service_module`` conftest helper, which
namespaces ``sys.modules`` keys and therefore keeps the full unit suite
collision-free.

Requirements: 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from shared.models import EngineType, Severity
from tests.conftest import load_service_module

# ---------------------------------------------------------------------------
# Load the hyphenated geographic-anomaly-engine service modules (collision-free).
# ---------------------------------------------------------------------------

_SIBLINGS = [
    "config",
    "geo",
    "client",
    "transport",
    "mvr_client",
    "cbsa_client",
    "kafka_producer",
    "indicators",
    "main",
]

_transport = load_service_module(
    "geographic-anomaly-engine", "transport", siblings=_SIBLINGS
)
_mvr = load_service_module("geographic-anomaly-engine", "mvr_client", siblings=_SIBLINGS)
_cbsa = load_service_module(
    "geographic-anomaly-engine", "cbsa_client", siblings=_SIBLINGS
)
_client = load_service_module("geographic-anomaly-engine", "client", siblings=_SIBLINGS)
_producer_mod = load_service_module(
    "geographic-anomaly-engine", "kafka_producer", siblings=_SIBLINGS
)
_main = load_service_module("geographic-anomaly-engine", "main", siblings=_SIBLINGS)

# Task 29.1: protected routes require the INVESTIGATOR role; inject a principal.
from shared.auth import get_current_user as _get_current_user  # noqa: E402

_main.app.dependency_overrides[_get_current_user] = lambda: {"roles": ["INVESTIGATOR"]}

FakeGeoNamesClient = _client.FakeGeoNamesClient
FakeMVRClient = _mvr.FakeMVRClient
FakeCBSAClient = _cbsa.FakeCBSAClient
FakeIndicatorProducer = _producer_mod.FakeIndicatorProducer

weekly_transport_cost_exceeds_benefit = _transport.weekly_transport_cost_exceeds_benefit
prior_employers_over_threshold = _transport.prior_employers_over_threshold
PriorEmployer = _transport.PriorEmployer
exceeds_distance_threshold = _transport.exceeds_distance_threshold

try:
    from fastapi.testclient import TestClient

    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001
    _HAS_FASTAPI = False


# ===========================================================================
# Pure logic: exceeds_distance_threshold (Req 4.3 boundary)
# ===========================================================================


class TestDistanceThreshold:
    def test_exactly_100km_does_not_trigger(self):
        assert exceeds_distance_threshold(100.0) is False

    def test_just_over_100km_triggers(self):
        assert exceeds_distance_threshold(100.001) is True

    def test_well_under_does_not_trigger(self):
        assert exceeds_distance_threshold(42.0) is False


# ===========================================================================
# Pure logic: weekly transport cost vs weekly EI benefit (Req 4.5 / 4.6)
# ===========================================================================


class TestTransportCost:
    def test_cost_exceeds_benefit_is_critical_trigger(self):
        result = weekly_transport_cost_exceeds_benefit(
            {"flight": 500.00, "bus": 300.00}, 250.00
        )
        assert result is not None
        assert result.exceeds is True
        # Lowest-cost mode is the bus.
        assert result.lowest_cost_mode == "bus"
        assert result.weekly_transport_cost == Decimal("300.00")
        assert result.weekly_benefit_rate == Decimal("250.00")
        # 300 / 250 = 1.2000
        assert result.ratio == Decimal("1.2000")
        payload = result.payload()
        assert payload["weekly_transport_cost_cad"] == "300.00"
        assert payload["weekly_benefit_rate_cad"] == "250.00"
        assert payload["cost_to_benefit_ratio"] == "1.2000"

    def test_cost_equal_to_benefit_does_not_trigger(self):
        # Strictly greater is required; equal does not trigger.
        result = weekly_transport_cost_exceeds_benefit({"bus": 250.00}, 250.00)
        assert result is not None
        assert result.exceeds is False

    def test_cost_below_benefit_does_not_trigger(self):
        result = weekly_transport_cost_exceeds_benefit({"bus": 100.00}, 250.00)
        assert result is not None
        assert result.exceeds is False

    def test_all_sources_unavailable_returns_none(self):
        # Empty and all-None both mean data unavailable from all sources.
        assert weekly_transport_cost_exceeds_benefit({}, 250.00) is None
        assert (
            weekly_transport_cost_exceeds_benefit(
                {"flight": None, "bus": None}, 250.00
            )
            is None
        )

    def test_exact_decimal_arithmetic_no_float_drift(self):
        # 0.1 + 0.2 style inputs must be exact via Decimal string path.
        result = weekly_transport_cost_exceeds_benefit({"bus": "0.30"}, "0.30")
        assert result is not None
        assert result.exceeds is False
        assert result.weekly_transport_cost == Decimal("0.30")

    @settings(max_examples=200)
    @given(
        cost=st.decimals(
            min_value=Decimal("0.01"),
            max_value=Decimal("100000"),
            places=2,
            allow_nan=False,
            allow_infinity=False,
        ),
        benefit=st.decimals(
            min_value=Decimal("0.01"),
            max_value=Decimal("100000"),
            places=2,
            allow_nan=False,
            allow_infinity=False,
        ),
    )
    def test_property_exceeds_iff_strictly_greater(self, cost, benefit):
        """``exceeds`` is True iff the lowest weekly cost > weekly benefit."""
        result = weekly_transport_cost_exceeds_benefit({"bus": cost}, benefit)
        assert result is not None
        assert result.exceeds == (Decimal(str(cost)) > Decimal(str(benefit)))


# ===========================================================================
# Pure logic: prior-employer pattern (Req 4.7 / 4.8)
# ===========================================================================


class TestPriorEmployers:
    def test_fewer_than_two_omits_analysis(self):
        pattern = prior_employers_over_threshold(
            [PriorEmployer("Acme", 150.0)]
        )
        assert pattern.evaluated is False
        assert pattern.both_over_threshold is False
        assert pattern.employers == ()

    def test_both_over_threshold(self):
        pattern = prior_employers_over_threshold(
            [
                PriorEmployer("Acme", 150.0),
                PriorEmployer("Globex", 200.0),
                PriorEmployer("Initech", 5.0),  # older; ignored
            ]
        )
        assert pattern.evaluated is True
        assert pattern.both_over_threshold is True
        assert len(pattern.employers) == 2
        names = {e.employer_name for e in pattern.employers}
        assert names == {"Acme", "Globex"}

    def test_one_under_threshold_not_flagged(self):
        pattern = prior_employers_over_threshold(
            [PriorEmployer("Acme", 150.0), PriorEmployer("Globex", 50.0)]
        )
        assert pattern.evaluated is True
        assert pattern.both_over_threshold is False


# ===========================================================================
# Endpoint: distance-dependent analysis (Req 4.3 – 4.9)
# ===========================================================================


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not importable")
class TestAnalyzeDistanceDependent:
    """End-to-end analysis with injected fakes for GeoNames/MVR/CBSA/Kafka."""

    OTTAWA = (45.4215, -75.6972)
    TORONTO = (43.6532, -79.3832)  # ~350 km from Ottawa

    def _client(
        self,
        *,
        geonames=None,
        mvr=None,
        cbsa=None,
        producer=None,
        budget_ms=600,
    ) -> "TestClient":
        settings = _main.get_settings()
        # Override the budget for deterministic tests without touching env.
        try:
            object.__setattr__(settings, "analysis_budget_ms", budget_ms)
        except Exception:  # noqa: BLE001 - pydantic settings may allow attr set
            settings.analysis_budget_ms = budget_ms
        _main.app.state.settings = settings
        _main.app.state.geonames = geonames if geonames is not None else FakeGeoNamesClient()
        _main.app.state.mvr = mvr
        _main.app.state.cbsa = cbsa
        _main.app.state.producer = producer
        return TestClient(_main.app)

    def _geonames_long(self) -> "FakeGeoNamesClient":
        fake = FakeGeoNamesClient()
        fake.add("HOME", *self.OTTAWA)
        fake.add("WORK", *self.TORONTO)
        return fake

    def _base_request(self, **overrides) -> dict:
        req = {
            "claim_id": "11111111-1111-1111-1111-111111111111",
            "residential_postal_code": "HOME",
            "employer_work_location": "WORK",
            "sin_hash": "sinhash-abc",
            "residential_province": "ON",
            # Provide transport data so the economic check does not fire by default.
            "transport_mode_costs": {"bus": 50.00},
            "weekly_benefit_rate": 500.00,
        }
        req.update(overrides)
        return req

    def _severities(self, body) -> list[str]:
        return [i["severity"] for i in body["indicators"]]

    def _by_algorithm(self, body, algorithm) -> list[dict]:
        return [i for i in body["indicators"] if i["algorithm"] == algorithm]

    # ── MVR (Req 4.3) ──

    def test_no_vehicle_yields_high_indicator(self):
        mvr = FakeMVRClient()
        mvr.set_none("sinhash-abc")
        client = self._client(geonames=self._geonames_long(), mvr=mvr)
        resp = client.post("/engine/analyze", json=self._base_request())
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "OK"
        assert body["distance_km"] > 100.0
        matches = self._by_algorithm(body, "transport_implausibility_vehicle")
        assert len(matches) == 1
        assert matches[0]["severity"] == Severity.HIGH.value
        assert matches[0]["engine"] == EngineType.GEOGRAPHIC_ANOMALY.value

    def test_owned_vehicle_yields_no_vehicle_indicator(self):
        mvr = FakeMVRClient()
        mvr.set_owned("sinhash-abc")
        client = self._client(geonames=self._geonames_long(), mvr=mvr)
        resp = client.post("/engine/analyze", json=self._base_request())
        body = resp.json()
        assert self._by_algorithm(body, "transport_implausibility_vehicle") == []

    def test_mvr_unavailable_treated_as_unknown_no_indicator(self):
        """Adapter UNAVAILABLE => ownership UNKNOWN, no HIGH Indicator (Req 15.2)."""
        mvr = FakeMVRClient()
        mvr.set_unavailable("sinhash-abc")
        client = self._client(geonames=self._geonames_long(), mvr=mvr)
        resp = client.post("/engine/analyze", json=self._base_request())
        body = resp.json()
        assert self._by_algorithm(body, "transport_implausibility_vehicle") == []

    def test_short_distance_skips_mvr_check(self):
        """Distance <= 100 km must not query MVR nor raise the vehicle Indicator."""
        fake = FakeGeoNamesClient()
        fake.add("HOME", *self.OTTAWA)
        fake.add("WORK", 45.4180, -75.6990)  # ~ hundreds of metres
        mvr = FakeMVRClient()
        mvr.set_none("sinhash-abc")
        client = self._client(geonames=fake, mvr=mvr)
        resp = client.post("/engine/analyze", json=self._base_request())
        body = resp.json()
        assert body["distance_km"] < 100.0
        assert self._by_algorithm(body, "transport_implausibility_vehicle") == []
        # MVR must not even have been queried.
        assert mvr.calls == []

    # ── Transit (Req 4.4) ──

    def test_no_transit_route_yields_high_indicator(self):
        client = self._client(geonames=self._geonames_long())
        resp = client.post(
            "/engine/analyze",
            json=self._base_request(transit_route_exists=False),
        )
        body = resp.json()
        matches = self._by_algorithm(body, "transport_implausibility_transit")
        assert len(matches) == 1
        assert matches[0]["severity"] == Severity.HIGH.value

    def test_transit_route_exists_no_indicator(self):
        client = self._client(geonames=self._geonames_long())
        resp = client.post(
            "/engine/analyze",
            json=self._base_request(transit_route_exists=True),
        )
        body = resp.json()
        assert self._by_algorithm(body, "transport_implausibility_transit") == []

    # ── Economic implausibility (Req 4.5 / 4.6) ──

    def test_cost_exceeds_benefit_yields_critical(self):
        client = self._client(geonames=self._geonames_long())
        resp = client.post(
            "/engine/analyze",
            json=self._base_request(
                transport_mode_costs={"flight": 600.00, "bus": 550.00},
                weekly_benefit_rate=500.00,
            ),
        )
        body = resp.json()
        matches = self._by_algorithm(body, "economic_implausibility")
        assert len(matches) == 1
        ind = matches[0]
        assert ind["severity"] == Severity.CRITICAL.value
        assert ind["payload"]["weekly_transport_cost_cad"] == "550.00"
        assert ind["payload"]["weekly_benefit_rate_cad"] == "500.00"
        assert ind["payload"]["cost_to_benefit_ratio"] == "1.1000"

    def test_transport_data_unavailable_yields_medium(self):
        client = self._client(geonames=self._geonames_long())
        resp = client.post(
            "/engine/analyze",
            json=self._base_request(transport_mode_costs={}),
        )
        body = resp.json()
        matches = self._by_algorithm(body, "economic_implausibility")
        assert len(matches) == 1
        assert matches[0]["severity"] == Severity.MEDIUM.value
        assert matches[0]["payload"]["reason"] == "TRANSPORT_COST_DATA_UNAVAILABLE"

    # ── Prior-employer pattern (Req 4.7 / 4.8) ──

    def test_prior_employers_merged_into_indicator(self):
        client = self._client(geonames=self._geonames_long())
        resp = client.post(
            "/engine/analyze",
            json=self._base_request(
                transit_route_exists=False,
                prior_employers=[
                    {
                        "employer_name": "Acme",
                        "distance_km": 150.0,
                        "employment_start": "2022-01-01",
                        "employment_end": "2022-12-31",
                    },
                    {
                        "employer_name": "Globex",
                        "distance_km": 220.0,
                        "employment_start": "2021-01-01",
                        "employment_end": "2021-12-31",
                    },
                ],
            ),
        )
        body = resp.json()
        transit = self._by_algorithm(body, "transport_implausibility_transit")
        assert len(transit) == 1
        payload = transit[0]["payload"]
        assert payload["prior_employers_both_over_100km"] is True
        names = {e["employer_name"] for e in payload["prior_employers"]}
        assert names == {"Acme", "Globex"}
        assert payload["prior_employers"][0]["employment_start"] == "2022-01-01"

    def test_fewer_than_two_prior_employers_omitted(self):
        client = self._client(geonames=self._geonames_long())
        resp = client.post(
            "/engine/analyze",
            json=self._base_request(
                transit_route_exists=False,
                prior_employers=[{"employer_name": "Acme", "distance_km": 150.0}],
            ),
        )
        body = resp.json()
        transit = self._by_algorithm(body, "transport_implausibility_transit")[0]
        assert "prior_employers" not in transit["payload"]

    # ── CBSA cross-border absence (Req 4.9) ──

    def test_ten_day_absence_yields_high_indicator(self):
        cbsa = FakeCBSAClient()
        cbsa.set_absence_days("sinhash-abc", 10)
        client = self._client(geonames=self._geonames_long(), cbsa=cbsa)
        resp = client.post("/engine/analyze", json=self._base_request())
        body = resp.json()
        matches = self._by_algorithm(body, "cbsa_absence_detection")
        assert len(matches) == 1
        assert matches[0]["severity"] == Severity.HIGH.value
        assert matches[0]["payload"]["max_absence_days"] == 10

    def test_nine_day_absence_no_indicator(self):
        cbsa = FakeCBSAClient()
        cbsa.set_absence_days("sinhash-abc", 9)
        client = self._client(geonames=self._geonames_long(), cbsa=cbsa)
        resp = client.post("/engine/analyze", json=self._base_request())
        body = resp.json()
        assert self._by_algorithm(body, "cbsa_absence_detection") == []

    def test_cbsa_unavailable_no_indicator(self):
        cbsa = FakeCBSAClient()
        cbsa.set_unavailable("sinhash-abc")
        client = self._client(geonames=self._geonames_long(), cbsa=cbsa)
        resp = client.post("/engine/analyze", json=self._base_request())
        body = resp.json()
        assert self._by_algorithm(body, "cbsa_absence_detection") == []

    # ── Kafka publication (Req 4.9) ──

    def test_indicators_published_to_kafka(self):
        mvr = FakeMVRClient()
        mvr.set_none("sinhash-abc")
        cbsa = FakeCBSAClient()
        cbsa.set_absence_days("sinhash-abc", 30)
        producer = FakeIndicatorProducer()
        client = self._client(
            geonames=self._geonames_long(), mvr=mvr, cbsa=cbsa, producer=producer
        )
        resp = client.post(
            "/engine/analyze",
            json=self._base_request(transit_route_exists=False),
        )
        body = resp.json()
        # vehicle + transit + cbsa indicators produced and all published.
        assert body["published_count"] == len(body["indicators"])
        assert len(producer.published) == len(body["indicators"])
        for event in producer.published:
            assert event["event_type"] == "indicator.geographic-anomaly"
            assert event["indicator"]["engine"] == EngineType.GEOGRAPHIC_ANOMALY.value

    def test_publish_failure_does_not_fail_response(self):
        producer = FakeIndicatorProducer()
        producer.fail_next_publish()
        mvr = FakeMVRClient()
        mvr.set_none("sinhash-abc")
        client = self._client(
            geonames=self._geonames_long(), mvr=mvr, producer=producer
        )
        resp = client.post("/engine/analyze", json=self._base_request())
        assert resp.status_code == 200
        body = resp.json()
        assert body["publish_error"] is not None
        assert body["published_count"] == 0
        # The Indicator is still returned to the caller.
        assert len(body["indicators"]) >= 1

    # ── Budget (Req 4.9) ──

    def test_completes_within_budget_reported(self):
        client = self._client(geonames=self._geonames_long())
        resp = client.post("/engine/analyze", json=self._base_request())
        body = resp.json()
        assert body["elapsed_ms"] >= 0.0
        assert body["budget_exceeded"] is False

    def test_zero_budget_skips_distance_dependent_checks(self):
        """A 0 ms budget means the deadline is already passed; heavy checks skip."""
        mvr = FakeMVRClient()
        mvr.set_none("sinhash-abc")
        cbsa = FakeCBSAClient()
        cbsa.set_absence_days("sinhash-abc", 30)
        client = self._client(
            geonames=self._geonames_long(), mvr=mvr, cbsa=cbsa, budget_ms=0
        )
        resp = client.post(
            "/engine/analyze",
            json=self._base_request(transit_route_exists=False),
        )
        body = resp.json()
        assert body["status"] == "OK"
        # Distance still computed, but budget-gated checks are skipped.
        assert body["distance_km"] > 100.0
        assert body["indicators"] == []
        assert mvr.calls == []
        assert cbsa.calls == []
