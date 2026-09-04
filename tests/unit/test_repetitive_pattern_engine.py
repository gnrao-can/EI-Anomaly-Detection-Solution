"""
tests/unit/test_repetitive_pattern_engine.py
=============================================
Unit tests for the Repetitive Pattern Engine (task 11.1).

Coverage:
  * Employer-repetition boundary within a 36-month window (Requirement 3.2):
      - exactly 2 same-employer claims  -> NO flag
      - exactly 3 same-employer claims  -> HIGH flag
      - a same-employer trio spanning MORE than 36 months -> NO flag
  * Sequential interval pattern within +/-14 days -> HIGH flag (Req 3.3, 3.8).
  * DB unavailable -> a single DATA_UNAVAILABLE Indicator and NO other analysis
    (Requirement 3.1 fallback).

All tests use the in-memory ``FakeClaimHistoryRepository`` — no live database.

The service directory is hyphenated (``services/repetitive-pattern-engine``)
and its modules import each other by bare name, so we load them under unique
namespaced keys and restore any pre-existing bare-name modules afterwards,
mirroring ``tests/unit/test_cbsa_adapter.py``.

Requirements: 3.1, 3.2, 3.3, 3.8
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from datetime import date, timedelta
from typing import Any

import pytest

from shared.models import EngineType, Severity

# ---------------------------------------------------------------------------
# Load the hyphenated repetitive-pattern-engine service modules.
# ---------------------------------------------------------------------------

_SERVICE_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "services",
        "repetitive-pattern-engine",
    )
)

_BARE_NAMES = (
    "config",
    "repository",
    "patterns",
    "hours_clustering",
    "llm_client",
    "kafka_producer",
    "main",
)
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
        sys.modules[f"reppattern_{bare}"] = module
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
_repository = _loaded["repository"]
_patterns = _loaded["patterns"]
_main = _loaded["main"]

# Task 29.1: protected routes require the INVESTIGATOR role; inject a principal.
from shared.auth import get_current_user as _get_current_user  # noqa: E402

_main.app.dependency_overrides[_get_current_user] = lambda: {"roles": ["INVESTIGATOR"]}

ClaimHistoryRecord = _repository.ClaimHistoryRecord
FakeClaimHistoryRepository = _repository.FakeClaimHistoryRepository
check_employer_repetition = _patterns.check_employer_repetition
sequential_pattern_mining = _patterns.sequential_pattern_mining

try:
    from fastapi.testclient import TestClient

    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001
    _HAS_FASTAPI = False


def _rec(bn: str, iso: str, hours: float = 700.0) -> "ClaimHistoryRecord":
    return ClaimHistoryRecord(
        claim_id=f"c-{bn}-{iso}",
        employer_bn=bn,
        separation_date=date.fromisoformat(iso),
        insurable_hours=hours,
    )


# ===========================================================================
# Employer repetition boundary within a 36-month window (3.2)
# ===========================================================================


class TestEmployerRepetition:
    def test_two_claims_same_employer_no_flag(self):
        """Exactly 2 same-employer claims -> no Indicator (boundary just below 3)."""
        claims = [_rec("BN1", "2022-01-01"), _rec("BN1", "2022-07-01")]
        assert check_employer_repetition(claims) == []

    def test_three_claims_same_employer_within_36_months_flags(self):
        """Exactly 3 same-employer claims within 36 months -> one HIGH Indicator."""
        claims = [
            _rec("BN1", "2022-01-01"),
            _rec("BN1", "2023-01-01"),
            _rec("BN1", "2024-01-01"),
        ]
        indicators = check_employer_repetition(claims)
        assert len(indicators) == 1
        ind = indicators[0]
        assert ind.engine == EngineType.REPETITIVE_PATTERN
        assert ind.severity == Severity.HIGH
        assert ind.algorithm == "employer_repetition"
        assert ind.payload["employer_bn"] == "BN1"
        assert ind.payload["claim_count"] == 3

    def test_three_claims_spanning_more_than_36_months_no_flag(self):
        """3 same-employer claims spread across > 36 months do NOT flag."""
        # 2019-01, 2021-06 (~29 mo later), 2024-01 (>36 mo from first, and the
        # last two are ~31 mo apart) -> no 3 fit inside a single 36-month window.
        claims = [
            _rec("BN1", "2019-01-01"),
            _rec("BN1", "2021-06-01"),
            _rec("BN1", "2024-01-01"),
        ]
        assert check_employer_repetition(claims) == []

    def test_different_employers_do_not_aggregate(self):
        """Claims from different employers are not combined."""
        claims = [
            _rec("BN1", "2022-01-01"),
            _rec("BN2", "2022-06-01"),
            _rec("BN3", "2022-09-01"),
        ]
        assert check_employer_repetition(claims) == []

    def test_three_within_window_among_wider_spread_flags(self):
        """A tight trio inside a wider spread still triggers via the sliding window."""
        claims = [
            _rec("BN1", "2018-01-01"),  # far in the past
            _rec("BN1", "2023-01-01"),
            _rec("BN1", "2023-06-01"),
            _rec("BN1", "2023-11-01"),  # these last three fit in < 36 months
        ]
        indicators = check_employer_repetition(claims)
        assert len(indicators) == 1
        assert indicators[0].payload["claim_count"] >= 3


# ===========================================================================
# Sequential interval pattern within +/-14 days (3.3, 3.8)
# ===========================================================================


class TestSequentialPattern:
    def test_regular_cadence_within_tolerance_flags(self):
        """3 claims ~365 days apart (within +/-14 days) -> one HIGH Indicator."""
        base = date(2021, 1, 1)
        dates = [base, base + timedelta(days=360), base + timedelta(days=360 + 372)]
        indicators = sequential_pattern_mining(dates)
        assert len(indicators) == 1
        ind = indicators[0]
        assert ind.severity == Severity.HIGH
        assert ind.algorithm == "sequential_pattern_mining"
        assert ind.payload["pattern_length"] >= 3

    def test_two_claims_no_pattern(self):
        """Fewer than 3 claims cannot form a sequential pattern."""
        base = date(2021, 1, 1)
        assert sequential_pattern_mining([base, base + timedelta(days=365)]) == []

    def test_irregular_intervals_no_flag(self):
        """Wildly varying intervals do not form a repeating cadence."""
        base = date(2020, 1, 1)
        dates = [
            base,
            base + timedelta(days=30),
            base + timedelta(days=30 + 400),
        ]
        assert sequential_pattern_mining(dates) == []

    def test_exact_equal_intervals_flag(self):
        """Perfectly equal intervals (spread 0) always flag."""
        base = date(2019, 3, 1)
        dates = [base + timedelta(days=200 * i) for i in range(4)]
        indicators = sequential_pattern_mining(dates)
        assert len(indicators) == 1
        assert indicators[0].payload["pattern_length"] == 4


# ===========================================================================
# Endpoint: DB unavailable -> single DATA_UNAVAILABLE, no other analysis (3.1)
# ===========================================================================


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not importable")
class TestAnalyzeEndpointDataUnavailable:
    def _client_with_repo(self, repo):
        _main.app.state.settings = _main.get_settings()
        _main.app.state.pool = None
        _main.app.state.repository = repo
        return TestClient(_main.app)

    def test_db_unavailable_returns_single_data_unavailable_indicator(self):
        """A repository failure yields exactly one DATA_UNAVAILABLE Indicator and
        no employer/sequential Indicators."""
        repo = FakeClaimHistoryRepository()
        # Even with repetitive history present, a fetch failure short-circuits.
        repo.add_many(
            "sin-x",
            [
                _rec("BN1", "2022-01-01"),
                _rec("BN1", "2023-01-01"),
                _rec("BN1", "2024-01-01"),
            ],
        )
        repo.fail_next()

        client = self._client_with_repo(repo)
        resp = client.post(
            "/engine/analyze",
            json={"sin_hash": "sin-x", "claim_id": None, "employer_bn": "BN1"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "DATA_UNAVAILABLE"
        assert body["history_count"] == 0
        assert len(body["indicators"]) == 1
        ind = body["indicators"][0]
        assert ind["payload"]["reason"] == "DATA_UNAVAILABLE"
        assert ind["algorithm"] == "claim_history_retrieval"
        assert ind["engine"] == EngineType.REPETITIVE_PATTERN.value

    def test_no_repository_returns_data_unavailable(self):
        """When no repository is wired up at all, DATA_UNAVAILABLE is returned."""
        client = self._client_with_repo(None)
        resp = client.post("/engine/analyze", json={"sin_hash": "sin-none"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "DATA_UNAVAILABLE"
        assert len(body["indicators"]) == 1

    def test_healthy_history_produces_pattern_indicators(self):
        """With a working repository and repetitive history, Indicators appear."""
        repo = FakeClaimHistoryRepository()
        repo.add_many(
            "sin-y",
            [
                _rec("BN9", "2022-01-01"),
                _rec("BN9", "2023-01-05"),
                _rec("BN9", "2024-01-02"),
            ],
        )
        client = self._client_with_repo(repo)
        resp = client.post(
            "/engine/analyze",
            json={"sin_hash": "sin-y", "employer_bn": "BN9", "as_of": "2024-06-01"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "OK"
        assert body["history_count"] == 3
        algorithms = {i["algorithm"] for i in body["indicators"]}
        # Both detectors should fire on this yearly, same-employer cadence.
        assert "employer_repetition" in algorithms
        assert "sequential_pattern_mining" in algorithms


# ===========================================================================
# Repository rolling-window filter (3.1)
# ===========================================================================


class TestRepositoryWindow:
    def test_fetch_history_filters_to_rolling_window(self):
        repo = FakeClaimHistoryRepository(window_months=60)
        repo.add_many(
            "sin-z",
            [
                _rec("BN1", "2015-01-01"),  # older than 60 months from as_of
                _rec("BN1", "2021-01-01"),  # within window
                _rec("BN1", "2023-01-01"),  # within window
            ],
        )
        result = asyncio.run(
            repo.fetch_history("sin-z", as_of=date(2024, 1, 1))
        )
        dates = [r.separation_date for r in result]
        assert date(2015, 1, 1) not in dates
        assert date(2021, 1, 1) in dates
        assert date(2023, 1, 1) in dates
        # Ascending order preserved.
        assert dates == sorted(dates)
