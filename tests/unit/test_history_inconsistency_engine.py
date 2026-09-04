"""
tests/unit/test_history_inconsistency_engine.py
================================================
Unit tests for the Employment History Inconsistency Engine (task 14.1).

Coverage:
  * Exact-Decimal earnings discrepancy boundary (Requirements 6.2, 6.4):
      - exactly $2,000.00 difference -> NO flag
      - $2,000.01 difference         -> one CRITICAL flag
  * Equal ROE / T4 earnings -> discrepancy == Decimal("0"), no CRITICAL
    Indicator (Requirement 6.10 / Property 4 example).
  * CRA Adapter unavailable / timeout -> a single UNRESOLVED Indicator
    (Requirement 6.1, 6.3).
  * Missing or non-numeric T4 ``employment_income`` -> a single UNRESOLVED
    Indicator (Requirement 6.3).
  * The CRITICAL discrepancy Indicator carries the CAD amounts in its payload.

All tests inject a fake async CRA transport — no live network is used.

The service directory is hyphenated (``services/history-inconsistency-engine``)
and its modules import each other by bare name, so we load them under unique
namespaced (``histinconsist_*``) keys and restore any pre-existing bare-name
modules afterwards, mirroring ``tests/unit/test_repetitive_pattern_engine.py``.

Requirements: 6.1, 6.2, 6.3, 6.4, 6.10
"""

from __future__ import annotations

import importlib.util
import os
import sys
from decimal import Decimal
from typing import Any

import pytest

from shared.models import EngineType, Severity

# ---------------------------------------------------------------------------
# Load the hyphenated history-inconsistency-engine service modules.
# ---------------------------------------------------------------------------

_SERVICE_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "services",
        "history-inconsistency-engine",
    )
)

_BARE_NAMES = ("config", "client", "discrepancy", "main")
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
        sys.modules[f"histinconsist_{bare}"] = module
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
_client = _loaded["client"]
_discrepancy = _loaded["discrepancy"]
_main = _loaded["main"]

# Task 29.1: protected routes require the INVESTIGATOR role; inject a principal.
from shared.auth import get_current_user as _get_current_user  # noqa: E402

_main.app.dependency_overrides[_get_current_user] = lambda: {"roles": ["INVESTIGATOR"]}

compute_discrepancy = _discrepancy.compute_discrepancy
check_earnings_discrepancy = _discrepancy.check_earnings_discrepancy
CRAAdapterClient = _client.CRAAdapterClient
CRAAdapterUnavailable = _client.CRAAdapterUnavailable

try:
    from fastapi.testclient import TestClient

    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001
    _HAS_FASTAPI = False


# ---------------------------------------------------------------------------
# Fake async CRA transports (no network)
# ---------------------------------------------------------------------------


def _ok_transport(record: dict[str, Any]):
    async def _t(url: str, timeout: float) -> dict[str, Any]:
        return {"status": "OK", "record": record}

    return _t


def _status_transport(status_value: str):
    async def _t(url: str, timeout: float) -> dict[str, Any]:
        return {"status": status_value}

    return _t


def _raising_transport(exc: Exception):
    async def _t(url: str, timeout: float) -> dict[str, Any]:
        raise exc

    return _t


def _make_client(transport) -> "CRAAdapterClient":
    return CRAAdapterClient(
        "http://cra-adapter:8011", timeout_seconds=5.0, transport=transport
    )


# ===========================================================================
# compute_discrepancy — exact Decimal arithmetic (6.2, 6.10)
# ===========================================================================


class TestComputeDiscrepancy:
    def test_equal_earnings_is_exactly_zero(self):
        """Numerically identical amounts produce exactly Decimal('0')."""
        result = compute_discrepancy(Decimal("4321.55"), Decimal("4321.55"))
        assert result == Decimal("0")

    def test_absolute_difference_is_symmetric(self):
        """The result is the absolute difference regardless of argument order."""
        assert compute_discrepancy(Decimal("100"), Decimal("2500")) == Decimal("2400")
        assert compute_discrepancy(Decimal("2500"), Decimal("100")) == Decimal("2400")

    def test_exact_decimal_no_float_drift(self):
        """Many-decimal-place values stay exact (no binary-float noise)."""
        result = compute_discrepancy(Decimal("0.10"), Decimal("0.30"))
        assert result == Decimal("0.20")


# ===========================================================================
# check_earnings_discrepancy — $2,000 boundary (6.4) and P4 example (6.10)
# ===========================================================================


class TestEarningsDiscrepancyBoundary:
    def test_exactly_2000_no_flag(self):
        """A difference of exactly $2,000 does NOT flag (strict > threshold)."""
        indicators = check_earnings_discrepancy(Decimal("5000"), Decimal("3000"))
        assert indicators == []

    def test_2000_01_flags_critical(self):
        """A difference of $2,000.01 produces one CRITICAL Indicator."""
        indicators = check_earnings_discrepancy(
            Decimal("5000.01"), Decimal("3000.00")
        )
        assert len(indicators) == 1
        ind = indicators[0]
        assert ind.engine == EngineType.HISTORY_INCONSISTENCY
        assert ind.severity == Severity.CRITICAL
        assert ind.algorithm == "earnings_discrepancy"

    def test_equal_earnings_no_flag_and_zero_discrepancy(self):
        """Equal earnings -> discrepancy 0 and no CRITICAL Indicator (P4)."""
        earnings = Decimal("42000.00")
        assert compute_discrepancy(earnings, earnings) == Decimal("0")
        assert check_earnings_discrepancy(earnings, earnings) == []

    def test_critical_indicator_carries_cad_amounts(self):
        """The CRITICAL Indicator payload records the exact CAD amounts."""
        indicators = check_earnings_discrepancy(
            Decimal("10000.00"), Decimal("2500.00"), claim_id="claim-1"
        )
        assert len(indicators) == 1
        payload = indicators[0].payload
        assert payload["roe_earnings_cad"] == "10000.00"
        assert payload["t4_earnings_cad"] == "2500.00"
        assert payload["discrepancy_cad"] == "7500.00"
        assert payload["threshold_cad"] == "2000"

    def test_custom_threshold_respected(self):
        """A custom threshold changes the flag boundary."""
        # Difference is 3000; with a 5000 threshold there is no flag.
        assert (
            check_earnings_discrepancy(
                Decimal("8000"), Decimal("5000"), threshold=Decimal("5000")
            )
            == []
        )


# ===========================================================================
# CRA client — unavailable / missing / non-numeric map to UNRESOLVED (6.1, 6.3)
# ===========================================================================


class TestCRAAdapterClient:
    def test_ok_record_resolves_with_decimal(self):
        client = _make_client(_ok_transport({"employment_income": "48000.50"}))
        lookup = _run(client.fetch_t4("sin-1", 2023, "BN1"))
        assert lookup.resolved is True
        assert lookup.t4_earnings == Decimal("48000.50")

    def test_transport_error_is_unresolved(self):
        client = _make_client(_raising_transport(CRAAdapterUnavailable("boom")))
        lookup = _run(client.fetch_t4("sin-1", 2023, "BN1"))
        assert lookup.unresolved is True
        assert lookup.reason == _client.REASON_UNAVAILABLE

    def test_adapter_reported_unavailable_is_unresolved(self):
        client = _make_client(_status_transport("UNAVAILABLE"))
        lookup = _run(client.fetch_t4("sin-1", 2023, "BN1"))
        assert lookup.unresolved is True
        assert lookup.reason == _client.REASON_UNAVAILABLE

    def test_missing_field_is_unresolved(self):
        client = _make_client(_ok_transport({"noc_category": "1"}))
        lookup = _run(client.fetch_t4("sin-1", 2023, "BN1"))
        assert lookup.unresolved is True
        assert lookup.reason == _client.REASON_MISSING_FIELD

    def test_non_numeric_field_is_unresolved(self):
        client = _make_client(_ok_transport({"employment_income": "not-a-number"}))
        lookup = _run(client.fetch_t4("sin-1", 2023, "BN1"))
        assert lookup.unresolved is True
        assert lookup.reason == _client.REASON_NON_NUMERIC


# ===========================================================================
# Endpoint behaviour (6.1, 6.3, 6.4)
# ===========================================================================


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not importable")
class TestAnalyzeEndpoint:
    def _client_with_transport(self, transport):
        _main.app.state.settings = _main.get_settings()
        _main.app.state.cra_client = _make_client(transport)
        return TestClient(_main.app)

    def _payload(self, roe: str = "5000.00", **overrides):
        payload = {
            "sin_hash": "sin-abc",
            "tax_year": 2023,
            "employer_bn": "123456789RT0001",
            "roe_earnings": roe,
        }
        payload.update(overrides)
        return payload

    def test_cra_unavailable_returns_single_unresolved(self):
        client = self._client_with_transport(
            _raising_transport(CRAAdapterUnavailable("timeout"))
        )
        resp = client.post("/engine/analyze", json=self._payload())
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "UNRESOLVED"
        assert len(body["indicators"]) == 1
        ind = body["indicators"][0]
        assert ind["severity"] == Severity.UNRESOLVED.value
        assert ind["engine"] == EngineType.HISTORY_INCONSISTENCY.value
        assert ind["algorithm"] == "cra_t4_retrieval"

    def test_missing_t4_field_returns_single_unresolved(self):
        client = self._client_with_transport(_ok_transport({"noc_category": "2"}))
        resp = client.post("/engine/analyze", json=self._payload())
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "UNRESOLVED"
        assert len(body["indicators"]) == 1
        assert body["indicators"][0]["payload"]["reason"] == _client.REASON_MISSING_FIELD

    def test_non_numeric_t4_field_returns_single_unresolved(self):
        client = self._client_with_transport(
            _ok_transport({"employment_income": "abc"})
        )
        resp = client.post("/engine/analyze", json=self._payload())
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "UNRESOLVED"
        assert body["indicators"][0]["payload"]["reason"] == _client.REASON_NON_NUMERIC

    def test_equal_earnings_returns_ok_with_no_indicators(self):
        client = self._client_with_transport(
            _ok_transport({"employment_income": "5000.00"})
        )
        resp = client.post("/engine/analyze", json=self._payload(roe="5000.00"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "OK"
        assert body["indicators"] == []

    def test_boundary_exactly_2000_no_indicator(self):
        client = self._client_with_transport(
            _ok_transport({"employment_income": "3000.00"})
        )
        resp = client.post("/engine/analyze", json=self._payload(roe="5000.00"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "OK"
        assert body["indicators"] == []

    def test_boundary_2000_01_returns_critical(self):
        client = self._client_with_transport(
            _ok_transport({"employment_income": "3000.00"})
        )
        resp = client.post("/engine/analyze", json=self._payload(roe="5000.01"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "OK"
        assert len(body["indicators"]) == 1
        ind = body["indicators"][0]
        assert ind["severity"] == Severity.CRITICAL.value
        assert ind["payload"]["discrepancy_cad"] == "2000.01"

    def test_healthz(self):
        client = self._client_with_transport(
            _ok_transport({"employment_income": "1.00"})
        )
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# tiny async runner (avoids depending on pytest-asyncio)
# ---------------------------------------------------------------------------


def _run(coro):
    import asyncio

    return asyncio.run(coro)
