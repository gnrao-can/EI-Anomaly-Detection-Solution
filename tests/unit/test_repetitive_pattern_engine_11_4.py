"""
tests/unit/test_repetitive_pattern_engine_11_4.py
=================================================
Task 11.4 unit tests for the Repetitive Pattern Engine.

This file is deliberately kept SEPARATE from the other repetitive-pattern test
modules so it can be authored alongside the concurrently-running property-test
task (11.3) without write conflicts. It targets exactly the four scenarios
enumerated by task 11.4, exercising the code delivered in tasks 11.1 and 11.2:

  1. DATA_UNAVAILABLE Indicator on DB failure (Req 3.1, 3.2) — the claim-history
     store raising ``ClaimHistoryUnavailable`` yields a single DATA_UNAVAILABLE
     Indicator and NO other analysis, both via the ``POST /engine/analyze``
     endpoint and directly against the repository fallback contract.
  2. Employer repetition boundary (Req 3.3): exactly 2 same-employer claims ->
     no flag; exactly 3 within a 36-month window -> a HIGH Indicator.
  3. Hours clustering at the +/-5 hour threshold boundary (Req 3.4): inclusive at
     exactly 5 hours, excluded just past 5 hours.
  4. NARRATIVE_ANALYSIS_TIMEOUT on LLM timeout (Req 3.6): a slow / failing LLM
     transport records the timeout flag and the engine proceeds.

All dependencies are stubbed with in-memory fakes / injected transports — no
live PostgreSQL, Kafka, or LLM Service is required. Per the task constraints we
load the hyphenated ``services/repetitive-pattern-engine`` modules through the
shared ``load_service_module`` conftest helper (NOT ad-hoc importlib) so the
unit suite stays collision-free when run as a whole.

Requirements: 3.1, 3.2, 3.3, 3.4, 3.6
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import pytest

from shared.models import EngineType, Severity
from tests.conftest import load_service_module

# ---------------------------------------------------------------------------
# Load the hyphenated repetitive-pattern-engine service modules (collision-free)
# ---------------------------------------------------------------------------

_SVC = "repetitive-pattern-engine"
_SIBLINGS = [
    "config",
    "repository",
    "patterns",
    "hours_clustering",
    "llm_client",
    "kafka_producer",
    "main",
]

_repository = load_service_module(_SVC, "repository", siblings=_SIBLINGS)
_patterns = load_service_module(_SVC, "patterns", siblings=_SIBLINGS)
_hours = load_service_module(_SVC, "hours_clustering", siblings=_SIBLINGS)
_llm = load_service_module(_SVC, "llm_client", siblings=_SIBLINGS)
_main = load_service_module(_SVC, "main", siblings=_SIBLINGS)

# Task 29.1: protected routes require the INVESTIGATOR role; inject a principal.
from shared.auth import get_current_user as _get_current_user  # noqa: E402

_main.app.dependency_overrides[_get_current_user] = lambda: {"roles": ["INVESTIGATOR"]}

ClaimHistoryRecord = _repository.ClaimHistoryRecord
FakeClaimHistoryRepository = _repository.FakeClaimHistoryRepository
check_employer_repetition = _patterns.check_employer_repetition
check_hours_clustering = _hours.check_hours_clustering
LLMServiceClient = _llm.LLMServiceClient
build_narrative_pairs = _llm.build_narrative_pairs
NARRATIVE_ANALYSIS_TIMEOUT = _llm.NARRATIVE_ANALYSIS_TIMEOUT

try:
    from fastapi.testclient import TestClient

    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001
    _HAS_FASTAPI = False


# ---------------------------------------------------------------------------
# Small builders
# ---------------------------------------------------------------------------


def _rec(bn: str, iso: str, hours: float = 700.0) -> "ClaimHistoryRecord":
    return ClaimHistoryRecord(
        claim_id=f"c-{bn}-{iso}",
        employer_bn=bn,
        separation_date=date.fromisoformat(iso),
        insurable_hours=hours,
    )


def _prior(hours: float, iso: str = "2022-01-01", bn: str = "BN1") -> "ClaimHistoryRecord":
    return _rec(bn, iso, hours)


def _fixed_transport(score: float):
    async def _t(url: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        return {"similarity": score}

    return _t


# ===========================================================================
# 1. DATA_UNAVAILABLE Indicator on DB failure (Req 3.1, 3.2)
# ===========================================================================


class TestDataUnavailableOnDbFailure:
    def test_fake_repository_raises_claim_history_unavailable(self):
        """``fail_next`` arms the fake to raise the unavailability error, which
        the engine converts into the DATA_UNAVAILABLE path."""
        repo = FakeClaimHistoryRepository()
        repo.add(
            "sin-fail",
            _rec("BN1", "2023-01-01"),
        )
        repo.fail_next()
        with pytest.raises(_repository.ClaimHistoryUnavailable):
            asyncio.run(repo.fetch_history("sin-fail"))

    @pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not importable")
    def test_endpoint_db_failure_returns_single_data_unavailable(self):
        """A repository fetch failure yields exactly one DATA_UNAVAILABLE
        Indicator and NO employer/sequential/hours Indicators (Req 3.1/3.2)."""
        repo = FakeClaimHistoryRepository()
        # Repetitive history that WOULD otherwise flag — proves the DB failure
        # short-circuits all further analysis.
        repo.add_many(
            "sin-db",
            [
                _rec("BN1", "2022-01-01"),
                _rec("BN1", "2023-01-01"),
                _rec("BN1", "2024-01-01"),
            ],
        )
        repo.fail_next()

        client = self._client(repo)
        resp = client.post(
            "/engine/analyze",
            json={
                "sin_hash": "sin-db",
                "employer_bn": "BN1",
                "insurable_hours": 700.0,
                "as_of": "2024-06-01",
            },
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
        # No pattern algorithms present — analysis did not proceed.
        algos = {i["algorithm"] for i in body["indicators"]}
        assert "employer_repetition" not in algos
        assert "sequential_pattern_mining" not in algos
        assert "hours_clustering" not in algos

    @pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not importable")
    def test_endpoint_no_repository_returns_data_unavailable(self):
        """When no repository is wired at all, DATA_UNAVAILABLE is returned."""
        client = self._client(None)
        resp = client.post("/engine/analyze", json={"sin_hash": "sin-none"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "DATA_UNAVAILABLE"
        assert len(body["indicators"]) == 1
        assert body["indicators"][0]["payload"]["reason"] == "DATA_UNAVAILABLE"

    def _client(self, repo, *, llm_client=None, producer=None):
        _main.app.state.settings = _main.get_settings()
        _main.app.state.pool = None
        _main.app.state.repository = repo
        _main.app.state.llm_client = llm_client
        _main.app.state.indicator_producer = producer
        return TestClient(_main.app)


# ===========================================================================
# 2. Employer repetition boundary: 2 -> no flag, 3 -> flag (Req 3.3)
# ===========================================================================


class TestEmployerRepetitionBoundary:
    def test_exactly_two_same_employer_no_flag(self):
        """Exactly 2 claims from the same employer BN -> no Indicator."""
        claims = [_rec("BN1", "2022-01-01"), _rec("BN1", "2023-01-01")]
        assert check_employer_repetition(claims) == []

    def test_exactly_three_same_employer_within_window_flags(self):
        """Exactly 3 same-employer claims inside 36 months -> one HIGH flag."""
        claims = [
            _rec("BN1", "2022-01-01"),
            _rec("BN1", "2023-01-01"),
            _rec("BN1", "2024-01-01"),
        ]
        indicators = check_employer_repetition(claims)
        assert len(indicators) == 1
        ind = indicators[0]
        assert ind.severity == Severity.HIGH
        assert ind.engine == EngineType.REPETITIVE_PATTERN
        assert ind.algorithm == "employer_repetition"
        assert ind.payload["employer_bn"] == "BN1"
        assert ind.payload["claim_count"] == 3

    @pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not importable")
    def test_endpoint_three_same_employer_surfaces_employer_indicator(self):
        """Three same-employer claims surface the employer-repetition Indicator
        end-to-end through the analyze endpoint."""
        repo = FakeClaimHistoryRepository()
        repo.add_many(
            "sin-e",
            [
                _rec("BN7", "2022-02-01"),
                _rec("BN7", "2023-02-01"),
                _rec("BN7", "2024-02-01"),
            ],
        )
        _main.app.state.settings = _main.get_settings()
        _main.app.state.pool = None
        _main.app.state.repository = repo
        _main.app.state.llm_client = None
        _main.app.state.indicator_producer = None
        client = TestClient(_main.app)
        resp = client.post(
            "/engine/analyze",
            json={"sin_hash": "sin-e", "employer_bn": "BN7", "as_of": "2024-06-01"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "OK"
        algos = {i["algorithm"] for i in body["indicators"]}
        assert "employer_repetition" in algos


# ===========================================================================
# 3. Hours clustering at the +/-5 hour threshold boundary (Req 3.4)
# ===========================================================================


class TestHoursClusteringBoundary:
    def test_exactly_five_hours_inclusive_flags(self):
        """Current claim exactly 5 hours from threshold + 2 near-threshold priors
        -> HIGH clustering Indicator (inclusive boundary)."""
        priors = [_prior(705.0), _prior(695.0)]
        indicators = check_hours_clustering(705.0, priors, threshold=700.0)
        assert len(indicators) == 1
        ind = indicators[0]
        assert ind.severity == Severity.HIGH
        assert ind.algorithm == "hours_clustering"
        assert ind.payload["clustered_claim_count"] == 3

    def test_just_over_five_hours_excluded(self):
        """Current claim 5.01 hours from threshold -> no Indicator."""
        priors = [_prior(705.0), _prior(695.0)]
        assert check_hours_clustering(705.01, priors, threshold=700.0) == []

    def test_prior_just_over_five_hours_not_counted(self):
        """A prior claim 5.01h away is not counted toward the >=2 prior tally,
        dropping the near-threshold priors below the minimum (no Indicator)."""
        # One prior at exactly 5h (counts), one at 5.01h (excluded) -> only 1
        # qualifying prior, below the min_prior=2 requirement.
        priors = [_prior(705.0), _prior(705.01)]
        assert check_hours_clustering(700.0, priors, threshold=700.0) == []

    def test_two_qualifying_priors_at_boundary_flags(self):
        """Two priors exactly at the +/-5h edges both count -> Indicator fires."""
        priors = [_prior(705.0), _prior(695.0)]
        indicators = check_hours_clustering(700.0, priors, threshold=700.0)
        assert len(indicators) == 1
        assert indicators[0].payload["prior_near_threshold_count"] == 2


# ===========================================================================
# 4. NARRATIVE_ANALYSIS_TIMEOUT on LLM timeout (Req 3.6)
# ===========================================================================


class TestNarrativeAnalysisTimeout:
    def test_slow_llm_records_timeout_flag(self):
        """An LLM transport that never answers in time -> a single
        NARRATIVE_ANALYSIS_TIMEOUT flag Indicator (analysis proceeds)."""

        async def _slow(url: str, body: dict[str, Any], timeout: float):
            await asyncio.sleep(1.0)
            return {"similarity": 0.99}

        client = LLMServiceClient("http://llm", timeout_seconds=0.05, transport=_slow)
        pairs = build_narrative_pairs([("c1", "same text"), ("c2", "same text")])
        indicators = asyncio.run(client.check_narratives(pairs, claim_id="c1"))
        assert len(indicators) == 1
        ind = indicators[0]
        assert ind.severity == Severity.UNRESOLVED
        assert ind.algorithm == "narrative_similarity"
        assert ind.payload["flag"] == NARRATIVE_ANALYSIS_TIMEOUT

    def test_transport_error_maps_to_timeout_flag(self):
        """Any transport failure is mapped to NARRATIVE_ANALYSIS_TIMEOUT."""

        async def _boom(url: str, body: dict[str, Any], timeout: float):
            raise RuntimeError("connection refused")

        client = LLMServiceClient("http://llm", transport=_boom)
        pairs = build_narrative_pairs([("c1", "x y z"), ("c2", "x y z")])
        indicators = asyncio.run(client.check_narratives(pairs))
        assert len(indicators) == 1
        assert indicators[0].payload["flag"] == NARRATIVE_ANALYSIS_TIMEOUT

    @pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not importable")
    def test_endpoint_llm_timeout_surfaces_flag(self):
        """Through the analyze endpoint, a slow LLM surfaces the timeout flag
        while the rest of the analysis still completes."""
        repo = FakeClaimHistoryRepository()
        repo.add_many("sin-lt", [_prior(700.0, "2023-01-01", "BN1")])

        async def _slow(url: str, body: dict[str, Any], timeout: float):
            await asyncio.sleep(0.3)
            return {"similarity": 0.99}

        llm_client = LLMServiceClient("http://llm", timeout_seconds=0.1, transport=_slow)
        _main.app.state.settings = _main.get_settings()
        _main.app.state.pool = None
        _main.app.state.repository = repo
        _main.app.state.llm_client = llm_client
        _main.app.state.indicator_producer = None
        client = TestClient(_main.app)

        resp = client.post(
            "/engine/analyze",
            json={
                "sin_hash": "sin-lt",
                "claim_id": "44444444-4444-4444-4444-444444444444",
                "separation_narrative": "quit",
                "prior_narratives": [{"claim_id": "old", "narrative": "quit"}],
                "as_of": "2024-06-01",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        flags = [
            i
            for i in body["indicators"]
            if i["payload"].get("flag") == NARRATIVE_ANALYSIS_TIMEOUT
        ]
        assert len(flags) == 1
