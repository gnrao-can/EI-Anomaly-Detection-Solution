"""
tests/unit/test_fraud_ring_engine.py
=====================================
Unit tests for the Fraud Ring Engine (task 13.1).

Coverage:
  * Shell-employer detection (Requirement 5.2):
      - registered 11 months before separation + residential  -> CRITICAL
      - registered 13 months before separation + residential  -> no shell flag
      - registered 11 months before separation + commercial   -> no shell flag
      - no registry record                                     -> HIGH
      - registered AFTER the separation date + residential     -> no shell flag
  * Coordinated-layoff detection (Requirements 5.3, 5.4):
      - 79% share the separation date                          -> no flag
      - 80% share the date + all ROEs within 24h               -> CRITICAL
      - 80% share the date but ROEs spread > 24h               -> no flag
      - empty cohort                                           -> no flag
  * ``/engine/analyze`` endpoint with injected fakes (no live network):
      - registry client + cohort source wired via DI
      - registry unavailable -> shell check skipped (no crash)

The service directory is hyphenated (``services/fraud-ring-engine``) and its
modules import each other by bare name, so we load them under unique namespaced
keys (``fraudring_*``) and restore any pre-existing bare-name modules afterwards,
mirroring ``tests/unit/test_repetitive_pattern_engine.py``.

Requirements: 5.2, 5.3, 5.4
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from shared.models import EngineType, Severity

# ---------------------------------------------------------------------------
# Load the hyphenated fraud-ring-engine service modules.
# ---------------------------------------------------------------------------

_SERVICE_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "services",
        "fraud-ring-engine",
    )
)

_BARE_NAMES = ("config", "detection", "client", "main")
_SAVED = {name: sys.modules.get(name) for name in _BARE_NAMES}


def _load_service_modules() -> dict[str, Any]:
    if os.path.isdir(_SERVICE_DIR) and _SERVICE_DIR not in sys.path:
        sys.path.insert(0, _SERVICE_DIR)

    loaded: dict[str, Any] = {}
    for bare in _BARE_NAMES:
        filename = os.path.join(_SERVICE_DIR, f"{bare}.py")
        spec = importlib.util.spec_from_file_location(bare, filename)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        # Register under bare name (so sibling ``from X import Y`` works) and a
        # namespaced key (stable reference for the tests).
        sys.modules[bare] = module
        sys.modules[f"fraudring_{bare}"] = module
        spec.loader.exec_module(module)
        loaded[bare] = module

    # Restore pre-existing bare-name modules so sibling suites are unaffected.
    for name, saved in _SAVED.items():
        if saved is not None:
            sys.modules[name] = saved
        else:
            sys.modules.pop(name, None)

    return loaded


_loaded = _load_service_modules()
_config = _loaded["config"]
_detection = _loaded["detection"]
_client = _loaded["client"]
_main = _loaded["main"]

CohortMember = _detection.CohortMember
check_shell_employer = _detection.check_shell_employer
check_coordinated_layoff = _detection.check_coordinated_layoff

BusinessRegistryAdapterClient = _client.BusinessRegistryAdapterClient
FakeCohortSource = _client.FakeCohortSource
RegistryLookupStatus = _client.RegistryLookupStatus

try:
    from fastapi.testclient import TestClient

    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001
    _HAS_FASTAPI = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEPARATION = date(2024, 3, 1)


def _registration(
    *, months_before: float, residential: bool
) -> dict[str, Any]:
    """Build an EmployerRegistration-shaped dict registered N months before sep."""
    reg_date = _SEPARATION - timedelta(days=round(months_before * 30.4375))
    return {
        "bn": "123456789RT0001",
        "legal_name": "Shelltest Inc.",
        "jurisdiction": "CA",
        "registration_date": reg_date.isoformat(),
        "corporate_status": "ACTIVE",
        "registered_address": "12 Maple Lane",
        "registered_postal_code": "K1S",
        "address_type": "RESIDENTIAL" if residential else "COMMERCIAL",
        "unverified_zoning": False,
    }


def _member(sin: str, sep_iso: str, roe_ts: datetime) -> CohortMember:
    return CohortMember(
        sin_hash=sin,
        separation_date=date.fromisoformat(sep_iso),
        roe_submission_ts=roe_ts,
    )


# ===========================================================================
# Shell-employer detection (Req 5.2)
# ===========================================================================


class TestShellEmployer:
    def test_registered_11_months_before_residential_is_critical(self):
        reg = _registration(months_before=11, residential=True)
        indicators = check_shell_employer(reg, _SEPARATION, employer_bn="BN1")
        assert len(indicators) == 1
        ind = indicators[0]
        assert ind.engine == EngineType.FRAUD_RING
        assert ind.severity == Severity.CRITICAL
        assert ind.algorithm == "shell_employer_recent_residential"
        assert ind.payload["reason"] == "RECENT_REGISTRATION_RESIDENTIAL"
        assert ind.payload["address_type"] == "RESIDENTIAL"

    def test_registered_13_months_before_residential_no_flag(self):
        reg = _registration(months_before=13, residential=True)
        assert check_shell_employer(reg, _SEPARATION) == []

    def test_registered_11_months_before_commercial_no_flag(self):
        reg = _registration(months_before=11, residential=False)
        assert check_shell_employer(reg, _SEPARATION) == []

    def test_no_registry_record_is_high(self):
        indicators = check_shell_employer(None, _SEPARATION, employer_bn="BN9")
        assert len(indicators) == 1
        ind = indicators[0]
        assert ind.engine == EngineType.FRAUD_RING
        assert ind.severity == Severity.HIGH
        assert ind.algorithm == "shell_employer_no_record"
        assert ind.payload["reason"] == "NO_REGISTRY_RECORD"
        assert ind.payload["employer_bn"] == "BN9"

    def test_registered_after_separation_no_flag(self):
        """A registration dated AFTER separation is not "within N months before"."""
        reg = dict(_registration(months_before=1, residential=True))
        reg["registration_date"] = (_SEPARATION + timedelta(days=40)).isoformat()
        assert check_shell_employer(reg, _SEPARATION) == []

    def test_exact_12_month_boundary_residential_is_critical(self):
        """Exactly 12 months before (inclusive boundary) + residential -> CRITICAL."""
        reg = _registration(months_before=12, residential=True)
        indicators = check_shell_employer(reg, _SEPARATION)
        assert len(indicators) == 1
        assert indicators[0].severity == Severity.CRITICAL


# ===========================================================================
# Coordinated-layoff detection (Req 5.3, 5.4)
# ===========================================================================


class TestCoordinatedLayoff:
    def _cohort_same_date(
        self, same_count: int, other_count: int, *, roe_within_24h: bool
    ) -> list[CohortMember]:
        """Build a cohort: ``same_count`` share a date, ``other_count`` differ."""
        base_ts = datetime(2024, 3, 1, 9, 0, tzinfo=timezone.utc)
        members: list[CohortMember] = []
        for i in range(same_count):
            # Spread ROE timestamps either inside or outside 24h.
            offset = timedelta(hours=i * (2 if roe_within_24h else 12))
            members.append(_member(f"same-{i}", "2024-03-01", base_ts + offset))
        for j in range(other_count):
            members.append(
                _member(
                    f"other-{j}",
                    (date(2024, 2, 1) + timedelta(days=j)).isoformat(),
                    base_ts + timedelta(days=5 + j),
                )
            )
        return members

    def test_79_percent_same_date_no_flag(self):
        """79% share the separation date -> below the 80% threshold, no flag."""
        # 79/100 share the date.
        cohort = self._cohort_same_date(79, 21, roe_within_24h=True)
        assert check_coordinated_layoff(cohort) == []

    def test_80_percent_same_date_roes_within_24h_is_critical(self):
        """80% share the date AND all ROEs within 24h -> CRITICAL."""
        # 8/10 share the date; their ROE timestamps span 2h*7 = 14h < 24h.
        cohort = self._cohort_same_date(8, 2, roe_within_24h=True)
        indicators = check_coordinated_layoff(cohort)
        assert len(indicators) == 1
        ind = indicators[0]
        assert ind.engine == EngineType.FRAUD_RING
        assert ind.severity == Severity.CRITICAL
        assert ind.algorithm == "coordinated_layoff"
        assert ind.payload["same_date_count"] == 8
        assert ind.payload["cohort_size"] == 10
        assert ind.payload["shared_separation_date"] == "2024-03-01"
        assert ind.payload["same_date_fraction"] == pytest.approx(0.80)

    def test_80_percent_same_date_roes_spread_over_24h_no_flag(self):
        """80% share the date but ROEs spread > 24h -> no coordinated flag."""
        # 8/10 share the date; ROE timestamps span 12h*7 = 84h > 24h.
        cohort = self._cohort_same_date(8, 2, roe_within_24h=False)
        assert check_coordinated_layoff(cohort) == []

    def test_empty_cohort_no_flag(self):
        assert check_coordinated_layoff([]) == []

    def test_all_same_date_within_24h_is_critical(self):
        """A fully coordinated cohort (100% same date, ROEs within 24h) -> CRITICAL."""
        cohort = self._cohort_same_date(5, 0, roe_within_24h=True)
        indicators = check_coordinated_layoff(cohort)
        assert len(indicators) == 1
        assert indicators[0].severity == Severity.CRITICAL
        assert indicators[0].payload["same_date_fraction"] == pytest.approx(1.0)


# ===========================================================================
# Registry client body interpretation (DI, no network)
# ===========================================================================


class _FakeResponse:
    def __init__(self, body: Any, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code

    def json(self) -> Any:
        return self._body


class _FakeHttp:
    """Minimal async httpx-like client returning a pre-set response, or raising."""

    def __init__(self, response: Any = None, *, raise_exc: bool = False) -> None:
        self._response = response
        self._raise = raise_exc

    async def get(self, url: str, *args: Any, **kwargs: Any) -> Any:
        if self._raise:
            raise RuntimeError("connection refused")
        return self._response


class TestBusinessRegistryClient:
    def test_found_selects_federal_registration(self):
        body = {
            "bn": "BN1",
            "status": "OK",
            "registrations": [
                {"jurisdiction": "ON", "registration_date": "2020-01-01"},
                {"jurisdiction": "CA", "registration_date": "2023-06-01"},
            ],
            "unreachable": [],
        }
        http = _FakeHttp(_FakeResponse(body))
        client = BusinessRegistryAdapterClient("http://reg", http)
        lookup = _run(client.lookup_employer("BN1"))
        assert lookup.status is RegistryLookupStatus.FOUND
        assert lookup.registration["jurisdiction"] == "CA"

    def test_no_registrations_is_no_record(self):
        body = {"bn": "BN1", "status": "OK", "registrations": [], "unreachable": []}
        client = BusinessRegistryAdapterClient("http://reg", _FakeHttp(_FakeResponse(body)))
        lookup = _run(client.lookup_employer("BN1"))
        assert lookup.status is RegistryLookupStatus.NO_RECORD

    def test_service_unavailable_body_is_unavailable(self):
        body = {"bn": "BN1", "status": "SERVICE_UNAVAILABLE", "registrations": []}
        client = BusinessRegistryAdapterClient("http://reg", _FakeHttp(_FakeResponse(body)))
        lookup = _run(client.lookup_employer("BN1"))
        assert lookup.status is RegistryLookupStatus.UNAVAILABLE

    def test_transport_failure_is_unavailable(self):
        client = BusinessRegistryAdapterClient("http://reg", _FakeHttp(raise_exc=True))
        lookup = _run(client.lookup_employer("BN1"))
        assert lookup.status is RegistryLookupStatus.UNAVAILABLE

    def test_5xx_is_unavailable(self):
        client = BusinessRegistryAdapterClient(
            "http://reg", _FakeHttp(_FakeResponse({}, status_code=503))
        )
        lookup = _run(client.lookup_employer("BN1"))
        assert lookup.status is RegistryLookupStatus.UNAVAILABLE


# ===========================================================================
# /engine/analyze endpoint (DI fakes, no live network)
# ===========================================================================


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not importable")
class TestAnalyzeEndpoint:
    def _client(self, *, registry_client=None, cohort_source=None) -> "TestClient":
        _main.app.state.settings = _main.get_settings()
        _main.app.state.http_client = None
        _main.app.state.registry_client = registry_client
        _main.app.state.cohort_source = cohort_source
        # Task 29.1: /engine/analyze now enforces the INVESTIGATOR role via the
        # shared GCKey dependency. Inject an authenticated principal so these
        # tests exercise the handler; the 401/403 boundary is covered in
        # tests/unit/test_auth_service_wiring.py.
        from shared.auth import get_current_user  # noqa: PLC0415

        _main.app.dependency_overrides[get_current_user] = lambda: {
            "roles": ["INVESTIGATOR"]
        }
        return TestClient(_main.app)

    def teardown_method(self):
        _main.app.dependency_overrides.clear()

    def test_inline_cohort_and_shell_employer_produce_indicators(self):
        reg_body = {
            "bn": "BN1",
            "status": "OK",
            "registrations": [_registration(months_before=6, residential=True)],
            "unreachable": [],
        }
        registry_client = BusinessRegistryAdapterClient(
            "http://reg", _FakeHttp(_FakeResponse(reg_body))
        )
        client = self._client(registry_client=registry_client)

        base_ts = datetime(2024, 3, 1, 9, 0, tzinfo=timezone.utc)
        cohort = [
            {
                "sin_hash": f"s{i}",
                "separation_date": "2024-03-01",
                "roe_submission_ts": (base_ts + timedelta(hours=i)).isoformat(),
            }
            for i in range(8)
        ] + [
            {
                "sin_hash": "sA",
                "separation_date": "2024-02-10",
                "roe_submission_ts": base_ts.isoformat(),
            },
            {
                "sin_hash": "sB",
                "separation_date": "2024-02-15",
                "roe_submission_ts": base_ts.isoformat(),
            },
        ]

        resp = client.post(
            "/engine/analyze",
            json={
                "employer_bn": "BN1",
                "separation_date": "2024-03-01",
                "cohort": cohort,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        algorithms = {i["algorithm"] for i in body["indicators"]}
        assert "shell_employer_recent_residential" in algorithms
        assert "coordinated_layoff" in algorithms
        assert "shell_employer" in body["checks_run"]
        assert "coordinated_layoff" in body["checks_run"]
        assert body["cohort_size"] == 10

    def test_registry_unavailable_skips_shell_check_no_crash(self):
        registry_client = BusinessRegistryAdapterClient(
            "http://reg", _FakeHttp(raise_exc=True)
        )
        client = self._client(registry_client=registry_client)
        resp = client.post(
            "/engine/analyze",
            json={
                "employer_bn": "BN1",
                "separation_date": "2024-03-01",
                "cohort": [],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert any(
            s.startswith("shell_employer:REGISTRY_UNAVAILABLE")
            for s in body["checks_skipped"]
        )
        assert body["indicators"] == []

    def test_cohort_source_loaded_when_no_inline_cohort(self):
        source = FakeCohortSource()
        base_ts = datetime(2024, 3, 1, 9, 0, tzinfo=timezone.utc)
        source.add_many(
            "BN1",
            [_member(f"s{i}", "2024-03-01", base_ts + timedelta(hours=i)) for i in range(8)]
            + [
                _member("sA", "2024-02-10", base_ts),
                _member("sB", "2024-02-15", base_ts),
            ],
        )
        client = self._client(cohort_source=source)
        resp = client.post(
            "/engine/analyze",
            json={"employer_bn": "BN1", "separation_date": "2024-03-01"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["cohort_size"] == 10
        assert any(
            i["algorithm"] == "coordinated_layoff" for i in body["indicators"]
        )

    def test_cohort_source_failure_skips_coordinated_check(self):
        source = FakeCohortSource()
        source.fail_next()
        client = self._client(cohort_source=source)
        resp = client.post(
            "/engine/analyze",
            json={"employer_bn": "BN1", "separation_date": "2024-03-01"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert any(
            s.startswith("coordinated_layoff:COHORT_UNAVAILABLE")
            for s in body["checks_skipped"]
        )

    def test_healthz_ok(self):
        client = self._client()
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# asyncio runner helper
# ---------------------------------------------------------------------------


def _run(coro):
    import asyncio

    return asyncio.run(coro)
