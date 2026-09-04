"""
tests/unit/test_job_bank_adapter.py
===================================
Unit tests for the Job Bank Adapter (task 24.1).

Coverage:
  * ``serialise_job_bank_data`` / ``parse_job_bank_data`` round-trip preserves
    posting counts and the median wage exactly, with the Decimal wage carried as
    a string (Requirements 16.5 groundwork; P12 property proper is task 24.2).
  * :class:`JobBankCache` hit / miss against an in-memory fake cache
    (Requirement 16.2).
  * API-unavailable -> snapshot fallback flagged ``DATA_STALE`` (Requirement
    16.3); missing/stale snapshot -> ``DATA_UNAVAILABLE``.
  * Dev/test always serves from the open-data snapshot (Requirement 16.4).
  * The endpoint returns canonical JSON (median wage as a string).

Isolation note: the service directory is hyphenated
(``services/job-bank-adapter``) and its modules import each other by BARE name
(``from serialization import ...``). Loading them under the bare names would
collide with the ``config`` / ``client`` / ``main`` modules of the other
adapters across the suite. To stay isolated we load each module under a UNIQUE
namespaced key (``jobbank_serialization``, ``jobbank_client``, ...) while
registering the bare name only transiently so intra-package imports resolve,
then restore the pre-existing bare-name modules. This mirrors
``tests/unit/test_cbsa_adapter.py``.

Requirements: 16.1, 16.2, 16.3, 16.4, 16.5
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from decimal import Decimal
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Load the hyphenated job-bank-adapter service modules.
# ---------------------------------------------------------------------------

_SERVICE_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "services", "job-bank-adapter"
    )
)
_SNAPSHOT_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "mock-data", "job-bank")
)

_BARE_NAMES = ("serialization", "config", "client", "main")
_SAVED = {name: sys.modules.get(name) for name in _BARE_NAMES}


def _load_service_modules() -> dict[str, Any]:
    if os.path.isdir(_SERVICE_DIR) and _SERVICE_DIR not in sys.path:
        sys.path.insert(0, _SERVICE_DIR)

    loaded: dict[str, Any] = {}
    for bare, filename in (
        ("serialization", "serialization.py"),
        ("config", "config.py"),
        ("client", "client.py"),
        ("main", "main.py"),
    ):
        spec = importlib.util.spec_from_file_location(
            bare, os.path.join(_SERVICE_DIR, filename)
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        # Register under the bare name so sibling imports resolve, and under a
        # namespaced key so tests can reference it without collisions.
        sys.modules[bare] = module
        sys.modules[f"jobbank_{bare}"] = module
        spec.loader.exec_module(module)
        loaded[bare] = module

    # Restore pre-existing bare-name modules so other adapters' tests are safe.
    for name, saved in _SAVED.items():
        if saved is not None:
            sys.modules[name] = saved
        else:
            sys.modules.pop(name, None)

    return loaded


_loaded = _load_service_modules()
_serialization = _loaded["serialization"]
_config = _loaded["config"]
_client = _loaded["client"]
_main = _loaded["main"]

# Task 29.1: protected routes require the INVESTIGATOR role; inject a principal.
from shared.auth import get_current_user as _get_current_user  # noqa: E402

_main.app.dependency_overrides[_get_current_user] = lambda: {"roles": ["INVESTIGATOR"]}

JobBankData = _serialization.JobBankData
serialise_job_bank_data = _serialization.serialise_job_bank_data
parse_job_bank_data = _serialization.parse_job_bank_data

JobBankCache = _client.JobBankCache
JobBankFetcher = _client.JobBankFetcher
JobBankSnapshotStore = _client.JobBankSnapshotStore
JobBankUnavailableError = _client.JobBankUnavailableError
SnapshotStaleError = _client.SnapshotStaleError
SnapshotMissingError = _client.SnapshotMissingError

try:
    from fastapi.testclient import TestClient

    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001
    _HAS_FASTAPI = False


# ---------------------------------------------------------------------------
# In-memory fake cache (satisfies the AsyncCache protocol)
# ---------------------------------------------------------------------------


class FakeCache:
    """Minimal in-memory async cache used to exercise JobBankCache."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.gets = 0
        self.sets = 0

    async def get(self, key: str) -> str | None:
        self.gets += 1
        return self.store.get(key)

    async def set_with_ttl(self, key: str, value: str, ttl_seconds: int) -> None:
        self.sets += 1
        self.store[key] = value


def _run(coro):
    return asyncio.run(coro)


# ===========================================================================
# Serialise / parse round-trip (16.5 groundwork)
# ===========================================================================


class TestSerialisationRoundTrip:
    def test_round_trip_preserves_count_and_wage(self):
        data = JobBankData(
            noc_code="21234",
            region_code="35",
            active_posting_count=187,
            median_hourly_wage=Decimal("42.7550"),
        )
        restored = parse_job_bank_data(serialise_job_bank_data(data))
        assert restored.active_posting_count == 187
        assert restored.median_hourly_wage == Decimal("42.7550")
        assert restored.noc_code == "21234"
        assert restored.region_code == "35"

    def test_median_wage_serialised_as_string(self):
        data = JobBankData("21234", "35", 10, Decimal("19.99"))
        payload = serialise_job_bank_data(data)
        assert isinstance(payload["median_hourly_wage"], str)
        assert payload["median_hourly_wage"] == "19.99"
        assert isinstance(payload["active_posting_count"], int)

    def test_high_precision_wage_survives_round_trip(self):
        """A wage with many decimals must not be corrupted by float."""
        wage = Decimal("33.123456789")
        data = JobBankData("00000", "10", 1, wage)
        restored = parse_job_bank_data(serialise_job_bank_data(data))
        assert restored.median_hourly_wage == wage

    def test_trailing_zeros_preserved(self):
        wage = Decimal("24.1000")
        data = JobBankData("63201", "59", 245, wage)
        restored = parse_job_bank_data(serialise_job_bank_data(data))
        # str(Decimal) keeps the exact digits, so trailing zeros survive.
        assert str(restored.median_hourly_wage) == "24.1000"

    def test_no_extra_fields_when_optional_absent(self):
        data = JobBankData("21234", "35", 5, Decimal("20.00"))
        payload = serialise_job_bank_data(data)
        assert set(payload.keys()) == {
            "noc_code",
            "region_code",
            "active_posting_count",
            "median_hourly_wage",
            "data_stale",
        }


# ===========================================================================
# JobBankCache hit / miss (16.2)
# ===========================================================================


class TestJobBankCache:
    def test_miss_returns_none(self):
        cache = JobBankCache(FakeCache())
        result = _run(cache.read("21234", "35", "2024-05"))
        assert result is None

    def test_write_then_hit_round_trips_via_cache(self):
        fake = FakeCache()
        cache = JobBankCache(fake)
        data = JobBankData("21234", "35", 187, Decimal("42.7550"))
        _run(cache.write("2024-05", data))
        # Key uses the shared builder pattern.
        assert "job_bank:postings:21234:35:2024-05" in fake.store
        hit = _run(cache.read("21234", "35", "2024-05"))
        assert hit is not None
        assert hit.active_posting_count == 187
        assert hit.median_hourly_wage == Decimal("42.7550")

    def test_none_cache_is_noop(self):
        cache = JobBankCache(None)
        _run(cache.write("2024-05", JobBankData("1", "2", 3, Decimal("4.00"))))
        assert _run(cache.read("1", "2", "2024-05")) is None


# ===========================================================================
# Snapshot store (16.3, 16.4)
# ===========================================================================


class TestSnapshotStore:
    def _store(self, max_age_days: int = 7) -> "JobBankSnapshotStore":
        return JobBankSnapshotStore(_SNAPSHOT_DIR, max_age_days=max_age_days)

    def test_loads_known_combo(self):
        data = self._store().load("21234", "35")
        assert data.active_posting_count == 187
        assert data.median_hourly_wage == Decimal("42.7550")
        assert data.data_stale is True  # snapshot-served marked stale by default

    def test_missing_combo_raises(self):
        with pytest.raises(SnapshotMissingError):
            self._store().load("99999", "01")

    def test_stale_snapshot_raises(self):
        # The 10010:99 fixture carries an old snapshot_date (> 7 days).
        with pytest.raises(SnapshotStaleError):
            self._store().load("10010", "99")

    def test_data_stale_flag_can_be_overridden(self):
        data = self._store().load("63201", "59", data_stale=False)
        assert data.data_stale is False


# ===========================================================================
# Fetcher: API-unavailable path (16.1, 16.3)
# ===========================================================================


class TestFetcher:
    def test_fetch_parses_ok_response(self):
        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "noc_code": "21234",
                    "region_code": "35",
                    "active_posting_count": 99,
                    "median_hourly_wage": "40.25",
                }

        class _Client:
            async def get(self, *a, **k):
                return _Resp()

        fetcher = JobBankFetcher(base_url="http://x", client=_Client())
        data = _run(fetcher.fetch("21234", "35"))
        assert data.active_posting_count == 99
        assert data.median_hourly_wage == Decimal("40.25")
        assert data.data_stale is False

    def test_fetch_raises_unavailable_on_5xx(self):
        class _Resp:
            status_code = 503

            @staticmethod
            def json():
                return {}

        class _Client:
            async def get(self, *a, **k):
                return _Resp()

        fetcher = JobBankFetcher(base_url="http://x", client=_Client())
        with pytest.raises(JobBankUnavailableError):
            _run(fetcher.fetch("21234", "35"))

    def test_fetch_raises_unavailable_on_transport_error(self):
        class _Client:
            async def get(self, *a, **k):
                raise RuntimeError("connection reset")

        fetcher = JobBankFetcher(base_url="http://x", client=_Client())
        with pytest.raises(JobBankUnavailableError):
            _run(fetcher.fetch("21234", "35"))


# ===========================================================================
# Endpoint tests (16.1, 16.3, 16.4)
# ===========================================================================


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not importable")
class TestPostingsEndpoint:
    def _dev_client(self):
        """Endpoint in dev/test mode: always serves from the snapshot."""

        class _DevSettings:
            job_bank_snapshot_dir = _SNAPSHOT_DIR
            job_bank_snapshot_max_age_days = 7
            job_bank_api_base_url = "http://unused"
            job_bank_api_key = ""
            job_bank_timeout = 5.0
            serve_from_snapshot = True

        _main.app.state.settings = _DevSettings()
        _main.app.state.snapshot = JobBankSnapshotStore(
            _SNAPSHOT_DIR, max_age_days=7
        )
        _main.app.state.cache = JobBankCache(None)
        _main.app.state.raw_cache = None
        _main.app.state.fetcher = JobBankFetcher(base_url="http://unused")
        return TestClient(_main.app)

    def test_dev_serves_from_snapshot_canonical_json(self):
        resp = self._dev_client().get("/jobbank/postings/21234/35/2024-05")
        assert resp.status_code == 200
        body = resp.json()
        assert body["flag"] == "OK"
        assert body["noc_code"] == "21234"
        assert body["active_posting_count"] == 187
        # Median wage is a string in the canonical JSON (exact Decimal).
        assert body["median_hourly_wage"] == "42.7550"
        assert isinstance(body["median_hourly_wage"], str)

    def test_dev_unknown_combo_returns_unavailable(self):
        resp = self._dev_client().get("/jobbank/postings/99999/01/2024-05")
        assert resp.status_code == 503
        assert resp.json()["flag"] == "DATA_UNAVAILABLE"

    def test_invalid_year_month_rejected(self):
        resp = self._dev_client().get("/jobbank/postings/21234/35/2024-13")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_YEAR_MONTH"

    def test_healthz_ok_in_snapshot_mode(self):
        resp = self._dev_client().get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["checks"]["snapshot_mode"] is True

    def _prod_client(self, fetcher):
        """Endpoint in production mode with an injected fetcher + fake cache."""

        class _ProdSettings:
            job_bank_snapshot_dir = _SNAPSHOT_DIR
            job_bank_snapshot_max_age_days = 7
            job_bank_api_base_url = "http://unused"
            job_bank_api_key = ""
            job_bank_timeout = 5.0
            serve_from_snapshot = False

        _main.app.state.settings = _ProdSettings()
        _main.app.state.snapshot = JobBankSnapshotStore(
            _SNAPSHOT_DIR, max_age_days=7
        )
        _main.app.state.cache = JobBankCache(FakeCache())
        _main.app.state.raw_cache = None
        _main.app.state.fetcher = fetcher
        return TestClient(_main.app)

    def test_prod_api_unavailable_falls_back_to_snapshot_stale(self):
        class _FailingFetcher:
            async def fetch(self, *a, **k):
                raise JobBankUnavailableError("down")

        resp = self._prod_client(_FailingFetcher()).get(
            "/jobbank/postings/21234/35/2024-05"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["flag"] == "DATA_STALE"
        assert body["active_posting_count"] == 187

    def test_prod_api_unavailable_and_no_snapshot_returns_unavailable(self):
        class _FailingFetcher:
            async def fetch(self, *a, **k):
                raise JobBankUnavailableError("down")

        resp = self._prod_client(_FailingFetcher()).get(
            "/jobbank/postings/99999/01/2024-05"
        )
        assert resp.status_code == 503
        assert resp.json()["flag"] == "DATA_UNAVAILABLE"

    def test_prod_api_ok_returns_fresh_and_caches(self):
        class _OkFetcher:
            async def fetch(self, noc, region, **k):
                return JobBankData(noc, region, 500, Decimal("55.55"))

        client = self._prod_client(_OkFetcher())
        resp = client.get("/jobbank/postings/21234/35/2024-05")
        assert resp.status_code == 200
        body = resp.json()
        assert body["flag"] == "OK"
        assert body["active_posting_count"] == 500
        assert body["median_hourly_wage"] == "55.55"
        # The result was written to the cache.
        fake = _main.app.state.cache._cache  # noqa: SLF001 - test introspection
        assert "job_bank:postings:21234:35:2024-05" in fake.store
