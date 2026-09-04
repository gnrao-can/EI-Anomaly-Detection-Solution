"""
tests/unit/test_fraud_ring_engine_13_4.py
==========================================
Task 13.4 — Unit tests for the Fraud Ring Engine boundary scenarios.

This file authors the *specific* boundary cases called out by task 13.4,
distinct from (and non-conflicting with) the broader coverage already provided
by ``test_fraud_ring_engine.py`` (task 13.1) and ``test_fraud_ring_community.py``
(task 13.2). It focuses tightly on the numeric / disposition boundaries that
task 13.4 enumerates:

  * Hours-band cohort size threshold (Requirement 5.7):
      - cohort of exactly 2 (< 3)          -> NOT_EVALUATED (no Indicator)
      - cohort of exactly 3 (== min)       -> EVALUATED (may flag)
  * Coordinated-layoff same-date fraction boundary (Requirements 5.3, 5.4):
      - 79% share the same separation date -> NO flag
      - exactly 80% share the date + ROEs within 24h -> CRITICAL flag
  * Shell-employer registry-record boundary (Requirement 5.3):
      - no Business Registry record        -> HIGH Indicator

Both the Business Registry Adapter and the Graph Service HTTP transports are
mocked (via the engine's dependency-injected fakes / stub HTTP clients); no live
network call is made. The pure detection functions operate on already-fetched
data, so the registry/graph calls are stubbed by construction.

Module isolation uses the shared ``load_service_module`` conftest helper so this
file stays collision-free when the whole unit suite runs in one process.

Requirements: 5.3, 5.4, 5.6, 5.7
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from shared.models import EngineType, Severity
from tests.conftest import load_service_module

# ---------------------------------------------------------------------------
# Load the hyphenated fraud-ring-engine service modules via the shared helper
# (namespaced sys.modules keys -> collision-free on full pytest runs).
# ---------------------------------------------------------------------------

_SIBLINGS = [
    "config",
    "detection",
    "client",
    "community_analysis",
    "graph_client",
    "publisher",
    "main",
]

_detection = load_service_module("fraud-ring-engine", "detection", siblings=_SIBLINGS)
_community_analysis = load_service_module(
    "fraud-ring-engine", "community_analysis", siblings=_SIBLINGS
)
_client = load_service_module("fraud-ring-engine", "client", siblings=_SIBLINGS)
_graph_client = load_service_module(
    "fraud-ring-engine", "graph_client", siblings=_SIBLINGS
)
_publisher = load_service_module("fraud-ring-engine", "publisher", siblings=_SIBLINGS)
_main = load_service_module("fraud-ring-engine", "main", siblings=_SIBLINGS)

# Task 29.1: protected routes now require the INVESTIGATOR role. Inject an
# authenticated principal for the module (auth boundary covered elsewhere).
from shared.auth import get_current_user as _get_current_user  # noqa: E402

_main.app.dependency_overrides[_get_current_user] = lambda: {"roles": ["INVESTIGATOR"]}

# Pure detection functions (operate on already-fetched data — no I/O).
CohortMember = _detection.CohortMember
check_shell_employer = _detection.check_shell_employer
check_coordinated_layoff = _detection.check_coordinated_layoff

# Community / hours-band pure functions.
CohortClaimant = _community_analysis.CohortClaimant
check_hours_band = _community_analysis.check_hours_band

# Mocked-transport clients (Business Registry Adapter + Graph Service).
BusinessRegistryAdapterClient = _client.BusinessRegistryAdapterClient
FakeCohortSource = _client.FakeCohortSource
RegistryLookupStatus = _client.RegistryLookupStatus
GraphServiceClient = _graph_client.GraphServiceClient
InMemoryIndicatorPublisher = _publisher.InMemoryIndicatorPublisher

try:
    from fastapi.testclient import TestClient

    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001
    _HAS_FASTAPI = False


# ---------------------------------------------------------------------------
# Shared constants / helpers
# ---------------------------------------------------------------------------

_SEPARATION = date(2024, 3, 1)
_MIN_QUALIFYING_HOURS = 700.0


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def _cohort_member(sin: str, sep: date, roe_ts: datetime) -> Any:
    return CohortMember(sin_hash=sin, separation_date=sep, roe_submission_ts=roe_ts)


def _claimant(sin: str, hours: float) -> Any:
    return CohortClaimant(sin_hash=sin, insurable_hours=hours)


def _same_date_cohort(same_count: int, other_count: int) -> list[Any]:
    """Build a cohort where ``same_count`` share 2024-03-01 with ROEs inside 24h.

    The ``other_count`` members each get a distinct earlier separation date so
    they do not inflate the dominant same-date group.
    """
    base_ts = datetime(2024, 3, 1, 9, 0, tzinfo=timezone.utc)
    members: list[Any] = []
    # Distribute the same-date group's ROE submissions evenly across a tight
    # 12-hour window so the whole group always fits inside the rolling 24h
    # window regardless of ``same_count``.
    window_minutes = 12 * 60
    step = window_minutes / max(same_count - 1, 1)
    for i in range(same_count):
        members.append(
            _cohort_member(
                f"same-{i}", _SEPARATION, base_ts + timedelta(minutes=step * i)
            )
        )
    for j in range(other_count):
        members.append(
            _cohort_member(
                f"other-{j}",
                date(2024, 2, 1) + timedelta(days=j),
                base_ts + timedelta(days=5 + j),
            )
        )
    return members


# ===========================================================================
# Hours-band cohort-size boundary (Requirement 5.7 / 5.6)
# ===========================================================================


class TestHoursBandCohortSizeBoundary:
    def test_cohort_of_two_is_not_evaluated(self):
        """cohort < 3 -> NOT_EVALUATED disposition, no Indicator (Req 5.7)."""
        cohort = [
            _claimant("s0", 705.0),
            _claimant("s1", 708.0),
        ]
        result = check_hours_band(cohort, _MIN_QUALIFYING_HOURS)
        assert result.indicators == []
        assert result.disposition == "NOT_EVALUATED"
        assert result.detail.startswith("COHORT_BELOW_MIN")

    def test_cohort_of_three_is_evaluated_and_flags(self):
        """cohort == 3 (== min) crosses the boundary -> EVALUATED, HIGH flag (Req 5.6)."""
        cohort = [
            _claimant("s0", 705.0),
            _claimant("s1", 708.0),
            _claimant("s2", 712.0),
        ]
        result = check_hours_band(cohort, _MIN_QUALIFYING_HOURS)
        assert result.disposition == "EVALUATED"
        assert len(result.indicators) == 1
        ind = result.indicators[0]
        assert ind.engine == EngineType.FRAUD_RING
        assert ind.severity == Severity.HIGH
        assert ind.algorithm == "cohort_hours_band"
        assert ind.payload["cohort_size"] == 3
        assert ind.payload["min_qualifying_hours"] == _MIN_QUALIFYING_HOURS


# ===========================================================================
# Coordinated-layoff same-date fraction boundary (Req 5.3, 5.4)
# ===========================================================================


class TestCoordinatedLayoffFractionBoundary:
    def test_79_percent_same_date_no_flag(self):
        """79% share the separation date -> below the 80% threshold -> no flag."""
        # 79 of 100 share 2024-03-01.
        cohort = _same_date_cohort(same_count=79, other_count=21)
        assert check_coordinated_layoff(cohort) == []

    def test_80_percent_same_date_within_24h_is_critical(self):
        """Exactly 80% share the date AND ROEs within 24h -> CRITICAL (Req 5.4)."""
        # 80 of 100 share 2024-03-01, ROEs spread across a 12h window (< 24h).
        cohort = _same_date_cohort(same_count=80, other_count=20)
        indicators = check_coordinated_layoff(cohort)
        assert len(indicators) == 1
        ind = indicators[0]
        assert ind.engine == EngineType.FRAUD_RING
        assert ind.severity == Severity.CRITICAL
        assert ind.algorithm == "coordinated_layoff"
        # Indicator carries separation date, total count, and ROE span (Req 5.4).
        assert ind.payload["shared_separation_date"] == "2024-03-01"
        assert ind.payload["same_date_count"] == 80
        assert ind.payload["cohort_size"] == 100
        assert ind.payload["same_date_fraction"] == pytest.approx(0.80)
        assert "roe_span_hours" in ind.payload

    def test_small_cohort_four_of_five_is_80_percent_flag(self):
        """A small 4/5 cohort is exactly 80% -> CRITICAL, confirming the ratio math."""
        cohort = _same_date_cohort(same_count=4, other_count=1)
        indicators = check_coordinated_layoff(cohort)
        assert len(indicators) == 1
        assert indicators[0].severity == Severity.CRITICAL
        assert indicators[0].payload["same_date_fraction"] == pytest.approx(0.80)


# ===========================================================================
# Shell-employer: no Business Registry record -> HIGH (Req 5.3)
# ===========================================================================


class TestNoBusinessRegistryRecord:
    def test_no_record_pure_function_is_high(self):
        """A ``None`` registration (no registry record) -> HIGH Indicator (Req 5.3)."""
        indicators = check_shell_employer(None, _SEPARATION, employer_bn="123456789RT0001")
        assert len(indicators) == 1
        ind = indicators[0]
        assert ind.engine == EngineType.FRAUD_RING
        assert ind.severity == Severity.HIGH
        assert ind.algorithm == "shell_employer_no_record"
        assert ind.payload["reason"] == "NO_REGISTRY_RECORD"
        assert ind.payload["employer_bn"] == "123456789RT0001"

    def test_registry_adapter_no_record_then_high_indicator(self):
        """End-to-end: mocked Business Registry Adapter says NO_RECORD -> HIGH.

        The Business Registry Adapter HTTP call is mocked with a stub transport
        that returns an empty ``registrations`` list, so the client resolves
        NO_RECORD and the pure detector emits a HIGH Indicator.
        """
        body = {
            "bn": "123456789RT0001",
            "status": "OK",
            "registrations": [],
            "unreachable": [],
        }
        client = BusinessRegistryAdapterClient("http://reg", _FakeHttp(_FakeResponse(body)))
        lookup = _run(client.lookup_employer("123456789RT0001"))
        assert lookup.status is RegistryLookupStatus.NO_RECORD

        # NO_RECORD -> pass ``None`` registration to the detector.
        indicators = check_shell_employer(
            None if lookup.registration is None else lookup.registration,
            _SEPARATION,
            employer_bn="123456789RT0001",
        )
        assert len(indicators) == 1
        assert indicators[0].severity == Severity.HIGH
        assert indicators[0].payload["reason"] == "NO_REGISTRY_RECORD"


# ---------------------------------------------------------------------------
# Minimal mocked HTTP transports (Business Registry Adapter / Graph Service)
# ---------------------------------------------------------------------------


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

    async def post(self, url: str, *args: Any, **kwargs: Any) -> Any:
        if self._raise:
            raise RuntimeError("connection refused")
        return self._response


# ===========================================================================
# /engine/analyze endpoint boundaries (Business Registry + Graph mocked)
# ===========================================================================


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not importable")
class TestAnalyzeEndpointBoundaries:
    def _client(
        self, *, registry_client=None, graph_client=None, publisher=None
    ) -> "TestClient":
        _main.app.state.settings = _main.get_settings()
        _main.app.state.http_client = None
        _main.app.state.registry_client = registry_client
        _main.app.state.cohort_source = None
        _main.app.state.graph_client = graph_client
        _main.app.state.publisher = publisher or InMemoryIndicatorPublisher()
        return TestClient(_main.app)

    def test_no_registry_record_endpoint_emits_high_indicator(self):
        """Mocked Business Registry Adapter -> no record -> HIGH Indicator (Req 5.3)."""
        reg_body = {
            "bn": "123456789RT0001",
            "status": "OK",
            "registrations": [],
            "unreachable": [],
        }
        registry_client = BusinessRegistryAdapterClient(
            "http://reg", _FakeHttp(_FakeResponse(reg_body))
        )
        client = self._client(registry_client=registry_client)
        resp = client.post(
            "/engine/analyze",
            json={
                "employer_bn": "123456789RT0001",
                "separation_date": "2024-03-01",
                "cohort": [],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        shell = [
            i for i in body["indicators"] if i["algorithm"] == "shell_employer_no_record"
        ]
        assert len(shell) == 1
        assert shell[0]["severity"] == "HIGH"

    def test_small_graph_cohort_hours_band_not_evaluated_endpoint(self):
        """Graph Service mocked; cohort of 2 -> hours-band NOT_EVALUATED (Req 5.7)."""
        graph_body = {"partition": {"s0": 0, "s1": 0}, "num_communities": 1}
        graph_client = GraphServiceClient(
            "http://graph", _FakeHttp(_FakeResponse(graph_body))
        )
        client = self._client(graph_client=graph_client)
        resp = client.post(
            "/engine/analyze",
            json={
                "employer_bn": "123456789RT0001",
                "separation_date": "2024-03-01",
                "graph_cohort": [
                    {"sin_hash": "s0", "postal_fsa": "K1S", "insurable_hours": 705},
                    {"sin_hash": "s1", "postal_fsa": "K1S", "insurable_hours": 706},
                ],
                "min_qualifying_hours": 700.0,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["dispositions"]["hours_band"] == "NOT_EVALUATED"
        assert not any(
            i["algorithm"] == "cohort_hours_band" for i in body["indicators"]
        )
