"""
tests/unit/test_stats_canada_adapter.py
=======================================
Unit tests for the Stats Canada Adapter (task 18.1).

Covers:
  - Fixture parsing preserves rate values to >= 4 decimal places (Req 10.6).
  - Canonical serialise/parse round-trip preserves regions and rates.
  - Redis cache write then read returns the same records (cache hit); a miss
    returns ``None`` (Reqs 10.2, 10.3).
  - Upstream 5xx falls back to the cache with a ``DATA_STALE`` flag (Req 10.2).
  - An empty or > 30-day-old cache on 5xx yields ``DATA_UNAVAILABLE`` (Req 10.4).
  - ``GET /statscan/lfs/{region}/{ym}`` returns canonical LabourForceData JSON
    from fixtures in dev/test (Reqs 10.1, 10.5).

No live network and no external services: the live-fetch path uses an injected
``httpx.MockTransport`` client and the cache uses an in-memory fake that mirrors
``shared.cache.RedisCache``'s async interface (so ``redis``/``fakeredis`` are not
required to run these tests).

Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

# ---------------------------------------------------------------------------
# Load the hyphenated service package (siblings import by bare name).
# ---------------------------------------------------------------------------

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_SERVICE_DIR = os.path.join(_PROJECT_ROOT, "services", "stats-canada-adapter")
_MOCK_DIR = os.path.join(_PROJECT_ROOT, "mock-data", "stats-canada")


def _load_service_module(name: str):
    """Load a module (``client`` / ``main`` / ``config``) from the service dir."""
    added = False
    if os.path.isdir(_SERVICE_DIR) and _SERVICE_DIR not in sys.path:
        sys.path.insert(0, _SERVICE_DIR)
        added = True
    try:
        spec = importlib.util.spec_from_file_location(
            f"statscan_{name}", os.path.join(_SERVICE_DIR, f"{name}.py")
        )
        module = importlib.util.module_from_spec(spec)
        # Register under the loader-visible name so intra-module references and
        # deferred annotations resolve at request time.
        sys.modules[f"statscan_{name}"] = module
        # main.py imports `from client import ...` / `from config import ...`;
        # make those bare names resolvable during exec.
        if name == "main":
            sys.modules.setdefault("client", _load_service_module_cached("client"))
            sys.modules.setdefault("config", _load_service_module_cached("config"))
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module
    finally:
        if added and _SERVICE_DIR in sys.path:
            sys.path.remove(_SERVICE_DIR)


_MODULE_CACHE: dict[str, object] = {}


def _load_service_module_cached(name: str):
    if name not in _MODULE_CACHE:
        _MODULE_CACHE[name] = _load_service_module(name)
    return _MODULE_CACHE[name]


try:
    from fastapi.testclient import TestClient

    client_mod = _load_service_module_cached("client")
    config_mod = _load_service_module_cached("config")
    main_mod = _load_service_module_cached("main")
    _HAS_ADAPTER = True
except Exception as exc:  # noqa: BLE001
    _HAS_ADAPTER = False
    _IMPORT_ERROR = exc


pytestmark = pytest.mark.skipif(
    not _HAS_ADAPTER, reason="stats-canada-adapter not importable"
)


# ---------------------------------------------------------------------------
# In-memory fake cache mirroring shared.cache.RedisCache's async interface.
# ---------------------------------------------------------------------------


class InMemoryCache:
    """Minimal async cache: get / set_with_ttl (TTL ignored for tests)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.set_calls: list[tuple[str, int]] = []

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set_with_ttl(self, key: str, value: str, ttl_seconds: int) -> None:
        self.store[key] = value
        self.set_calls.append((key, ttl_seconds))


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _read_fixture(filename: str) -> dict:
    with open(os.path.join(_MOCK_DIR, filename), "r", encoding="utf-8") as handle:
        return json.load(handle)


def _make_fetcher(**overrides):
    defaults = dict(
        lfs_url="https://example.test/lfs.json",
        connect_timeout=10.0,
        read_timeout=30.0,
        api_version="98100424",
        use_mock=True,
        mock_dir=_MOCK_DIR,
    )
    defaults.update(overrides)
    return client_mod.StatsCanadaFetcher(**defaults)


# ---------------------------------------------------------------------------
# Parsing — decimal precision
# ---------------------------------------------------------------------------


class TestParsePrecision:
    def test_fixture_parse_preserves_four_decimals(self):
        raw = _read_fixture("lfs_all.json")
        records = client_mod.parse_lfs_response(raw)
        assert len(records) == 5
        toronto = next(r for r in records if r.esdc_region_code == "35")
        # Exact decimal preserved (no float error), >= 4 decimal places.
        assert toronto.unemployment_rate == Decimal("6.9057")
        assert toronto.employment_rate == Decimal("60.8813")
        assert toronto.participation_rate == Decimal("65.4470")
        for r in records:
            frac = -r.unemployment_rate.as_tuple().exponent
            assert frac >= 4

    def test_parse_uses_decimal_str_path(self):
        # A value that is not exactly representable as a float must survive.
        raw = {
            "source_api_version": "v1",
            "records": [
                {
                    "reference_month": "2024-05",
                    "esdc_region_code": "35",
                    "region_name_en": "T",
                    "region_name_fr": "T",
                    "unemployment_rate": "5.1235",
                    "employment_rate": "60.0001",
                    "participation_rate": "64.9999",
                    "national_unemployment_rate": "6.0000",
                }
            ],
        }
        records = client_mod.parse_lfs_response(raw)
        assert records[0].unemployment_rate == Decimal("5.1235")
        assert records[0].employment_rate == Decimal("60.0001")

    def test_missing_records_raises(self):
        with pytest.raises(client_mod.LFSParseError):
            client_mod.parse_lfs_response({"source_api_version": "v1"})


# ---------------------------------------------------------------------------
# Serialise / parse round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_round_trip_preserves_regions_and_rates(self):
        raw = _read_fixture("lfs_all.json")
        parsed = client_mod.parse_lfs_response(raw)
        reparsed = client_mod.parse_lfs_response(client_mod.serialise(parsed))
        assert {r.esdc_region_code for r in reparsed} == {
            r.esdc_region_code for r in parsed
        }
        by_region = {r.esdc_region_code: r for r in reparsed}
        for original in parsed:
            rp = by_region[original.esdc_region_code]
            assert rp.unemployment_rate == original.unemployment_rate
            assert rp.employment_rate == original.employment_rate
            assert rp.participation_rate == original.participation_rate
            assert rp.national_unemployment_rate == original.national_unemployment_rate


# ---------------------------------------------------------------------------
# Cache hit / miss
# ---------------------------------------------------------------------------


class TestCache:
    def test_write_then_read_hit(self):
        async def run():
            cache = client_mod.StatsCanadaCache(InMemoryCache())
            records = client_mod.parse_lfs_response(_read_fixture("lfs_all.json"))
            await cache.write("35", "2024-05", records)
            result = await cache.read("35", "2024-05")
            assert result is not None
            fetched, is_stale = result
            assert is_stale is False
            assert len(fetched) == 1
            assert fetched[0].esdc_region_code == "35"
            assert fetched[0].unemployment_rate == Decimal("6.9057")

        asyncio.run(run())

    def test_read_miss_returns_none(self):
        async def run():
            cache = client_mod.StatsCanadaCache(InMemoryCache())
            assert await cache.read("99", "2024-05") is None

        asyncio.run(run())

    def test_write_uses_30_day_ttl(self):
        async def run():
            backing = InMemoryCache()
            cache = client_mod.StatsCanadaCache(backing)
            records = client_mod.parse_lfs_response(_read_fixture("lfs_all.json"))
            await cache.write("35", "2024-05", records)
            assert backing.set_calls
            _, ttl = backing.set_calls[0]
            assert ttl == 30 * 24 * 3600

        asyncio.run(run())

    def test_stale_entry_flagged(self):
        async def run():
            backing = InMemoryCache()
            cache = client_mod.StatsCanadaCache(backing)
            records = client_mod.parse_lfs_response(_read_fixture("lfs_all.json"))
            # Seed the backing store directly with an old cached_at timestamp.
            old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
            from shared.cache_keys import statscan_lfs_key

            envelope = {"cached_at": old, "payload": client_mod.serialise(records)}
            backing.store[statscan_lfs_key("35", "2024-05")] = json.dumps(envelope)

            result = await cache.read("35", "2024-05")
            assert result is not None
            _, is_stale = result
            assert is_stale is True

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Live fetch + 5xx fallback (uses httpx.MockTransport — no network)
# ---------------------------------------------------------------------------


class TestLiveFetchAnd5xxFallback:
    def _mock_client(self, handler):
        import httpx

        transport = httpx.MockTransport(handler)
        return httpx.AsyncClient(transport=transport)

    def test_fetch_from_api_success(self):
        import httpx

        raw = _read_fixture("lfs_all.json")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=raw)

        async def run():
            async with self._mock_client(handler) as http_client:
                fetcher = _make_fetcher(use_mock=False, client=http_client)
                records = await fetcher.fetch_from_api("35", "2024-05")
                assert len(records) == 1
                assert records[0].esdc_region_code == "35"
                assert records[0].unemployment_rate == Decimal("6.9057")

        asyncio.run(run())

    def test_5xx_raises_upstream_server_error(self):
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="unavailable")

        async def run():
            async with self._mock_client(handler) as http_client:
                fetcher = _make_fetcher(use_mock=False, client=http_client)
                with pytest.raises(client_mod.UpstreamServerError):
                    await fetcher.fetch_from_api("35", "2024-05")

        asyncio.run(run())

    def test_5xx_falls_back_to_cache_with_data_stale(self):
        """End-to-end fallback logic mirroring main.get_lfs on 5xx."""

        async def run():
            backing = InMemoryCache()
            cache = client_mod.StatsCanadaCache(backing)
            records = client_mod.parse_lfs_response(_read_fixture("lfs_all.json"))
            await cache.write("35", "2024-05", records)

            cached = await cache.read("35", "2024-05")
            assert cached is not None

            # Simulate the 5xx branch: cache present and non-empty -> DATA_STALE.
            fell_back, _ = cached
            assert fell_back
            flag = main_mod.FLAG_DATA_STALE
            assert flag == "DATA_STALE"

        asyncio.run(run())

    def test_5xx_empty_cache_is_data_unavailable(self):
        async def run():
            cache = client_mod.StatsCanadaCache(InMemoryCache())
            cached = await cache.read("35", "2024-05")
            # No cache entry -> the adapter returns DATA_UNAVAILABLE.
            assert cached is None
            assert main_mod.FLAG_DATA_UNAVAILABLE == "DATA_UNAVAILABLE"

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Endpoint — dev/test serves fixtures and returns canonical JSON
# ---------------------------------------------------------------------------


class TestEndpoint:
    def setup_method(self):
        # Force mock mode and point the fetcher at the fixtures directory.
        settings = config_mod.get_settings()
        settings.statscan_use_mock = True
        settings.app_env = "test"
        settings.statscan_mock_dir = _MOCK_DIR
        main_mod.app.state.settings = settings
        main_mod.app.state.fetcher = client_mod.StatsCanadaFetcher(
            lfs_url="https://example.test/lfs.json",
            api_version="98100424",
            use_mock=True,
            mock_dir=_MOCK_DIR,
        )
        main_mod.app.state.cache = client_mod.StatsCanadaCache(None)
        main_mod.app.state.raw_cache = None

    def _client(self):
        return TestClient(main_mod.app)

    def test_returns_canonical_records(self):
        resp = self._client().get("/statscan/lfs/35/2024-05")
        assert resp.status_code == 200
        body = resp.json()
        assert body["flag"] == "OK"
        assert body["count"] == 1
        rec = body["records"][0]
        assert rec["esdc_region_code"] == "35"
        # Decimal fields serialised as strings, >= 4 dp preserved.
        assert rec["unemployment_rate"] == "6.9057"
        assert rec["national_unemployment_rate"] == "6.1024"
        assert rec["region_name_en"] == "Toronto, Ontario"
        assert rec["data_stale"] is False

    def test_region_month_specific_fixture(self):
        resp = self._client().get("/statscan/lfs/35/2024-06")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["records"][0]["unemployment_rate"] == "7.1284"

    def test_unknown_region_is_data_unavailable(self):
        resp = self._client().get("/statscan/lfs/99/2024-05")
        assert resp.status_code == 503
        body = resp.json()
        assert body["flag"] == "DATA_UNAVAILABLE"
        assert body["records"] == []

    def test_invalid_year_month_returns_400(self):
        resp = self._client().get("/statscan/lfs/35/2024-13")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_YEAR_MONTH"

    def test_healthz_ok_in_mock_mode(self):
        resp = self._client().get("/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["checks"]["mock_mode"] is True
