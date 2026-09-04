"""
tests/unit/test_business_registry_adapter.py
============================================
Unit tests for the Business Registry Adapter (task 20.1).

Covered behaviours (Requirements 12.1-12.6):
  - All registries reachable                 -> status OK, records aggregated
    from the federal + provincial fixtures.
  - One province unreachable                  -> status PARTIAL_RESULT and the
    unreachable jurisdiction is listed.
  - Every registry unreachable                -> status SERVICE_UNAVAILABLE (and
    the endpoint surfaces HTTP 503).
  - Zoning classification via GeoNames         -> address_type reflects the
    GeoNames answer, unverified_zoning is False.
  - Zoning fallback when GeoNames unavailable  -> FSA-range fallback classifies
    the address and flags unverified_zoning True (UNVERIFIED_ZONING).
  - Dev/test serves from mock-data fixtures    -> the fixture-backed caller
    returns the seeded records with no live network.

All external callers (per-registry HTTP + GeoNames) are dependency-injected
fakes; no network access occurs.

NOTE: the round-trip serialisation test is task 20.2 and is intentionally NOT
included here.

Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date

import pytest

# The business-registry-adapter service directory contains a hyphen, so it is
# not importable as a package. Load its ``client`` / ``config`` modules under
# collision-free namespaced keys (see tests/conftest.py) so a stale bare
# ``config`` / ``client`` left in sys.modules by another service's test file
# cannot shadow them when the whole unit suite runs together.
from tests.conftest import load_service_module  # noqa: E402

_client_mod = load_service_module(
    "business-registry-adapter", "client", siblings=["config", "client"]
)
_config_mod = load_service_module(
    "business-registry-adapter", "config", siblings=["config"]
)

AddressType = _client_mod.AddressType
CorporateStatus = _client_mod.CorporateStatus
EmployerRegistration = _client_mod.EmployerRegistration
RegistryLookupResult = _client_mod.RegistryLookupResult
RegistryStatus = _client_mod.RegistryStatus
RegistryUnavailableError = _client_mod.RegistryUnavailableError
classify_address = _client_mod.classify_address
make_fixture_registry_caller = _client_mod.make_fixture_registry_caller
normalise_registration = _client_mod.normalise_registration
query_all_registries = _client_mod.query_all_registries

FEDERAL_JURISDICTION = _config_mod.FEDERAL_JURISDICTION
PROVINCIAL_JURISDICTIONS = _config_mod.PROVINCIAL_JURISDICTIONS
get_settings = _config_mod.get_settings

_SHARED_BN = "900000001RT0001"  # present in CA, ON, AB fixtures


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fixture directories (resolved from the repo root, same as the service).
# ---------------------------------------------------------------------------

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_FEDERAL_DIR = os.path.join(_REPO_ROOT, "mock-data", "business-registries")
_PROVINCIAL_DIR = os.path.join(_REPO_ROOT, "mock-data", "provincial-registries")


# ===========================================================================
# In-memory fake callers (dependency-injected; no network)
# ===========================================================================


def make_dict_registry_caller(
    records_by_jurisdiction: dict[str, list[dict]],
    *,
    unreachable: set[str] | None = None,
):
    """Build a registry caller from an in-memory jurisdiction->records map."""
    unreachable = unreachable or set()

    async def _caller(bn: str, jurisdiction: str) -> list[dict]:
        if jurisdiction in unreachable:
            raise RegistryUnavailableError(jurisdiction)
        recs = records_by_jurisdiction.get(jurisdiction, [])
        return [r for r in recs if r.get("bn") == bn]

    return _caller


def make_geonames_resolver(residential: bool | None):
    """Build a GeoNames resolver stub that always returns ``residential``."""

    async def _resolver(postal_code: str) -> bool | None:
        return residential

    return _resolver


def _base_records() -> dict[str, list[dict]]:
    """Seed a record for the shared BN in CA + a couple of provinces."""
    rec = {
        "bn": _SHARED_BN,
        "legal_name": "Test Employer Inc.",
        "registration_date": "2016-01-01",
        "corporate_status": "active",
        "registered_address": "1 Test Street, Toronto, ON",
        "registered_postal_code": "M5J 2J2",
    }
    return {
        FEDERAL_JURISDICTION: [dict(rec, jurisdiction="CA")],
        "ON": [dict(rec, jurisdiction="ON")],
        "AB": [dict(rec, jurisdiction="AB")],
    }


# ===========================================================================
# normalise_registration
# ===========================================================================


class TestNormalisation:
    def test_maps_common_aliases(self):
        raw = {
            "business_number": _SHARED_BN,
            "name": "Alias Co.",
            "status": "in good standing",
            "incorporation_date": "2020-05-05",
            "address": "5 King St",
            "postal_code": "K1A 0A9",
        }
        reg = normalise_registration(raw, "ON")
        assert reg.bn == _SHARED_BN
        assert reg.legal_name == "Alias Co."
        assert reg.jurisdiction == "ON"
        assert reg.corporate_status is CorporateStatus.ACTIVE
        assert reg.registration_date == date(2020, 5, 5)
        assert reg.registered_postal_code == "K1A 0A9"

    def test_unknown_status_and_bad_date(self):
        reg = normalise_registration(
            {"bn": _SHARED_BN, "status": "mystery", "registration_date": "not-a-date"},
            "BC",
        )
        assert reg.corporate_status is CorporateStatus.UNKNOWN
        assert reg.registration_date is None


# ===========================================================================
# All sources OK -> full result
# ===========================================================================


class TestAllSourcesOk:
    def test_status_ok_when_all_reachable(self):
        caller = make_dict_registry_caller(_base_records())
        result = _run(
            query_all_registries(
                _SHARED_BN,
                registry_caller=caller,
                geonames_resolver=make_geonames_resolver(False),
            )
        )
        assert result.status is RegistryStatus.OK
        assert result.unreachable == []

    def test_aggregates_records_from_multiple_jurisdictions(self):
        caller = make_dict_registry_caller(_base_records())
        result = _run(
            query_all_registries(
                _SHARED_BN,
                registry_caller=caller,
                geonames_resolver=make_geonames_resolver(False),
            )
        )
        jurisdictions = {r.jurisdiction for r in result.registrations}
        assert jurisdictions == {"CA", "ON", "AB"}
        assert len(result.registrations) == 3


# ===========================================================================
# One province down -> PARTIAL_RESULT lists it
# ===========================================================================


class TestPartialResult:
    def test_one_province_down_yields_partial_result(self):
        caller = make_dict_registry_caller(_base_records(), unreachable={"ON"})
        result = _run(
            query_all_registries(
                _SHARED_BN,
                registry_caller=caller,
                geonames_resolver=make_geonames_resolver(False),
            )
        )
        assert result.status is RegistryStatus.PARTIAL_RESULT
        assert "ON" in result.unreachable
        # Reachable jurisdictions still contributed their records.
        assert any(r.jurisdiction == "CA" for r in result.registrations)
        assert not any(r.jurisdiction == "ON" for r in result.registrations)

    def test_multiple_provinces_down_all_listed(self):
        caller = make_dict_registry_caller(
            _base_records(), unreachable={"ON", "AB", "QC"}
        )
        result = _run(
            query_all_registries(
                _SHARED_BN,
                registry_caller=caller,
                geonames_resolver=make_geonames_resolver(False),
            )
        )
        assert result.status is RegistryStatus.PARTIAL_RESULT
        assert {"ON", "AB", "QC"}.issubset(set(result.unreachable))


# ===========================================================================
# All down -> SERVICE_UNAVAILABLE
# ===========================================================================


class TestServiceUnavailable:
    def test_all_registries_down_yields_service_unavailable(self):
        all_j = {FEDERAL_JURISDICTION, *PROVINCIAL_JURISDICTIONS}
        caller = make_dict_registry_caller({}, unreachable=all_j)
        result = _run(
            query_all_registries(
                _SHARED_BN,
                registry_caller=caller,
                geonames_resolver=make_geonames_resolver(False),
            )
        )
        assert result.status is RegistryStatus.SERVICE_UNAVAILABLE
        assert result.registrations == []
        assert len(result.unreachable) == 14  # federal + 13 provincial


# ===========================================================================
# Zoning via GeoNames vs FSA fallback (UNVERIFIED_ZONING)
# ===========================================================================


class TestZoningClassification:
    def test_geonames_residential_verified(self):
        result = _run(classify_address("M5J 2J2", make_geonames_resolver(True)))
        assert result.address_type is AddressType.RESIDENTIAL
        assert result.unverified_zoning is False

    def test_geonames_commercial_verified(self):
        result = _run(classify_address("M5J 2J2", make_geonames_resolver(False)))
        assert result.address_type is AddressType.COMMERCIAL
        assert result.unverified_zoning is False

    def test_fallback_when_geonames_returns_none(self):
        # M4E is in RESIDENTIAL_FSA_RANGES -> residential + UNVERIFIED_ZONING.
        result = _run(classify_address("M4E 1B1", make_geonames_resolver(None)))
        assert result.address_type is AddressType.RESIDENTIAL
        assert result.unverified_zoning is True

    def test_fallback_when_geonames_missing(self):
        result = _run(classify_address("M4E 1B1", None))
        assert result.address_type is AddressType.RESIDENTIAL
        assert result.unverified_zoning is True

    def test_fallback_commercial_for_non_residential_fsa(self):
        # A known-format FSA that isn't in the residential set -> commercial,
        # still flagged unverified because GeoNames could not confirm it.
        result = _run(classify_address("H3C 1A3", None))
        assert result.address_type is AddressType.COMMERCIAL
        assert result.unverified_zoning is True

    def test_fallback_when_geonames_raises(self):
        async def _boom(postal_code: str):
            raise RuntimeError("geonames down")

        result = _run(classify_address("M4E 1B1", _boom))
        assert result.address_type is AddressType.RESIDENTIAL
        assert result.unverified_zoning is True

    def test_lookup_flags_unverified_zoning_end_to_end(self):
        # Shell employer with a residential FSA, GeoNames unavailable.
        records = {
            FEDERAL_JURISDICTION: [
                {
                    "bn": "900000002RT0001",
                    "legal_name": "Shell Co.",
                    "registration_date": "2024-02-15",
                    "corporate_status": "active",
                    "registered_address": "88 Maple Street",
                    "registered_postal_code": "M4E 1B1",
                }
            ]
        }
        caller = make_dict_registry_caller(records)
        result = _run(
            query_all_registries(
                "900000002RT0001",
                registry_caller=caller,
                geonames_resolver=None,  # GeoNames unavailable -> fallback
            )
        )
        assert result.status is RegistryStatus.OK
        assert len(result.registrations) == 1
        reg = result.registrations[0]
        assert reg.address_type is AddressType.RESIDENTIAL
        assert reg.unverified_zoning is True


# ===========================================================================
# Serve-from-fixtures in dev
# ===========================================================================


class TestFixtureBackedCaller:
    def test_fixture_caller_returns_seeded_records(self):
        caller = make_fixture_registry_caller(_FEDERAL_DIR, _PROVINCIAL_DIR)
        result = _run(
            query_all_registries(
                _SHARED_BN,
                registry_caller=caller,
                geonames_resolver=None,
            )
        )
        # Fixtures seed the shared BN in CA + ON + AB.
        assert result.status is RegistryStatus.OK
        jurisdictions = {r.jurisdiction for r in result.registrations}
        assert {"CA", "ON", "AB"}.issubset(jurisdictions)

    def test_fixture_caller_forced_unreachable_partial(self):
        caller = make_fixture_registry_caller(
            _FEDERAL_DIR, _PROVINCIAL_DIR, unreachable_jurisdictions={"ON"}
        )
        result = _run(
            query_all_registries(
                _SHARED_BN,
                registry_caller=caller,
                geonames_resolver=None,
            )
        )
        assert result.status is RegistryStatus.PARTIAL_RESULT
        assert "ON" in result.unreachable

    def test_settings_point_at_mock_dirs_in_dev(self):
        settings = get_settings()
        assert settings.is_development is True
        assert "business-registries" in settings.business_registries_dir
        assert "provincial-registries" in settings.provincial_registries_dir


# ===========================================================================
# Endpoint smoke tests (skipped if FastAPI TestClient unavailable)
# ===========================================================================

try:
    from fastapi.testclient import TestClient as _TestClient

    _TESTCLIENT_AVAILABLE = True
    _SKIP_REASON = ""
except Exception as _exc:  # noqa: BLE001
    _TESTCLIENT_AVAILABLE = False
    _SKIP_REASON = f"FastAPI TestClient unavailable: {_exc}"


@pytest.mark.skipif(not _TESTCLIENT_AVAILABLE, reason=_SKIP_REASON)
class TestEndpoint:
    def _main(self):
        return load_service_module(
            "business-registry-adapter", "main", siblings=["config", "client"]
        )

    def _client(self):
        main = self._main()

        # Reset wiring so each test controls the injected callers.
        main.app.state.registry_caller = None
        if hasattr(main.app.state, "geonames_resolver"):
            del main.app.state.geonames_resolver

        # Task 29.1: every functional route now enforces the INVESTIGATOR role
        # via the shared GCKey dependency. Inject an authenticated principal so
        # these endpoint tests exercise the handler rather than the auth gate;
        # the 401/403 boundary itself is covered in test_auth_service_wiring.py.
        from shared.auth import get_current_user  # noqa: PLC0415

        main.app.dependency_overrides[get_current_user] = lambda: {
            "roles": ["INVESTIGATOR"]
        }
        return _TestClient(main.app), main

    def teardown_method(self):
        try:
            self._main().app.dependency_overrides.clear()
        except Exception:  # noqa: BLE001
            pass

    def test_healthz_ok(self):
        client, _ = self._client()
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_employer_lookup_ok_from_fixtures(self):
        client, main = self._client()
        # Ensure fixture-backed wiring (dev default) is used.
        resp = client.get(f"/registry/employer/{_SHARED_BN}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "OK"
        assert body["bn"] == _SHARED_BN
        assert len(body["registrations"]) >= 3

    def test_employer_lookup_service_unavailable_returns_503(self):
        client, main = self._client()
        all_j = {FEDERAL_JURISDICTION, *PROVINCIAL_JURISDICTIONS}
        main.app.state.registry_caller = make_dict_registry_caller(
            {}, unreachable=all_j
        )
        main.app.state.geonames_resolver = None
        resp = client.get(f"/registry/employer/{_SHARED_BN}")
        assert resp.status_code == 503
        assert resp.json()["status"] == "SERVICE_UNAVAILABLE"

    def test_employer_lookup_partial_result(self):
        client, main = self._client()
        main.app.state.registry_caller = make_dict_registry_caller(
            _base_records(), unreachable={"ON"}
        )
        main.app.state.geonames_resolver = make_geonames_resolver(False)
        resp = client.get(f"/registry/employer/{_SHARED_BN}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "PARTIAL_RESULT"
        assert "ON" in body["unreachable"]
