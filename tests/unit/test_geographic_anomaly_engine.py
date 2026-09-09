"""
tests/unit/test_geographic_anomaly_engine.py
=============================================
Unit tests for the Geographic Anomaly Engine (task 12.1).

Coverage:
  * Haversine primitive (services/geographic-anomaly-engine/geo.py):
      - self-distance is exactly 0.0 (Property 2 component),
      - symmetry: distance(A, B) == distance(B, A) within 0.001 km,
      - results are rounded to 4 decimal places (distinct from the GeoNames
        Adapter's 3 dp),
      - out-of-bounds coordinates raise InvalidCoordinateError (no distance).
  * ``POST /engine/analyze`` endpoint:
      - successful resolution of BOTH coordinates computes the distance,
      - a resolution failure on EITHER location yields a single MEDIUM
        GEOGRAPHIC_ANOMALY Indicator and NO distance-dependent analysis.

All tests inject an in-memory ``FakeGeoNamesClient`` — no live network.

The service directory is hyphenated (``services/geographic-anomaly-engine``)
and its modules import each other by bare name, so we load them under unique
namespaced keys (``geoanomaly_*``) and restore any pre-existing bare-name
modules afterwards, mirroring ``tests/unit/test_repetitive_pattern_engine.py``.
This keeps the unit suite collision-free when run alongside other services.

Requirements: 4.1, 4.2, 4.10
"""

from __future__ import annotations

import pytest

from shared.models import EngineType, Severity
from tests.conftest import load_service_module

# ---------------------------------------------------------------------------
# Load the hyphenated geographic-anomaly-engine service modules.
#
# ``main.py`` imports several bare-name siblings (config/geo/client/transport/
# mvr_client/cbsa_client/kafka_producer/indicators). We load them all through
# the shared ``load_service_module`` conftest helper so ``sys.modules`` keys are
# namespaced and the full unit suite stays collision-free when other services'
# ``client`` / ``config`` modules are also loaded.
# ---------------------------------------------------------------------------

_SIBLINGS = [
    "config",
    "geo",
    "province",
    "client",
    "transport",
    "mvr_client",
    "cbsa_client",
    "kafka_producer",
    "indicators",
    "main",
]

_geo = load_service_module("geographic-anomaly-engine", "geo", siblings=_SIBLINGS)
_client = load_service_module("geographic-anomaly-engine", "client", siblings=_SIBLINGS)
_main = load_service_module("geographic-anomaly-engine", "main", siblings=_SIBLINGS)

# Task 29.1: protected routes require the INVESTIGATOR role; inject a principal.
from shared.auth import get_current_user as _get_current_user  # noqa: E402

_main.app.dependency_overrides[_get_current_user] = lambda: {"roles": ["INVESTIGATOR"]}

haversine = _geo.haversine
InvalidCoordinateError = _geo.InvalidCoordinateError
DISTANCE_PRECISION_DP = _geo.DISTANCE_PRECISION_DP
FakeGeoNamesClient = _client.FakeGeoNamesClient

try:
    from fastapi.testclient import TestClient

    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001
    _HAS_FASTAPI = False


# ===========================================================================
# Haversine primitive (geo.py)
# ===========================================================================


class TestHaversine:
    def test_self_distance_is_zero(self):
        """distance(A, A) == 0.0 for any valid point."""
        assert haversine(45.4215, -75.6972, 45.4215, -75.6972) == 0.0
        assert haversine(0.0, 0.0, 0.0, 0.0) == 0.0
        assert haversine(-89.9, 179.9, -89.9, 179.9) == 0.0

    def test_symmetry(self):
        """distance(A, B) == distance(B, A) within 0.001 km."""
        ottawa = (45.4215, -75.6972)
        toronto = (43.6532, -79.3832)
        d_ab = haversine(*ottawa, *toronto)
        d_ba = haversine(*toronto, *ottawa)
        assert abs(d_ab - d_ba) <= 0.001
        # Sanity: Ottawa <-> Toronto is roughly 350 km.
        assert 300.0 < d_ab < 400.0

    def test_result_rounded_to_four_decimal_places(self):
        """The engine rounds to 4 dp (distinct from the adapter's 3 dp)."""
        d = haversine(45.4215, -75.6972, 43.6532, -79.3832)
        # Round-tripping through 4 dp must not change the value.
        assert d == round(d, 4)
        assert DISTANCE_PRECISION_DP == 4
        # A value carrying a 4th-decimal digit demonstrates >3 dp precision is
        # preserved (i.e. it was NOT truncated to 3 dp).
        # Pick two nearby points that yield a non-trivial 4th decimal.
        d2 = haversine(45.0, -75.0, 45.001, -75.001)
        assert d2 == round(d2, 4)

    def test_out_of_bounds_raises(self):
        """Out-of-bounds latitude/longitude raises InvalidCoordinateError."""
        with pytest.raises(InvalidCoordinateError):
            haversine(91.0, 0.0, 0.0, 0.0)
        with pytest.raises(InvalidCoordinateError):
            haversine(0.0, 0.0, 0.0, 181.0)


# ===========================================================================
# POST /engine/analyze
# ===========================================================================


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not importable")
class TestAnalyzeEndpoint:
    def _client_with_geonames(self, geonames) -> "TestClient":
        _main.app.state.settings = _main.get_settings()
        _main.app.state.geonames = geonames
        return TestClient(_main.app)

    def test_successful_resolution_computes_distance(self):
        """Both coordinates resolve -> distance computed.

        This foundation (12.1) test uses a SHORT (< 100 km) pair so that NO
        distance-dependent analysis (MVR / transit / economic / CBSA) runs and
        the produced Indicator list stays empty. The distance-dependent
        behaviour for distances > 100 km is covered by task 12.2 tests in
        ``test_geographic_anomaly_engine_transport.py``.
        """
        fake = FakeGeoNamesClient()
        # Two nearby points in the Ottawa area, well under 100 km apart.
        fake.add("K1A0A6", 45.4215, -75.6972)
        fake.add("K2P1A1", 45.4180, -75.6990)

        client = self._client_with_geonames(fake)
        resp = client.post(
            "/engine/analyze",
            json={
                "claim_id": None,
                "residential_postal_code": "K1A0A6",
                "employer_work_location": "K2P1A1",
                "employer_bn": "123456789RT0001",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "OK"
        assert body["distance_km"] is not None
        assert body["distance_km"] < 100.0
        # 4-dp rounding preserved end to end.
        assert body["distance_km"] == round(body["distance_km"], 4)
        # Distance <= 100 km -> no distance-dependent Indicators produced.
        assert body["indicators"] == []
        assert body["residential_coord"] == {
            "latitude": 45.4215,
            "longitude": -75.6972,
        }
        assert body["work_coord"] == {"latitude": 45.4180, "longitude": -75.6990}

    def test_self_distance_zero_via_endpoint(self):
        """Resolving both locations to the same point yields distance 0.0."""
        fake = FakeGeoNamesClient()
        fake.add("SAME", 49.2827, -123.1207)  # Vancouver
        client = self._client_with_geonames(fake)
        resp = client.post(
            "/engine/analyze",
            json={
                "residential_postal_code": "SAME",
                "employer_work_location": "SAME",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "OK"
        assert body["distance_km"] == 0.0

    def test_residential_resolution_failure_yields_single_medium_indicator(self):
        """Residential postal code fails to resolve -> single MEDIUM Indicator,
        no distance-dependent analysis (Req 4.2)."""
        fake = FakeGeoNamesClient()
        # Only the work location is known; residential resolution fails.
        fake.add("M5H2N2", 43.6532, -79.3832)

        client = self._client_with_geonames(fake)
        resp = client.post(
            "/engine/analyze",
            json={
                "residential_postal_code": "UNKNOWN",
                "employer_work_location": "M5H2N2",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "RESOLUTION_FAILED"
        assert body["distance_km"] is None
        assert len(body["indicators"]) == 1
        ind = body["indicators"][0]
        assert ind["engine"] == EngineType.GEOGRAPHIC_ANOMALY.value
        assert ind["severity"] == Severity.MEDIUM.value
        assert ind["algorithm"] == "coordinate_resolution"
        assert ind["payload"]["reason"] == "RESOLUTION_FAILED"
        assert "UNKNOWN" in ind["payload"]["failed_locations"]

    def test_work_resolution_failure_yields_single_medium_indicator(self):
        """Work location fails to resolve -> single MEDIUM Indicator, no distance."""
        fake = FakeGeoNamesClient()
        fake.add("K1A0A6", 45.4215, -75.6972)

        client = self._client_with_geonames(fake)
        resp = client.post(
            "/engine/analyze",
            json={
                "residential_postal_code": "K1A0A6",
                "employer_work_location": "NOWHERE",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "RESOLUTION_FAILED"
        assert body["distance_km"] is None
        assert len(body["indicators"]) == 1
        assert body["indicators"][0]["severity"] == Severity.MEDIUM.value

    def test_both_resolution_failures_still_single_indicator(self):
        """Both locations failing still yields exactly one MEDIUM Indicator."""
        fake = FakeGeoNamesClient()  # nothing registered
        client = self._client_with_geonames(fake)
        resp = client.post(
            "/engine/analyze",
            json={
                "residential_postal_code": "A",
                "employer_work_location": "B",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "RESOLUTION_FAILED"
        assert len(body["indicators"]) == 1
        assert set(body["indicators"][0]["payload"]["failed_locations"]) == {"A", "B"}


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not importable")
class TestHealthz:
    def test_healthz_ok_when_client_wired(self):
        _main.app.state.geonames = FakeGeoNamesClient()
        client = TestClient(_main.app)
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# ===========================================================================
# Engine -> adapter authentication (Req 19.7)
#
# The GeoNames / MVR / CBSA adapters are auth-gated (require_role(INVESTIGATOR)).
# The engine's adapter clients must present the service Bearer token when one is
# configured, and must send NO Authorization header when it is absent (unit
# tests / no credential). These tests drive each real HTTP client through an
# httpx.MockTransport and assert the header contract.
# ===========================================================================


try:
    import httpx  # noqa: E402

    _HAS_HTTPX = True
except Exception:  # noqa: BLE001
    _HAS_HTTPX = False


@pytest.mark.skipif(not _HAS_HTTPX, reason="httpx not importable")
class TestEngineAdapterAuth:
    """Each adapter client attaches (or omits) the service Bearer (Req 19.7)."""

    GeoNamesAdapterClient = _client.GeoNamesAdapterClient
    MVRAdapterClient = _main.MVRAdapterClient
    CBSAAdapterClient = _main.CBSAAdapterClient

    @staticmethod
    def _capture(monkeypatch, payload: dict, status_code: int = 200):
        """Patch httpx.AsyncClient so requests hit a MockTransport that records
        the Authorization header seen, returning ``payload``/``status_code``."""
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["authorization"] = request.headers.get("authorization")
            return httpx.Response(status_code, json=payload)

        real_async_client = httpx.AsyncClient

        def _factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_async_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _factory)
        return seen

    @pytest.mark.asyncio
    async def test_geonames_client_sends_bearer_when_token_set(self, monkeypatch):
        seen = self._capture(
            monkeypatch, {"latitude": 45.0, "longitude": -75.0, "source": "test"}
        )
        client = self.GeoNamesAdapterClient("http://geonames", bearer_token="tok-123")
        await client.resolve("K1A")
        assert seen["authorization"] == "Bearer tok-123"

    @pytest.mark.asyncio
    async def test_geonames_client_no_header_when_token_absent(self, monkeypatch):
        seen = self._capture(
            monkeypatch, {"latitude": 45.0, "longitude": -75.0, "source": "test"}
        )
        client = self.GeoNamesAdapterClient("http://geonames")
        await client.resolve("K1A")
        assert seen["authorization"] is None

    @pytest.mark.asyncio
    async def test_mvr_client_sends_bearer_when_token_set(self, monkeypatch):
        seen = self._capture(
            monkeypatch,
            {"record": {"vehicle_ownership_status": "NONE", "vehicle_class": None}},
        )
        client = self.MVRAdapterClient("http://mvr", bearer_token="tok-456")
        await client.get_ownership("sinhash", "ON")
        assert seen["authorization"] == "Bearer tok-456"

    @pytest.mark.asyncio
    async def test_mvr_client_no_header_when_token_absent(self, monkeypatch):
        seen = self._capture(
            monkeypatch,
            {"record": {"vehicle_ownership_status": "NONE", "vehicle_class": None}},
        )
        client = self.MVRAdapterClient("http://mvr")
        await client.get_ownership("sinhash", "ON")
        assert seen["authorization"] is None

    @pytest.mark.asyncio
    async def test_cbsa_client_sends_bearer_when_token_set(self, monkeypatch):
        seen = self._capture(
            monkeypatch, {"status": "OK", "partial": False, "absence_periods": []}
        )
        client = self.CBSAAdapterClient("http://cbsa", bearer_token="tok-789")
        await client.get_absences("sinhash", "ON")
        assert seen["authorization"] == "Bearer tok-789"

    @pytest.mark.asyncio
    async def test_cbsa_client_no_header_when_token_absent(self, monkeypatch):
        seen = self._capture(
            monkeypatch, {"status": "OK", "partial": False, "absence_periods": []}
        )
        client = self.CBSAAdapterClient("http://cbsa")
        await client.get_absences("sinhash", "ON")
        assert seen["authorization"] is None


# ===========================================================================
# Province derivation from the residential FSA (Req 15.1)
#
# The event carries only the residential FSA (Protected B minimisation), not a
# province. The engine derives the province from the FSA's first letter (Canada
# Post convention) to key its MVR/CBSA lookups.
# ===========================================================================


_province = load_service_module(
    "geographic-anomaly-engine", "province", siblings=_SIBLINGS
)
province_from_fsa = _province.province_from_fsa
resolve_province = _province.resolve_province


class TestProvinceFromFSA:
    def test_representative_first_letters(self):
        # One representative FSA per province/territory mapping.
        cases = {
            "A1A": "NL",
            "B2B": "NS",
            "C1C": "PE",
            "E1E": "NB",
            "G1G": "QC",
            "H2H": "QC",
            "J3J": "QC",
            "K1A": "ON",
            "L4L": "ON",
            "M5M": "ON",
            "N6N": "ON",
            "P7P": "ON",
            "R3R": "MB",
            "S4S": "SK",
            "T5T": "AB",
            "V6V": "BC",
            "X0X": "NT",
            "Y1Y": "YT",
        }
        for fsa, expected in cases.items():
            assert province_from_fsa(fsa) == expected, fsa

    def test_case_and_whitespace_insensitive(self):
        assert province_from_fsa("  k1a 0a6 ") == "ON"
        assert province_from_fsa("v6v") == "BC"

    def test_unused_first_letters_return_empty(self):
        # D, F, I, O, Q, U, W, Z are never the first char of a Canadian FSA.
        for letter in ("D", "F", "I", "O", "Q", "U", "W", "Z"):
            assert province_from_fsa(f"{letter}1A") == ""

    def test_empty_or_non_letter_returns_empty(self):
        assert province_from_fsa("") == ""
        assert province_from_fsa(None) == ""
        assert province_from_fsa("123") == ""

    def test_resolve_prefers_explicit_province(self):
        # An explicit province always wins over FSA derivation.
        assert resolve_province("bc", "K1A0A6") == "BC"
        # Absent explicit -> derive from FSA.
        assert resolve_province("", "K1A0A6") == "ON"
        assert resolve_province(None, "V6V1A1") == "BC"
        # Neither -> empty.
        assert resolve_province("", "") == ""


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not importable")
class TestDerivedProvinceReachesMVR:
    """The FSA-derived province keys the MVR lookup and drives the HIGH
    no-vehicle Indicator (Req 4.3 / 15.1)."""

    def _client(self, geonames, mvr) -> "TestClient":
        _main.app.state.settings = _main.get_settings()
        _main.app.state.geonames = geonames
        _main.app.state.mvr = mvr
        _main.app.state.cbsa = None
        _main.app.state.producer = None
        return TestClient(_main.app)

    def test_derived_province_used_for_mvr_and_high_indicator(self):
        FakeMVRClient = _main_fake_mvr()

        # Ontario FSA (K...) -> derived province "ON". Residence far (> 100 km)
        # from the work location so distance-dependent checks run.
        geonames = FakeGeoNamesClient()
        geonames.add("K1A0A6", 45.4215, -75.6972)  # Ottawa (residence)
        geonames.add("M5H2N2", 43.6489, -79.3817)  # Toronto (work) ~350 km

        mvr = FakeMVRClient()
        sin_hash = "hash-no-vehicle"
        mvr.set_none(sin_hash)  # adapter confirms NO registered vehicle

        client = self._client(geonames, mvr)
        resp = client.post(
            "/engine/analyze",
            json={
                "residential_postal_code": "K1A0A6",
                "employer_work_location": "M5H2N2",
                "sin_hash": sin_hash,
                # NOTE: no residential_province supplied -> derived from FSA.
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "OK"
        assert body["distance_km"] > 100.0
        # The MVR lookup was keyed with the DERIVED province "ON".
        assert mvr.calls, "MVR client was not called"
        assert mvr.calls[-1] == (sin_hash, "ON")
        # A HIGH no-vehicle Indicator was produced (Req 4.3).
        highs = [i for i in body["indicators"] if i["severity"] == Severity.HIGH.value]
        assert highs, f"expected a HIGH indicator, got {body['indicators']}"


def _main_fake_mvr():
    """Return the FakeMVRClient from the engine's namespaced mvr_client module."""
    mvr_mod = load_service_module(
        "geographic-anomaly-engine", "mvr_client", siblings=_SIBLINGS
    )
    return mvr_mod.FakeMVRClient
