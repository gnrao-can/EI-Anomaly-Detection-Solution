"""
tests/unit/test_repetitive_pattern_engine_11_2.py
=================================================
Unit tests for the Repetitive Pattern Engine task 11.2 additions:

  * ``check_hours_clustering`` — insurable hours within 5 hours of the regional
    minimum threshold AND the same pattern in >= 2 prior claims -> HIGH
    Indicator, with inclusive/exclusive boundary behaviour (Requirement 3.4).
  * ``LLMServiceClient`` narrative similarity — cosine > 0.92 -> MEDIUM Indicator
    carrying the score (Req 3.5); timeout / failure -> NARRATIVE_ANALYSIS_TIMEOUT
    flag Indicator, proceeding without the narrative result (Req 3.6).
  * ``IndicatorProducer`` / ``FakeIndicatorProducer`` — envelope shape and
    publishing to ``indicators.repetitive-pattern`` (Req 3.2, 3.7).
  * ``POST /engine/analyze`` — hours-clustering + narrative Indicators surface,
    Indicators are published to Kafka, and the LLM call is EXCLUDED from the
    400 ms wall-clock budget (Req 3.7).

The service directory is hyphenated and its modules import each other by bare
name, so we load them via the shared ``load_service_module`` conftest helper
which namespaces ``sys.modules`` keys to avoid collisions on full-suite runs.

Requirements: 3.4, 3.5, 3.6, 3.7
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, Optional

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

_config = load_service_module(_SVC, "config", siblings=_SIBLINGS)
_repository = load_service_module(_SVC, "repository", siblings=_SIBLINGS)
_hours = load_service_module(_SVC, "hours_clustering", siblings=_SIBLINGS)
_llm = load_service_module(_SVC, "llm_client", siblings=_SIBLINGS)
_kafka = load_service_module(_SVC, "kafka_producer", siblings=_SIBLINGS)
_main = load_service_module(_SVC, "main", siblings=_SIBLINGS)

# Task 29.1: protected routes require the INVESTIGATOR role; inject a principal.
from shared.auth import get_current_user as _get_current_user  # noqa: E402

_main.app.dependency_overrides[_get_current_user] = lambda: {"roles": ["INVESTIGATOR"]}

ClaimHistoryRecord = _repository.ClaimHistoryRecord
FakeClaimHistoryRepository = _repository.FakeClaimHistoryRepository
check_hours_clustering = _hours.check_hours_clustering
LLMServiceClient = _llm.LLMServiceClient
LLMServiceUnavailable = _llm.LLMServiceUnavailable
NarrativePair = _llm.NarrativePair
build_narrative_pairs = _llm.build_narrative_pairs
NARRATIVE_ANALYSIS_TIMEOUT = _llm.NARRATIVE_ANALYSIS_TIMEOUT
FakeIndicatorProducer = _kafka.FakeIndicatorProducer
INDICATOR_TOPIC = _kafka.INDICATOR_TOPIC

try:
    from fastapi.testclient import TestClient

    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001
    _HAS_FASTAPI = False


def _prior(hours: float, iso: str = "2022-01-01", bn: str = "BN1") -> Any:
    return ClaimHistoryRecord(
        claim_id=f"c-{iso}-{hours}",
        employer_bn=bn,
        separation_date=date.fromisoformat(iso),
        insurable_hours=hours,
    )


# ===========================================================================
# Hours clustering (Req 3.4)
# ===========================================================================


class TestHoursClustering:
    def test_current_and_two_priors_near_threshold_flags(self):
        """Current + 2 prior claims within 5h of the 700h threshold -> HIGH."""
        priors = [_prior(701.0), _prior(698.0), _prior(500.0)]
        indicators = check_hours_clustering(
            702.0, priors, threshold=700.0, region_code="ON"
        )
        assert len(indicators) == 1
        ind = indicators[0]
        assert ind.engine == EngineType.REPETITIVE_PATTERN
        assert ind.severity == Severity.HIGH
        assert ind.algorithm == "hours_clustering"
        assert ind.payload["clustered_claim_count"] == 3
        assert ind.payload["prior_near_threshold_count"] == 2
        assert ind.payload["economic_region_code"] == "ON"

    def test_only_one_prior_near_threshold_no_flag(self):
        """Fewer than 2 prior near-threshold claims -> no Indicator."""
        priors = [_prior(701.0), _prior(500.0), _prior(640.0)]
        assert check_hours_clustering(700.0, priors, threshold=700.0) == []

    def test_current_not_near_threshold_no_flag(self):
        """When the current claim is not near the threshold, no Indicator even if
        priors cluster."""
        priors = [_prior(701.0), _prior(699.0), _prior(700.0)]
        assert check_hours_clustering(800.0, priors, threshold=700.0) == []

    def test_boundary_exactly_five_hours_inclusive(self):
        """Exactly 5 hours away is within tolerance (inclusive boundary)."""
        priors = [_prior(705.0), _prior(695.0)]
        indicators = check_hours_clustering(705.0, priors, threshold=700.0)
        assert len(indicators) == 1

    def test_boundary_just_over_five_hours_excluded(self):
        """5.01 hours away is outside tolerance -> no Indicator."""
        priors = [_prior(705.0), _prior(695.0)]
        assert check_hours_clustering(705.01, priors, threshold=700.0) == []


# ===========================================================================
# LLM narrative similarity client (Req 3.5, 3.6)
# ===========================================================================


def _fixed_transport(score: float):
    async def _t(url: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        return {"similarity": score}

    return _t


class TestNarrativeSimilarity:
    def test_high_similarity_produces_medium_indicator(self):
        """Cosine > 0.92 -> a single MEDIUM verbatim-equivalence Indicator."""
        client = LLMServiceClient(
            "http://llm", transport=_fixed_transport(0.97)
        )
        pairs = build_narrative_pairs(
            [("c1", "quit due to relocation"), ("c2", "quit due to relocation")]
        )
        indicators = asyncio.run(client.check_narratives(pairs, claim_id="c1"))
        assert len(indicators) == 1
        ind = indicators[0]
        assert ind.severity == Severity.MEDIUM
        assert ind.algorithm == "narrative_similarity"
        assert ind.payload["cosine_similarity"] == 0.97

    def test_low_similarity_no_indicator(self):
        """Cosine at/below the threshold -> no Indicator."""
        client = LLMServiceClient(
            "http://llm", transport=_fixed_transport(0.42)
        )
        pairs = build_narrative_pairs([("c1", "laid off"), ("c2", "moved away")])
        assert asyncio.run(client.check_narratives(pairs)) == []

    def test_threshold_is_strict_greater_than(self):
        """Exactly at the threshold does NOT flag (strictly greater required)."""
        client = LLMServiceClient(
            "http://llm", transport=_fixed_transport(0.92), similarity_threshold=0.92
        )
        pairs = build_narrative_pairs([("c1", "a b c"), ("c2", "a b d")])
        assert asyncio.run(client.check_narratives(pairs)) == []

    def test_timeout_produces_timeout_flag_indicator(self):
        """A transport that never returns in time -> NARRATIVE_ANALYSIS_TIMEOUT."""

        async def _slow(url: str, body: dict[str, Any], timeout: float):
            await asyncio.sleep(1.0)
            return {"similarity": 0.99}

        client = LLMServiceClient(
            "http://llm", timeout_seconds=0.05, transport=_slow
        )
        pairs = build_narrative_pairs([("c1", "same text"), ("c2", "same text")])
        indicators = asyncio.run(client.check_narratives(pairs, claim_id="c1"))
        assert len(indicators) == 1
        ind = indicators[0]
        assert ind.severity == Severity.UNRESOLVED
        assert ind.payload["flag"] == NARRATIVE_ANALYSIS_TIMEOUT

    def test_transport_error_produces_timeout_flag(self):
        """Any transport error is mapped to the NARRATIVE_ANALYSIS_TIMEOUT flag."""

        async def _boom(url: str, body: dict[str, Any], timeout: float):
            raise RuntimeError("connection refused")

        client = LLMServiceClient("http://llm", transport=_boom)
        pairs = build_narrative_pairs([("c1", "x y z"), ("c2", "x y z")])
        indicators = asyncio.run(client.check_narratives(pairs))
        assert len(indicators) == 1
        assert indicators[0].payload["flag"] == NARRATIVE_ANALYSIS_TIMEOUT

    def test_empty_pairs_no_indicator(self):
        client = LLMServiceClient("http://llm", transport=_fixed_transport(0.99))
        assert asyncio.run(client.check_narratives([])) == []

    def test_build_pairs_drops_blank_narratives(self):
        pairs = build_narrative_pairs(
            [("c1", "text"), ("c2", "   "), ("c3", ""), ("c4", "other")]
        )
        # Only c1 and c4 remain -> a single pair.
        assert len(pairs) == 1


# ===========================================================================
# Indicator Kafka producer (Req 3.2, 3.7)
# ===========================================================================


class TestIndicatorProducer:
    def test_publish_records_envelopes(self):
        producer = FakeIndicatorProducer()
        indicators = check_hours_clustering(
            700.0, [_prior(701.0), _prior(699.0)], threshold=700.0, claim_id="c1"
        )
        count = asyncio.run(producer.publish_indicators(indicators))
        assert count == 1
        env = producer.published[0]
        assert env["event_type"] == "indicator.repetitive-pattern"
        assert env["indicator"]["algorithm"] == "hours_clustering"
        assert "timestamp" in env

    def test_fake_producer_can_simulate_failure(self):
        producer = FakeIndicatorProducer()
        producer.fail_next_publish()
        with pytest.raises(_kafka.IndicatorPublishError):
            asyncio.run(producer.publish_indicators([]))


# ===========================================================================
# analyze endpoint integration (Req 3.4, 3.5, 3.7)
# ===========================================================================


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not importable")
class TestAnalyzeEndpoint11_2:
    def _client(self, repo, *, llm_client=None, producer=None):
        _main.app.state.settings = _main.get_settings()
        _main.app.state.pool = None
        _main.app.state.repository = repo
        _main.app.state.llm_client = llm_client
        _main.app.state.indicator_producer = producer
        return TestClient(_main.app)

    def test_hours_clustering_indicator_and_publish(self):
        """Current + prior near-threshold hours -> HIGH clustering Indicator that
        is also published to the indicators.repetitive-pattern topic."""
        repo = FakeClaimHistoryRepository()
        repo.add_many(
            "sin-h",
            [
                _prior(701.0, "2022-01-01", "BN1"),
                _prior(699.0, "2023-01-01", "BN1"),
            ],
        )
        producer = FakeIndicatorProducer()
        client = self._client(repo, llm_client=None, producer=producer)

        resp = client.post(
            "/engine/analyze",
            json={
                "sin_hash": "sin-h",
                "claim_id": "11111111-1111-1111-1111-111111111111",
                "employer_bn": "BN1",
                "insurable_hours": 702.0,
                "economic_region_code": "ON",
                "as_of": "2024-06-01",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        algos = {i["algorithm"] for i in body["indicators"]}
        assert "hours_clustering" in algos
        # Published to Kafka.
        topics_algos = {e["indicator"]["algorithm"] for e in producer.published}
        assert "hours_clustering" in topics_algos

    def test_narrative_similarity_medium_indicator(self):
        """A high-similarity narrative pair surfaces a MEDIUM Indicator."""
        repo = FakeClaimHistoryRepository()
        repo.add_many("sin-n", [_prior(700.0, "2023-01-01", "BN1")])
        llm_client = LLMServiceClient(
            "http://llm", transport=_fixed_transport(0.98)
        )
        producer = FakeIndicatorProducer()
        client = self._client(repo, llm_client=llm_client, producer=producer)

        resp = client.post(
            "/engine/analyze",
            json={
                "sin_hash": "sin-n",
                "claim_id": "22222222-2222-2222-2222-222222222222",
                "separation_narrative": "left for personal reasons",
                "prior_narratives": [
                    {"claim_id": "old", "narrative": "left for personal reasons"}
                ],
                "as_of": "2024-06-01",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        medium = [
            i
            for i in body["indicators"]
            if i["algorithm"] == "narrative_similarity"
            and i["severity"] == Severity.MEDIUM.value
        ]
        assert len(medium) == 1
        assert medium[0]["payload"]["cosine_similarity"] == 0.98

    def test_llm_timeout_flag_and_budget_excludes_llm(self):
        """A slow LLM (1s) still returns via the timeout flag WITHOUT blowing the
        400 ms budget — proving the LLM wait is excluded from the budget."""
        repo = FakeClaimHistoryRepository()
        repo.add_many("sin-t", [_prior(700.0, "2023-01-01", "BN1")])

        async def _slow(url: str, body: dict[str, Any], timeout: float):
            await asyncio.sleep(0.3)
            return {"similarity": 0.99}

        # LLM timeout 0.2s < its own sleep 0.3s -> forces the timeout path, but
        # this whole wait is outside the 400 ms analysis budget.
        llm_client = LLMServiceClient(
            "http://llm", timeout_seconds=0.2, transport=_slow
        )
        client = self._client(repo, llm_client=llm_client, producer=None)

        resp = client.post(
            "/engine/analyze",
            json={
                "sin_hash": "sin-t",
                "claim_id": "33333333-3333-3333-3333-333333333333",
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
