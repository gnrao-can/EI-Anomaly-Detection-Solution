"""
tests/unit/test_fraud_ring_community.py
=========================================
Unit + property tests for the Fraud Ring Engine community-detection and
hours-band analysis (task 13.2).

Coverage:
  * Shared-attribute cluster (Requirement 5.5):
      - >= 40% share a postal FSA                -> CRITICAL (matched attr/value/ids)
      - >= 40% share a surname_hash              -> CRITICAL
      - 39% share -> no flag; 40% boundary -> flag
      - empty cohort                             -> NOT_EVALUATED
  * Hours-band (Requirements 5.6, 5.7):
      - cohort < 3                               -> NOT_EVALUATED
      - cohort >= 3, all within 10h AND >= min   -> HIGH (band range + threshold)
      - within 10h but below threshold           -> no flag
      - spread > 10h                             -> no flag
  * Graph Service client (mocked transport, no live network):
      - construct OK / community-detect partition parsing
      - transport failure / non-2xx -> UNAVAILABLE
  * Publisher: InMemoryIndicatorPublisher captures Indicators for the topic.
  * ``/engine/analyze`` endpoint (DI fakes):
      - graph_cohort produces community + hours-band Indicators and publishes
      - 800 ms budget: stale received_at -> checks skipped, partial results

Property (design correctness): the shared-attribute cluster flag fires iff the
dominant attribute group covers >= 40% of the cohort.

Requirements: 5.5, 5.6, 5.7, 5.8, 5.9
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from shared.models import EngineType, Severity
from tests.conftest import load_service_module

# ---------------------------------------------------------------------------
# Load the hyphenated fraud-ring-engine service modules via the shared helper
# (namespaced sys.modules keys -> collision-free on full pytest runs).
# ---------------------------------------------------------------------------

_SIBLINGS = ["config", "detection", "client", "community_analysis", "graph_client",
             "publisher", "main"]

_community_analysis = load_service_module(
    "fraud-ring-engine", "community_analysis", siblings=_SIBLINGS
)
_graph_client = load_service_module(
    "fraud-ring-engine", "graph_client", siblings=_SIBLINGS
)
_publisher = load_service_module(
    "fraud-ring-engine", "publisher", siblings=_SIBLINGS
)
_main = load_service_module("fraud-ring-engine", "main", siblings=_SIBLINGS)

# Task 29.1: protected routes now require the INVESTIGATOR role via the shared
# GCKey dependency. Inject an authenticated principal for the whole module so
# these handler tests bypass the auth gate (the 401/403 boundary is covered in
# tests/unit/test_auth_service_wiring.py).
from shared.auth import get_current_user as _get_current_user  # noqa: E402

_main.app.dependency_overrides[_get_current_user] = lambda: {"roles": ["INVESTIGATOR"]}

CohortClaimant = _community_analysis.CohortClaimant
check_shared_attribute_cluster = _community_analysis.check_shared_attribute_cluster
check_hours_band = _community_analysis.check_hours_band

GraphServiceClient = _graph_client.GraphServiceClient
GraphCallStatus = _graph_client.GraphCallStatus

InMemoryIndicatorPublisher = _publisher.InMemoryIndicatorPublisher
FRAUD_RING_TOPIC = _publisher.FRAUD_RING_TOPIC

try:
    from fastapi.testclient import TestClient

    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001
    _HAS_FASTAPI = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _claimant(
    sin: str,
    *,
    fsa: str | None = None,
    surname: str | None = None,
    hours: float | None = None,
) -> Any:
    return CohortClaimant(
        sin_hash=sin,
        postal_fsa=fsa,
        surname_hash=surname,
        insurable_hours=hours,
    )


def _run(coro):
    import asyncio

    return asyncio.run(coro)


# ===========================================================================
# Shared-attribute cluster (Req 5.5)
# ===========================================================================


class TestSharedAttributeCluster:
    def test_40_percent_share_postal_fsa_is_critical(self):
        # 4/10 share "K1S" -> exactly 40% -> flag.
        cohort = [_claimant(f"s{i}", fsa="K1S") for i in range(4)]
        cohort += [_claimant(f"o{i}", fsa=f"M{i}A") for i in range(6)]
        result = check_shared_attribute_cluster(cohort)
        assert len(result.indicators) == 1
        ind = result.indicators[0]
        assert ind.engine == EngineType.FRAUD_RING
        assert ind.severity == Severity.CRITICAL
        assert ind.algorithm == "community_shared_attribute"
        assert ind.payload["matched_attribute_type"] == "postal_fsa"
        assert ind.payload["matched_value"] == "K1S"
        assert ind.payload["cluster_size"] == 4
        assert ind.payload["cohort_size"] == 10
        assert sorted(ind.payload["claimant_ids"]) == ["s0", "s1", "s2", "s3"]

    def test_40_percent_share_surname_hash_is_critical(self):
        cohort = [_claimant(f"s{i}", surname="HASH_SMITH") for i in range(4)]
        cohort += [_claimant(f"o{i}", surname=f"H{i}") for i in range(6)]
        result = check_shared_attribute_cluster(cohort)
        assert len(result.indicators) == 1
        assert result.indicators[0].payload["matched_attribute_type"] == "surname_hash"
        assert result.indicators[0].payload["matched_value"] == "HASH_SMITH"

    def test_39_percent_no_flag(self):
        # 39/100 share -> below 40% -> no flag.
        cohort = [_claimant(f"s{i}", fsa="K1S") for i in range(39)]
        cohort += [_claimant(f"o{i}", fsa=f"X{i:03d}") for i in range(61)]
        result = check_shared_attribute_cluster(cohort)
        assert result.indicators == []
        assert result.disposition == "EVALUATED"

    def test_empty_cohort_not_evaluated(self):
        result = check_shared_attribute_cluster([])
        assert result.indicators == []
        assert result.disposition == "NOT_EVALUATED"

    def test_fsa_normalised_case_and_spaces(self):
        cohort = [
            _claimant("s0", fsa="k1s"),
            _claimant("s1", fsa="K1S 2B3"),
            _claimant("s2", fsa=" k1s"),
            _claimant("s3", fsa="M5V"),
        ]
        result = check_shared_attribute_cluster(cohort)
        assert len(result.indicators) == 1
        assert result.indicators[0].payload["matched_value"] == "K1S"
        assert result.indicators[0].payload["cluster_size"] == 3

    @hyp_settings(max_examples=100, deadline=None)
    @given(
        share=st.integers(min_value=0, max_value=20),
        other=st.integers(min_value=0, max_value=20),
    )
    def test_property_fires_iff_dominant_group_ge_40pct(self, share, other):
        """P: cluster flag fires iff the dominant group covers >= 40% of cohort.

        Validates: Requirements 5.5
        """
        total = share + other
        if total == 0:
            return
        # Give the "other" members all-distinct FSAs so the shared group is
        # exactly ``share`` members on "AAA".
        cohort = [_claimant(f"s{i}", fsa="AAA") for i in range(share)]
        cohort += [_claimant(f"o{i}", fsa=f"Z{i:02d}") for i in range(other)]
        result = check_shared_attribute_cluster(cohort)
        # Dominant group size is max(share, 1-per-other) -> share when share>=1.
        dominant = max([share] + [1] * (1 if other else 0)) if cohort else 0
        expected_fires = (dominant / total) >= 0.40
        assert bool(result.indicators) == expected_fires


# ===========================================================================
# Hours-band (Req 5.6, 5.7)
# ===========================================================================


class TestHoursBand:
    def test_cohort_below_3_not_evaluated(self):
        cohort = [_claimant("s0", hours=710.0), _claimant("s1", hours=712.0)]
        result = check_hours_band(cohort, 700.0)
        assert result.indicators == []
        assert result.disposition == "NOT_EVALUATED"
        assert result.detail.startswith("COHORT_BELOW_MIN")

    def test_all_within_10h_above_threshold_is_high(self):
        cohort = [
            _claimant("s0", hours=705.0),
            _claimant("s1", hours=710.0),
            _claimant("s2", hours=714.0),
        ]
        result = check_hours_band(cohort, 700.0)
        assert len(result.indicators) == 1
        ind = result.indicators[0]
        assert ind.severity == Severity.HIGH
        assert ind.algorithm == "cohort_hours_band"
        assert ind.payload["band_lower_hours"] == 705.0
        assert ind.payload["band_upper_hours"] == 714.0
        assert ind.payload["band_span_hours"] == 9.0
        assert ind.payload["min_qualifying_hours"] == 700.0
        assert result.disposition == "EVALUATED"

    def test_within_10h_but_below_threshold_no_flag(self):
        cohort = [
            _claimant("s0", hours=695.0),
            _claimant("s1", hours=700.0),
            _claimant("s2", hours=703.0),
        ]
        result = check_hours_band(cohort, 700.0)
        assert result.indicators == []
        assert result.disposition == "EVALUATED"

    def test_spread_over_10h_no_flag(self):
        cohort = [
            _claimant("s0", hours=705.0),
            _claimant("s1", hours=712.0),
            _claimant("s2", hours=730.0),
        ]
        result = check_hours_band(cohort, 700.0)
        assert result.indicators == []

    def test_exact_10h_band_at_threshold_is_high(self):
        cohort = [
            _claimant("s0", hours=700.0),
            _claimant("s1", hours=705.0),
            _claimant("s2", hours=710.0),
        ]
        result = check_hours_band(cohort, 700.0)
        assert len(result.indicators) == 1
        assert result.indicators[0].payload["band_span_hours"] == 10.0

    def test_missing_hours_data_not_evaluated(self):
        cohort = [
            _claimant("s0", hours=705.0),
            _claimant("s1", hours=None),
            _claimant("s2", hours=710.0),
        ]
        result = check_hours_band(cohort, 700.0)
        assert result.indicators == []
        assert result.disposition == "NOT_EVALUATED"
        assert result.detail == "INSUFFICIENT_HOURS_DATA"


# ===========================================================================
# Graph Service client (mocked transport)
# ===========================================================================


class _FakeResponse:
    def __init__(self, body: Any, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code

    def json(self) -> Any:
        return self._body


class _FakeHttp:
    def __init__(self, response: Any = None, *, raise_exc: bool = False) -> None:
        self._response = response
        self._raise = raise_exc
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append({"url": url, **kwargs})
        if self._raise:
            raise RuntimeError("connection refused")
        return self._response


class TestGraphServiceClient:
    def test_construct_ok(self):
        http = _FakeHttp(_FakeResponse({"status": "ok"}))
        client = GraphServiceClient("http://graph", http)
        result = _run(client.construct({"sin_hash": "s0", "employer_bn": "BN1"}))
        assert result.status is GraphCallStatus.OK
        assert http.calls[0]["url"].endswith("/graph/construct")

    def test_community_detect_parses_partition(self):
        body = {"partition": {"s0": 0, "s1": 0, "s2": 1}, "num_communities": 2}
        client = GraphServiceClient("http://graph", _FakeHttp(_FakeResponse(body)))
        result = _run(client.community_detect(edges=[("s0", "BN1"), ("s1", "BN1")]))
        assert result.status is GraphCallStatus.OK
        assert result.partition == {"s0": 0, "s1": 0, "s2": 1}
        assert result.num_communities == 2

    def test_transport_failure_is_unavailable(self):
        client = GraphServiceClient("http://graph", _FakeHttp(raise_exc=True))
        result = _run(client.community_detect(employer_bns=["BN1"]))
        assert result.status is GraphCallStatus.UNAVAILABLE

    def test_5xx_is_unavailable(self):
        client = GraphServiceClient(
            "http://graph", _FakeHttp(_FakeResponse({}, status_code=503))
        )
        result = _run(client.construct({"sin_hash": "s0", "employer_bn": "BN1"}))
        assert result.status is GraphCallStatus.UNAVAILABLE


# ===========================================================================
# Publisher
# ===========================================================================


class TestPublisher:
    def test_in_memory_publisher_captures_indicators(self):
        cohort = [_claimant(f"s{i}", fsa="K1S") for i in range(4)]
        cohort += [_claimant(f"o{i}", fsa=f"M{i}A") for i in range(6)]
        indicators = check_shared_attribute_cluster(cohort).indicators
        pub = InMemoryIndicatorPublisher()
        count = _run(pub.publish(indicators))
        assert count == 1
        assert pub.topic == FRAUD_RING_TOPIC
        assert len(pub.published) == 1
        assert pub.records[0]["engine"] == "FRAUD_RING"


# ===========================================================================
# /engine/analyze endpoint (DI fakes, no live network)
# ===========================================================================


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not importable")
class TestAnalyzeEndpointCommunity:
    def _client(
        self, *, graph_client=None, publisher=None
    ) -> "TestClient":
        _main.app.state.settings = _main.get_settings()
        _main.app.state.http_client = None
        _main.app.state.registry_client = None
        _main.app.state.cohort_source = None
        _main.app.state.graph_client = graph_client
        _main.app.state.publisher = publisher or InMemoryIndicatorPublisher()
        return TestClient(_main.app)

    def test_graph_cohort_produces_community_and_hours_band_indicators(self):
        pub = InMemoryIndicatorPublisher()
        graph_body = {
            "partition": {f"s{i}": 0 for i in range(4)},
            "num_communities": 1,
        }
        graph_client = GraphServiceClient(
            "http://graph", _FakeHttp(_FakeResponse(graph_body))
        )
        client = self._client(graph_client=graph_client, publisher=pub)

        # 4 claimants share FSA "K1S" (100% -> cluster) and all hours in 8h band.
        graph_cohort = [
            {"sin_hash": f"s{i}", "postal_fsa": "K1S", "insurable_hours": 705 + i}
            for i in range(4)
        ]
        resp = client.post(
            "/engine/analyze",
            json={
                "employer_bn": "BN1",
                "separation_date": "2024-03-01",
                "graph_cohort": graph_cohort,
                "min_qualifying_hours": 700.0,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        algos = {i["algorithm"] for i in body["indicators"]}
        assert "community_shared_attribute" in algos
        assert "cohort_hours_band" in algos
        assert "community_cluster" in body["checks_run"]
        assert "hours_band" in body["checks_run"]
        assert body["num_communities"] == 1
        assert body["graph_cohort_size"] == 4
        assert body["published_count"] == len(body["indicators"])
        assert len(pub.published) == len(body["indicators"])

    def test_small_graph_cohort_hours_band_not_evaluated(self):
        client = self._client()
        graph_cohort = [
            {"sin_hash": "s0", "postal_fsa": "K1S", "insurable_hours": 705},
            {"sin_hash": "s1", "postal_fsa": "K1S", "insurable_hours": 706},
        ]
        resp = client.post(
            "/engine/analyze",
            json={
                "employer_bn": "BN1",
                "separation_date": "2024-03-01",
                "graph_cohort": graph_cohort,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["dispositions"]["hours_band"] == "NOT_EVALUATED"
        assert any(
            s.startswith("hours_band:NOT_EVALUATED") for s in body["checks_skipped"]
        )

    def test_budget_exceeded_skips_community_checks(self):
        client = self._client()
        # A received_at well in the past forces the 800 ms budget to be exceeded.
        stale = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        resp = client.post(
            "/engine/analyze",
            json={
                "employer_bn": "BN1",
                "separation_date": "2024-03-01",
                "received_at": stale,
                "graph_cohort": [
                    {"sin_hash": f"s{i}", "postal_fsa": "K1S", "insurable_hours": 705}
                    for i in range(4)
                ],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["budget_exceeded"] is True
        assert any(
            s == "community_cluster:BUDGET_EXCEEDED" for s in body["checks_skipped"]
        )
        # No community/hours indicators when the budget was already blown.
        algos = {i["algorithm"] for i in body["indicators"]}
        assert "community_shared_attribute" not in algos
        assert "cohort_hours_band" not in algos

    def test_publish_failure_does_not_crash(self):
        pub = InMemoryIndicatorPublisher()
        pub.fail = True
        client = self._client(publisher=pub)
        resp = client.post(
            "/engine/analyze",
            json={
                "employer_bn": "BN1",
                "separation_date": "2024-03-01",
                "graph_cohort": [
                    {"sin_hash": f"s{i}", "postal_fsa": "K1S", "insurable_hours": 705}
                    for i in range(4)
                ],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["published_count"] == 0
        assert any(s == "publish:KAFKA_UNAVAILABLE" for s in body["checks_skipped"])
