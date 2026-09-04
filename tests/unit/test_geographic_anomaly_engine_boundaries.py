"""
tests/unit/test_geographic_anomaly_engine_boundaries.py
=======================================================
Focused boundary unit tests for the Geographic Anomaly Engine (task 12.4).

Where task 12.2's ``test_geographic_anomaly_engine_transport.py`` exercises the
distance-dependent analysis broadly, this file pins the exact boundary
conditions called out by task 12.4, at BOTH the pure-logic level (fast, no
FastAPI) and end to end through ``POST /engine/analyze`` (with every external
adapter replaced by an injected in-memory fake — no live network):

  * Distance threshold (Req 4.3):
      - exactly 100.0 km does NOT trigger distance-dependent analysis,
      - 100.001 km DOES trigger it.
  * CBSA cross-border absence (Req 4.9 / Req 13.2):
      - 9 consecutive days -> no HIGH Indicator,
      - 10 consecutive days -> HIGH Indicator (>= 10 is the inclusive trigger).
  * MVR degradation (Req 15.2 / 15.3):
      - an UNAVAILABLE adapter response -> ownership treated as UNKNOWN
        (not absent) -> NO HIGH "no vehicle" Indicator.

The service directory ``services/geographic-anomaly-engine`` is hyphenated and
its modules import each other by bare name, so we load them through the shared
``load_service_module`` conftest helper, which namespaces ``sys.modules`` keys
and keeps the full unit suite collision-free when run alongside other services.

Requirements: 4.3, 4.9, 15.2, 15.3
"""

from __future__ import annotations

import pytest

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
_main = load_service_module("geographic-anomaly-engine", "main", siblings=_SIBLINGS)

# Task 29.1: protected routes require the INVESTIGATOR role; inject a principal.
from shared.auth import get_current_user as _get_current_user  # noqa: E402

_main.app.dependency_overrides[_get_current_user] = lambda: {"roles": ["INVESTIGATOR"]}

exceeds_distance_threshold = _transport.exceeds_distance_threshold
DISTANCE_THRESHOLD_KM = _transport.DISTANCE_THRESHOLD_KM

classify_ownership = _mvr.classify_ownership
OWNERSHIP_OWNED = _mvr.OWNERSHIP_OWNED
OWNERSHIP_NONE = _mvr.OWNERSHIP_NONE
OWNERSHIP_UNKNOWN = _mvr.OWNERSHIP_UNKNOWN
FakeMVRClient = _mvr.FakeMVRClient

CBSAAbsenceResult = _cbsa.CBSAAbsenceResult
MIN_ABSENCE_DAYS = _cbsa.MIN_ABSENCE_DAYS
FakeCBSAClient = _cbsa.FakeCBSAClient

FakeGeoNamesClient = _client.FakeGeoNamesClient

try:
    from fastapi.testclient import TestClient

    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001
    _HAS_FASTAPI = False


# ===========================================================================
# Pure logic: distance threshold boundary (Req 4.3)
# ===========================================================================


class TestDistanceThresholdBoundary:
    """The 100 km boundary is EXCLUSIVE: > 100 triggers, == 100 does not."""

    def test_threshold_constant_is_100km(self):
        assert DISTANCE_THRESHOLD_KM == 100.0

    def test_exactly_100km_does_not_trigger(self):
        # Req 4.3: distance must *exceed* 100 km; exactly 100 km is not enough.
        assert exceeds_distance_threshold(100.0) is False

    def test_100_001km_triggers(self):
        assert exceeds_distance_threshold(100.001) is True

    def test_smallest_increment_above_threshold_triggers(self):
        # Any value strictly greater than 100.0 must trigger.
        import math

        just_over = math.nextafter(100.0, math.inf)
        assert just_over > 100.0
        assert exceeds_distance_threshold(just_over) is True

    def test_just_below_threshold_does_not_trigger(self):
        assert exceeds_distance_threshold(99.999) is False


# ===========================================================================
# Pure logic: CBSA consecutive-day boundary (Req 4.9 / 13.2)
# ===========================================================================


class TestCBSAAbsenceBoundary:
    """>= 10 consecutive days outside Canada is the inclusive HIGH trigger."""

    def _result(self, days: int) -> "CBSAAbsenceResult":
        return CBSAAbsenceResult(
            available=True,
            absence_periods=[{"start": None, "end": None, "days": days}],
        )

    def test_min_absence_days_is_10(self):
        assert MIN_ABSENCE_DAYS == 10

    def test_nine_days_does_not_trigger(self):
        result = self._result(9)
        assert result.max_absence_days == 9
        assert result.has_long_absence is False

    def test_ten_days_triggers(self):
        result = self._result(10)
        assert result.max_absence_days == 10
        assert result.has_long_absence is True

    def test_eleven_days_triggers(self):
        assert self._result(11).has_long_absence is True

    def test_no_absence_periods_does_not_trigger(self):
        result = CBSAAbsenceResult(available=True, absence_periods=[])
        assert result.max_absence_days == 0
        assert result.has_long_absence is False

    def test_longest_period_drives_decision(self):
        # A mix of short and one long period should trigger on the longest.
        result = CBSAAbsenceResult(
            available=True,
            absence_periods=[
                {"days": 3},
                {"days": 9},
                {"days": 10},
            ],
        )
        assert result.max_absence_days == 10
        assert result.has_long_absence is True


# ===========================================================================
# Pure logic: MVR UNAVAILABLE -> UNKNOWN (Req 15.2 / 15.3)
# ===========================================================================


class TestMVROwnershipClassification:
    """An absent/unavailable MVR signal must never be read as 'no vehicle'."""

    def test_none_status_is_unknown_not_absent(self):
        # A missing status (adapter did not provide it / UNAVAILABLE) -> UNKNOWN.
        result = classify_ownership(None, province="ON")
        assert result.disposition == OWNERSHIP_UNKNOWN
        assert result.unknown is True
        assert result.no_vehicle is False

    def test_owned_status_is_owned(self):
        result = classify_ownership("OWNED", province="ON")
        assert result.disposition == OWNERSHIP_OWNED
        assert result.owns_vehicle is True

    def test_explicit_none_status_is_no_vehicle(self):
        # Only an explicit "no vehicle" confirmation maps to NONE.
        result = classify_ownership("NONE", province="ON")
        assert result.disposition == OWNERSHIP_NONE
        assert result.no_vehicle is True

    def test_unrecognised_status_is_unknown(self):
        # Conservative: an unknown status string must not become a false HIGH.
        result = classify_ownership("SOMETHING_WEIRD", province="ON")
        assert result.disposition == OWNERSHIP_UNKNOWN


# ===========================================================================
# End-to-end: POST /engine/analyze boundary behaviour with mocked adapters
# ===========================================================================


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not importable")
class TestAnalyzeBoundaries:
    """Drive the full endpoint at each boundary; every adapter is a fake."""

    # A residence at the equator/prime-meridian keeps the maths simple: moving
    # purely east/west along the equator makes distance ~ longitude-degrees * a
    # known constant, so we can pin points that are ~100 km apart.
    HOME = (0.0, 0.0)

    def _client(self, *, geonames, mvr=None, cbsa=None, budget_ms=600):
        settings = _main.get_settings()
        try:
            object.__setattr__(settings, "analysis_budget_ms", budget_ms)
        except Exception:  # noqa: BLE001
            settings.analysis_budget_ms = budget_ms
        _main.app.state.settings = settings
        _main.app.state.geonames = geonames
        _main.app.state.mvr = mvr
        _main.app.state.cbsa = cbsa
        _main.app.state.producer = None
        return TestClient(_main.app)

    def _geonames_at_distance(self, target_km: float) -> "FakeGeoNamesClient":
        """Build a GeoNames fake whose HOME/WORK points are ~``target_km`` apart.

        We place both points on the equator and solve for the work longitude so
        the engine's own haversine yields the requested distance, then assert
        the endpoint's reported ``distance_km`` matches our intent. This keeps
        the boundary test honest against the real distance computation.
        """
        # Distance along the equator per degree of longitude (km), from the
        # engine's own primitive so we stay consistent with its rounding.
        geo = load_service_module("geographic-anomaly-engine", "geo", siblings=_SIBLINGS)
        km_per_degree = geo.haversine(0.0, 0.0, 0.0, 1.0)
        work_lon = target_km / km_per_degree
        fake = FakeGeoNamesClient()
        fake.add("HOME", *self.HOME)
        fake.add("WORK", 0.0, work_lon)
        return fake

    def _request(self, **overrides) -> dict:
        req = {
            "claim_id": "22222222-2222-2222-2222-222222222222",
            "residential_postal_code": "HOME",
            "employer_work_location": "WORK",
            "sin_hash": "sinhash-boundary",
            "residential_province": "ON",
            "transport_mode_costs": {"bus": 10.00},
            "weekly_benefit_rate": 500.00,
            "transit_route_exists": True,
        }
        req.update(overrides)
        return req

    def _by_algorithm(self, body, algorithm):
        return [i for i in body["indicators"] if i["algorithm"] == algorithm]

    # ── Distance threshold (Req 4.3) ──

    def test_exactly_100km_skips_distance_dependent_checks(self):
        """At exactly 100 km the MVR check must be skipped (no HIGH Indicator)."""
        geonames = self._geonames_at_distance(100.0)
        mvr = FakeMVRClient()
        mvr.set_none("sinhash-boundary")  # would be HIGH *if* the check ran
        client = self._client(geonames=geonames, mvr=mvr)
        resp = client.post("/engine/analyze", json=self._request())
        assert resp.status_code == 200
        body = resp.json()
        assert body["distance_km"] == pytest.approx(100.0, abs=0.01)
        assert body["distance_km"] <= 100.0001
        # No distance-dependent analysis => MVR never queried, no vehicle flag.
        assert self._by_algorithm(body, "transport_implausibility_vehicle") == []
        assert mvr.calls == []

    def test_just_over_100km_triggers_distance_dependent_checks(self):
        """Just above 100 km the MVR check runs and the HIGH Indicator fires."""
        geonames = self._geonames_at_distance(100.5)
        mvr = FakeMVRClient()
        mvr.set_none("sinhash-boundary")
        client = self._client(geonames=geonames, mvr=mvr)
        resp = client.post("/engine/analyze", json=self._request())
        assert resp.status_code == 200
        body = resp.json()
        assert body["distance_km"] > 100.0
        matches = self._by_algorithm(body, "transport_implausibility_vehicle")
        assert len(matches) == 1
        assert matches[0]["severity"] == Severity.HIGH.value
        assert matches[0]["engine"] == EngineType.GEOGRAPHIC_ANOMALY.value
        assert mvr.calls  # the adapter WAS queried past the threshold

    # ── CBSA consecutive-day boundary (Req 4.9) ──

    def test_cbsa_nine_days_no_indicator(self):
        geonames = self._geonames_at_distance(150.0)
        cbsa = FakeCBSAClient()
        cbsa.set_absence_days("sinhash-boundary", 9)
        client = self._client(geonames=geonames, cbsa=cbsa)
        resp = client.post("/engine/analyze", json=self._request())
        body = resp.json()
        assert self._by_algorithm(body, "cbsa_absence_detection") == []

    def test_cbsa_ten_days_yields_high_indicator(self):
        geonames = self._geonames_at_distance(150.0)
        cbsa = FakeCBSAClient()
        cbsa.set_absence_days("sinhash-boundary", 10)
        client = self._client(geonames=geonames, cbsa=cbsa)
        resp = client.post("/engine/analyze", json=self._request())
        body = resp.json()
        matches = self._by_algorithm(body, "cbsa_absence_detection")
        assert len(matches) == 1
        assert matches[0]["severity"] == Severity.HIGH.value
        assert matches[0]["payload"]["max_absence_days"] == 10

    # ── MVR UNAVAILABLE -> UNKNOWN (Req 15.2 / 15.3) ──

    def test_mvr_unavailable_treated_as_unknown_no_high_indicator(self):
        """UNAVAILABLE MVR => ownership UNKNOWN (not absent) => no HIGH flag."""
        geonames = self._geonames_at_distance(150.0)
        mvr = FakeMVRClient()
        mvr.set_unavailable("sinhash-boundary")
        client = self._client(geonames=geonames, mvr=mvr)
        resp = client.post("/engine/analyze", json=self._request())
        assert resp.status_code == 200
        body = resp.json()
        # Distance is well over 100 km, so the check ran, yet UNKNOWN ownership
        # must NOT produce the HIGH "no vehicle" Indicator.
        assert body["distance_km"] > 100.0
        assert mvr.calls  # the adapter WAS queried
        assert self._by_algorithm(body, "transport_implausibility_vehicle") == []
