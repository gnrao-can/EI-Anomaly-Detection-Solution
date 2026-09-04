"""
tests/unit/test_mvr_adapter.py
==============================
Unit and property tests for the MVR (Motor Vehicle Registry) Adapter (task 23.1).

Coverage:
  * Data-minimisation projection (Requirement 15.4):
      - Only ``vehicle_ownership_status`` and ``vehicle_class`` are ever
        returned; history / insurance / financial / VIN / owner-name fields are
        stripped.
      - Property test: for any record with arbitrary extra keys, the projection
        never leaks a non-allowed field.
  * 3-second timeout -> UNAVAILABLE (Requirements 15.2, 15.3).
  * Error -> UNAVAILABLE (Requirement 15.2).
  * Per-province synthetic fixture served in dev (Requirement 15.6):
      - All ten provinces produce a record; deterministic for identical inputs.
      - Endpoint returns only the two allowed fields.
  * UNKNOWN ownership handled (Requirement 15.2) — distinct from NONE.

The service directory is hyphenated (``services/mvr-adapter``) and its modules
import each other by bare name (``from client import ...``). To avoid colliding
with other services' bare ``main`` / ``config`` / ``client`` modules across the
whole suite, the modules are loaded under UNIQUE namespaced names (``mvr_main``,
``mvr_client``, ``mvr_config``) via importlib; the bare names are registered only
transiently while executing each module, then the previous ``sys.modules`` state
is restored — mirroring ``tests/unit/test_cbsa_adapter.py``.

Requirements: 15.1, 15.2, 15.3, 15.4, 15.5, 15.6
"""

from __future__ import annotations

import importlib.util
import os
import sys
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Load the hyphenated mvr-adapter service modules under namespaced names.
# ---------------------------------------------------------------------------

_SERVICE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "services", "mvr-adapter")
)
_MOCK_DIR = os.path.abspath(
    os.path.join(_SERVICE_DIR, "..", "..", "mock-data", "motor-vehicle")
)

# The service modules import each other by BARE name (``from client import ...``)
# while executing. To satisfy those bare imports without permanently polluting
# the shared ``sys.modules`` namespace (which collides with other services'
# ``config`` / ``client`` / ``main`` modules), we:
#   1. snapshot any pre-existing bare-name modules,
#   2. register each module under BOTH a namespaced ``mvr_*`` key and the bare
#      key so intra-package imports resolve while executing,
#   3. restore the snapshot afterwards, leaving only the namespaced ``mvr_*``
#      entries behind.
_BARE_NAMES = ("config", "client", "main")
_SAVED = {name: sys.modules.get(name) for name in _BARE_NAMES}


def _load_service_modules() -> dict[str, Any]:
    if os.path.isdir(_SERVICE_DIR) and _SERVICE_DIR not in sys.path:
        sys.path.insert(0, _SERVICE_DIR)

    loaded: dict[str, Any] = {}
    for bare, filename in (
        ("config", "config.py"),
        ("client", "client.py"),
        ("main", "main.py"),
    ):
        spec = importlib.util.spec_from_file_location(
            bare, os.path.join(_SERVICE_DIR, filename)
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        # Register under the bare name so sibling ``from X import Y`` works, and
        # under a namespaced key so tests can reference it stably.
        sys.modules[bare] = module
        sys.modules[f"mvr_{bare}"] = module
        spec.loader.exec_module(module)
        loaded[bare] = module

    # Restore the pre-existing bare-name modules so other test files that also
    # load a bare ``config`` / ``client`` / ``main`` are not affected by ours.
    for name, saved in _SAVED.items():
        if saved is not None:
            sys.modules[name] = saved
        else:
            sys.modules.pop(name, None)

    return loaded


_loaded = _load_service_modules()
_config = _loaded["config"]
_client = _loaded["client"]
_main = _loaded["main"]

# Task 29.1: protected routes require the INVESTIGATOR role; inject a principal.
from shared.auth import get_current_user as _get_current_user  # noqa: E402

_main.app.dependency_overrides[_get_current_user] = lambda: {"roles": ["INVESTIGATOR"]}

ALLOWED_FIELDS = _client.ALLOWED_FIELDS
project_allowed_fields = _client.project_allowed_fields
MVRResult = _client.MVRResult
SyntheticMVRGenerator = _client.SyntheticMVRGenerator
lookup_vehicle = _client.lookup_vehicle
OWNERSHIP_UNKNOWN = _client.OWNERSHIP_UNKNOWN
OWNERSHIP_NONE = _client.OWNERSHIP_NONE
OWNERSHIP_OWNED = _client.OWNERSHIP_OWNED
PROVINCES = _config.PROVINCES

try:
    from fastapi.testclient import TestClient

    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001
    _HAS_FASTAPI = False

try:
    from hypothesis import given, settings as hyp_settings
    from hypothesis import strategies as st

    _HAS_HYPOTHESIS = True
except Exception:  # noqa: BLE001
    _HAS_HYPOTHESIS = False


# ===========================================================================
# Data-minimisation projection (Requirement 15.4)
# ===========================================================================


class TestMinimisationProjection:
    def test_only_two_allowed_fields(self):
        assert ALLOWED_FIELDS == frozenset({"vehicle_ownership_status", "vehicle_class"})

    def test_strips_history_insurance_and_financial_fields(self):
        raw = {
            "vehicle_ownership_status": "OWNED",
            "vehicle_class": "TRUCK",
            "vin": "ABC123",
            "vehicle_history": ["registered", "renewed"],
            "insurance_provider": "Some Insurer",
            "lien_balance": 12345.67,
            "registered_owner_name": "Jane Doe",
            "registration_expiry": "2026-01-01",
        }
        minimised = project_allowed_fields(raw)
        assert set(minimised.keys()) == {"vehicle_ownership_status", "vehicle_class"}
        assert minimised["vehicle_ownership_status"] == "OWNED"
        assert minimised["vehicle_class"] == "TRUCK"
        # Explicitly assert the sensitive fields are gone.
        for forbidden in (
            "vin",
            "vehicle_history",
            "insurance_provider",
            "lien_balance",
            "registered_owner_name",
            "registration_expiry",
        ):
            assert forbidden not in minimised

    def test_missing_allowed_fields_not_fabricated(self):
        minimised = project_allowed_fields({"vehicle_class": "PASSENGER", "vin": "X"})
        assert minimised == {"vehicle_class": "PASSENGER"}

    def test_empty_record_yields_empty_projection(self):
        assert project_allowed_fields({}) == {}


if _HAS_HYPOTHESIS:

    class TestMinimisationProperty:
        @hyp_settings(max_examples=200)
        @given(
            extra=st.dictionaries(
                keys=st.text(min_size=1, max_size=20).filter(
                    lambda k: k not in {"vehicle_ownership_status", "vehicle_class"}
                ),
                values=st.one_of(
                    st.text(max_size=20),
                    st.integers(),
                    st.floats(allow_nan=False, allow_infinity=False),
                    st.lists(st.text(max_size=5), max_size=5),
                    st.none(),
                ),
                max_size=8,
            ),
            status=st.sampled_from(["OWNED", "NONE", "UNKNOWN"]),
            vclass=st.one_of(st.none(), st.sampled_from(["PASSENGER", "TRUCK"])),
        )
        def test_projection_never_leaks_extra_fields(self, extra, status, vclass):
            record = dict(extra)
            record["vehicle_ownership_status"] = status
            record["vehicle_class"] = vclass
            minimised = project_allowed_fields(record)
            # No key outside the allow-list may survive.
            assert set(minimised.keys()) <= ALLOWED_FIELDS
            # Allowed values are preserved exactly.
            assert minimised["vehicle_ownership_status"] == status
            assert minimised["vehicle_class"] == vclass


# ===========================================================================
# Per-province synthetic fixtures served in dev (Requirement 15.6)
# ===========================================================================


class TestSyntheticGenerator:
    def _gen(self) -> "SyntheticMVRGenerator":
        return SyntheticMVRGenerator(_MOCK_DIR)

    def test_all_ten_provinces_have_fixtures(self):
        assert len(PROVINCES) == 10
        for province in PROVINCES:
            path = os.path.join(_MOCK_DIR, f"{province}.json")
            assert os.path.isfile(path), f"missing fixture for {province}"

    def test_every_province_generates_a_record(self):
        gen = self._gen()
        for province in PROVINCES:
            raw = gen.generate("hash-abc", province)
            assert "vehicle_ownership_status" in raw
            # Minimised form always has only allowed keys.
            assert set(project_allowed_fields(raw).keys()) <= ALLOWED_FIELDS

    def test_deterministic_same_inputs_same_output(self):
        gen = self._gen()
        a = gen.generate("hash-abc", "AB")
        b = gen.generate("hash-abc", "AB")
        assert a == b

    def test_none_fixture_reports_no_vehicle(self):
        """NL fixture pins ownership NONE with a null vehicle class."""
        gen = self._gen()
        raw = gen.generate("any", "NL")
        minimised = project_allowed_fields(raw)
        assert minimised["vehicle_ownership_status"] == OWNERSHIP_NONE
        assert minimised["vehicle_class"] is None

    def test_unknown_fixture_reports_unknown(self):
        """ON fixture pins ownership UNKNOWN (distinct from NONE)."""
        gen = self._gen()
        raw = gen.generate("any", "ON")
        minimised = project_allowed_fields(raw)
        assert minimised["vehicle_ownership_status"] == OWNERSHIP_UNKNOWN

    def test_unknown_province_falls_back_without_error(self):
        gen = self._gen()
        raw = gen.generate("hash-abc", "ZZ")
        assert "vehicle_ownership_status" in raw


# ===========================================================================
# lookup_vehicle orchestration: synthetic OK / error -> UNAVAILABLE (15.2)
# ===========================================================================


class _FakeSettings:
    def __init__(self, use_synthetic=True, mock_dir=_MOCK_DIR, timeout=3.0):
        self.use_synthetic = use_synthetic
        self.mvr_mock_data_dir = mock_dir
        self.mvr_timeout = timeout
        self.mvr_api_key = ""
        self.mvr_province_base_urls = {p: f"https://mvr.{p.lower()}.ca" for p in PROVINCES}

    def base_url_for(self, province):
        return self.mvr_province_base_urls.get((province or "").upper(), "")


class TestLookupOrchestration:
    def test_synthetic_returns_minimised_ok(self):
        result = lookup_vehicle(_FakeSettings(), "hash-abc", "AB")
        assert result.status == "OK"
        assert set(result.record.keys()) <= ALLOWED_FIELDS

    def test_no_live_client_returns_unavailable(self):
        settings = _FakeSettings(use_synthetic=False)
        result = lookup_vehicle(settings, "hash-abc", "AB", live_client=None)
        assert result.status == "UNAVAILABLE"

    def test_live_client_error_returns_unavailable(self):
        """A live client raising -> its own fetch converts to UNAVAILABLE."""
        settings = _FakeSettings(use_synthetic=False)

        class _ErroringClient:
            def fetch_vehicle_record(self, sin_hash, province):
                return MVRResult.unavailable("MVR fetch failed: ConnectError")

        result = lookup_vehicle(
            settings, "hash-abc", "AB", live_client=_ErroringClient()
        )
        assert result.status == "UNAVAILABLE"
        assert "ConnectError" in (result.reason or "")


# ===========================================================================
# Endpoint tests (Requirements 15.1, 15.2, 15.3, 15.4, 15.6)
# ===========================================================================


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not importable")
class TestVehicleEndpoint:
    def _client_synthetic(self):
        _main.app.state.settings = _main.get_settings()
        _main.app.state.synthetic = SyntheticMVRGenerator(_MOCK_DIR)
        _main.app.state.mvr_clients = {}
        return TestClient(_main.app)

    def test_returns_only_allowed_fields(self):
        resp = self._client_synthetic().get(
            "/mvr/hash-abc", params={"province": "AB"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "OK"
        assert set(body["record"].keys()) == {"vehicle_ownership_status", "vehicle_class"}
        assert body["record"]["vehicle_ownership_status"] == "OWNED"
        assert body["record"]["vehicle_class"] == "TRUCK"

    def test_unknown_ownership_handled(self):
        """ON fixture -> UNKNOWN ownership surfaces intact (not coerced to NONE)."""
        resp = self._client_synthetic().get(
            "/mvr/hash-abc", params={"province": "ON"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["record"]["vehicle_ownership_status"] == "UNKNOWN"

    def test_no_vehicle_reported_as_none(self):
        resp = self._client_synthetic().get(
            "/mvr/hash-abc", params={"province": "NL"}
        )
        assert resp.status_code == 200
        assert resp.json()["record"]["vehicle_ownership_status"] == "NONE"

    def test_healthz_ok_in_synthetic_mode(self):
        resp = self._client_synthetic().get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["mode"] == "synthetic"
        assert sorted(resp.json()["allowed_fields"]) == [
            "vehicle_class",
            "vehicle_ownership_status",
        ]


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not importable")
class TestEndpointFailureHandling:
    """Force production mode and inject fakes to exercise timeout/error paths."""

    def _client_prod(self, fake_client=None, sleep_forever=False):
        class _ProdSettings:
            use_synthetic = False
            mvr_mock_data_dir = _MOCK_DIR
            mvr_timeout = 0.2 if sleep_forever else 3.0
            mvr_api_key = "test-key"
            mvr_province_base_urls = {p: f"https://mvr.{p.lower()}.ca" for p in PROVINCES}

            def base_url_for(self, province):
                return self.mvr_province_base_urls.get((province or "").upper(), "")

        _main.app.state.settings = _ProdSettings()
        _main.app.state.synthetic = SyntheticMVRGenerator(_MOCK_DIR)
        _main.app.state.mvr_clients = {"AB": fake_client} if fake_client else {}
        return TestClient(_main.app)

    def test_error_returns_unavailable(self):
        class _ErroringClient:
            def fetch_vehicle_record(self, sin_hash, province):
                return MVRResult.unavailable("MVR fetch failed: HTTPStatusError")

            def close(self):
                pass

        resp = self._client_prod(_ErroringClient()).get(
            "/mvr/hash-x", params={"province": "AB"}
        )
        assert resp.status_code == 502
        body = resp.json()
        assert body["status"] == "UNAVAILABLE"

    def test_timeout_returns_unavailable(self):
        """A client that blocks past the (tiny) timeout -> UNAVAILABLE (15.3)."""
        import time as _time

        class _SlowClient:
            def fetch_vehicle_record(self, sin_hash, province):
                _time.sleep(2.0)  # far exceeds the 0.2s test timeout
                return MVRResult.ok({"vehicle_ownership_status": "OWNED"})

            def close(self):
                pass

        resp = self._client_prod(_SlowClient(), sleep_forever=True).get(
            "/mvr/hash-slow", params={"province": "AB"}
        )
        assert resp.status_code == 502
        body = resp.json()
        assert body["status"] == "UNAVAILABLE"
        assert body["reason"] == "TIMEOUT"

    def test_no_client_for_province_returns_unavailable(self):
        # No fake injected and no base URL resolvable -> UNAVAILABLE.
        class _ProdSettingsNoUrl:
            use_synthetic = False
            mvr_mock_data_dir = _MOCK_DIR
            mvr_timeout = 3.0
            mvr_api_key = "test-key"
            mvr_province_base_urls = {}

            def base_url_for(self, province):
                return ""

        _main.app.state.settings = _ProdSettingsNoUrl()
        _main.app.state.synthetic = SyntheticMVRGenerator(_MOCK_DIR)
        _main.app.state.mvr_clients = {}
        resp = TestClient(_main.app).get("/mvr/hash-y", params={"province": "AB"})
        assert resp.status_code == 502
        assert resp.json()["status"] == "UNAVAILABLE"
