"""
tests/unit/test_shap_explainer.py
=================================
Unit tests for the SHAP attribution implementation (task 7.2):

  - ``assert_additivity`` helper: passes within tolerance, raises when violated
  - ``ShapAttributor.explain``: additivity holds for various Indicator sets
    (including empty), and produces exactly one ShapAttribution per Indicator
  - Deterministic linear fallback: same input -> same contributions
  - ``EnsembleScorer.assess``: wires SHAP into the scoring flow, asserts
    additivity, leaves LLM explanation fields empty, and yields a persistable
    RiskAssessment
  - ``RiskAssessmentRepository`` fake/test-mode captures the persisted record

The property-based additivity test (P7) lives in task 7.3, not here.

The ensemble-scorer service directory contains a hyphen, so it is not importable
as a package; we add it to ``sys.path`` (as the sibling scorer tests do).

Requirements: 9.3
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

# --- Make the ensemble-scorer service importable -----------------------------
# The service imports its siblings by BARE name (``from repository import ...``).
# When the whole unit suite runs in one process, another hyphenated service may
# have already registered a bare ``repository`` / ``features`` / ``scorer`` /
# ``shap_explainer`` in sys.modules, so we evict any stale bare-name entries
# first and prepend this service dir so the correct modules load from
# ensemble-scorer.
_SERVICE_DIR = (
    Path(__file__).resolve().parents[2] / "services" / "ensemble-scorer"
)
if str(_SERVICE_DIR) in sys.path:
    sys.path.remove(str(_SERVICE_DIR))
sys.path.insert(0, str(_SERVICE_DIR))
for _bare in ("features", "scorer", "shap_explainer", "repository"):
    sys.modules.pop(_bare, None)

from shared.models import (  # noqa: E402
    EngineType,
    Indicator,
    RiskAssessment,
    RiskDecision,
    Severity,
    ShapAttribution,
)

from features import DEFAULT_ENCODER  # noqa: E402
from scorer import EnsembleScorer  # noqa: E402
from shap_explainer import (  # noqa: E402
    ADDITIVITY_TOLERANCE,
    AdditivityError,
    ShapAttributor,
    assert_additivity,
)
from repository import (  # noqa: E402
    FakeRiskAssessmentRepository,
    RiskAssessmentRepository,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_indicator(
    engine: EngineType = EngineType.REPETITIVE_PATTERN,
    severity: Severity = Severity.HIGH,
    claim_id: uuid.UUID | None = None,
) -> Indicator:
    return Indicator(
        indicator_id=uuid.uuid4(),
        claim_id=claim_id or uuid.uuid4(),
        engine=engine,
        severity=severity,
        algorithm="test",
        title="test",
        explanation_en="test",
        explanation_fr="test",
        payload={},
        produced_at=datetime.now(timezone.utc),
    )


# Representative Indicator sets, including the empty set.
INDICATOR_SETS: list[list[Indicator]] = [
    [],
    [make_indicator(EngineType.FRAUD_RING, Severity.CRITICAL)],
    [
        make_indicator(EngineType.REPETITIVE_PATTERN, Severity.LOW),
        make_indicator(EngineType.REPETITIVE_PATTERN, Severity.HIGH),
    ],
    [make_indicator(e, Severity.MEDIUM) for e in EngineType],
    [make_indicator(EngineType.GEOGRAPHIC_ANOMALY, Severity.UNRESOLVED)],
]


# ---------------------------------------------------------------------------
# assert_additivity helper
# ---------------------------------------------------------------------------


class TestAssertAdditivity:
    def test_passes_when_sum_matches(self):
        # sum == risk_score - base_value exactly.
        assert_additivity([10.0, 5.0], risk_score=20, base_value=5.0)

    def test_passes_within_tolerance(self):
        # Off by less than 1e-4.
        assert_additivity([15.00005], risk_score=20, base_value=5.0)

    def test_empty_contributions_with_zero_target(self):
        assert_additivity([], risk_score=0, base_value=0.0)

    def test_raises_when_violated(self):
        with pytest.raises(AdditivityError):
            assert_additivity([1.0, 2.0], risk_score=90, base_value=0.0)

    def test_raises_just_outside_tolerance(self):
        # Off by more than 1e-4.
        with pytest.raises(AdditivityError):
            assert_additivity([15.001], risk_score=20, base_value=5.0)


# ---------------------------------------------------------------------------
# ShapAttributor.explain
# ---------------------------------------------------------------------------


class TestShapAttributorExplain:
    @pytest.mark.parametrize("indicators", INDICATOR_SETS)
    def test_additivity_holds(self, indicators):
        attributor = ShapAttributor()  # fallback mode (no model)
        risk_score = 3 * len(indicators)  # arbitrary deterministic score
        attributions, base_value = attributor.explain(
            DEFAULT_ENCODER.encode(indicators), indicators, risk_score
        )
        total = sum(a.contribution for a in attributions)
        assert abs(total - (risk_score - base_value)) < ADDITIVITY_TOLERANCE

    @pytest.mark.parametrize("indicators", INDICATOR_SETS)
    def test_attribution_count_matches_indicator_count(self, indicators):
        attributor = ShapAttributor()
        # An empty Indicator set implies a zero score (P8); every non-empty set
        # gets an arbitrary in-range score.
        risk_score = 42 if indicators else 0
        attributions, _ = attributor.explain(
            DEFAULT_ENCODER.encode(indicators), indicators, risk_score=risk_score
        )
        assert len(attributions) == len(indicators)

    def test_empty_indicators_produce_no_attributions(self):
        attributor = ShapAttributor()
        attributions, base_value = attributor.explain(
            DEFAULT_ENCODER.encode([]), [], risk_score=0
        )
        assert attributions == []
        assert base_value == 0.0

    def test_attribution_references_source_indicator(self):
        attributor = ShapAttributor()
        indicators = [
            make_indicator(EngineType.FRAUD_RING, Severity.CRITICAL),
            make_indicator(EngineType.DOCUMENT_FORGERY, Severity.HIGH),
        ]
        attributions, _ = attributor.explain(
            DEFAULT_ENCODER.encode(indicators), indicators, risk_score=60
        )
        attributed_ids = {a.indicator_id for a in attributions}
        assert attributed_ids == {i.indicator_id for i in indicators}

    def test_fallback_is_active_without_model(self):
        attributor = ShapAttributor()
        assert attributor.using_fallback is True

    def test_fallback_is_deterministic(self):
        # Reuse the SAME indicator objects for identical input.
        indicators = [
            make_indicator(EngineType.FRAUD_RING, Severity.CRITICAL),
            make_indicator(EngineType.REGIONAL_ANALYSIS, Severity.MEDIUM),
        ]
        vec = DEFAULT_ENCODER.encode(indicators)
        a1, b1 = ShapAttributor().explain(vec, indicators, risk_score=55)
        a2, b2 = ShapAttributor().explain(vec, indicators, risk_score=55)
        assert b1 == b2
        assert [x.contribution for x in a1] == [x.contribution for x in a2]

    def test_high_risk_score_additivity(self):
        attributor = ShapAttributor()
        indicators = [make_indicator(EngineType.FRAUD_RING, Severity.CRITICAL)] * 1
        attributions, base_value = attributor.explain(
            DEFAULT_ENCODER.encode(indicators), indicators, risk_score=100
        )
        total = sum(a.contribution for a in attributions)
        assert abs(total - (100 - base_value)) < ADDITIVITY_TOLERANCE


# ---------------------------------------------------------------------------
# EnsembleScorer.assess — SHAP wired into scoring flow
# ---------------------------------------------------------------------------


@pytest.fixture
def heuristic_scorer(tmp_path) -> EnsembleScorer:
    return EnsembleScorer(model_dir=tmp_path / "no_models")


class TestEnsembleScorerAssess:
    def test_assess_returns_risk_assessment(self, heuristic_scorer):
        indicators = [make_indicator(EngineType.FRAUD_RING, Severity.CRITICAL)]
        assessment = heuristic_scorer.assess(indicators)
        assert isinstance(assessment, RiskAssessment)
        assert 0 <= assessment.risk_score <= 100
        assert isinstance(assessment.risk_decision, RiskDecision)

    def test_assess_additivity_invariant(self, heuristic_scorer):
        indicators = [
            make_indicator(EngineType.FRAUD_RING, Severity.CRITICAL),
            make_indicator(EngineType.GEOGRAPHIC_ANOMALY, Severity.HIGH),
            make_indicator(EngineType.DOCUMENT_FORGERY, Severity.MEDIUM),
        ]
        assessment = heuristic_scorer.assess(indicators)
        total = sum(a.contribution for a in assessment.shap_attributions)
        expected = assessment.risk_score - assessment.shap_base_value
        assert abs(total - expected) < ADDITIVITY_TOLERANCE

    def test_assess_empty_is_zero_auto_approve(self, heuristic_scorer):
        assessment = heuristic_scorer.assess([])
        assert assessment.risk_score == 0
        assert assessment.risk_decision is RiskDecision.AUTO_APPROVE
        assert assessment.shap_attributions == []

    def test_assess_leaves_explanations_empty(self, heuristic_scorer):
        indicators = [make_indicator(EngineType.FRAUD_RING, Severity.CRITICAL)]
        assessment = heuristic_scorer.assess(indicators)
        assert assessment.explanation_en == ""
        assert assessment.explanation_fr == ""

    def test_assess_uses_shared_claim_id(self, heuristic_scorer):
        claim_id = uuid.uuid4()
        indicators = [
            make_indicator(EngineType.FRAUD_RING, Severity.HIGH, claim_id=claim_id),
            make_indicator(EngineType.REGIONAL_ANALYSIS, Severity.LOW, claim_id=claim_id),
        ]
        assessment = heuristic_scorer.assess(indicators)
        assert assessment.claim_id == claim_id

    def test_assess_one_attribution_per_indicator(self, heuristic_scorer):
        indicators = [make_indicator(e, Severity.HIGH) for e in EngineType]
        assessment = heuristic_scorer.assess(indicators)
        assert len(assessment.shap_attributions) == len(indicators)

    def test_assess_records_engines_completed(self, heuristic_scorer):
        indicators = [
            make_indicator(EngineType.FRAUD_RING, Severity.HIGH),
            make_indicator(EngineType.DOCUMENT_FORGERY, Severity.HIGH),
        ]
        assessment = heuristic_scorer.assess(
            indicators, engines_partial=[EngineType.REGIONAL_ANALYSIS]
        )
        assert set(assessment.engines_completed) == {
            EngineType.FRAUD_RING,
            EngineType.DOCUMENT_FORGERY,
        }
        assert assessment.engines_partial == [EngineType.REGIONAL_ANALYSIS]


# ---------------------------------------------------------------------------
# RiskAssessmentRepository — test-mode / fake persistence
# ---------------------------------------------------------------------------


class TestRiskAssessmentRepository:
    def _build_assessment(self) -> RiskAssessment:
        scorer = EnsembleScorer(model_dir=Path("/nonexistent"))
        indicators = [make_indicator(EngineType.FRAUD_RING, Severity.CRITICAL)]
        return scorer.assess(indicators)

    def test_fake_repository_captures_persisted(self):
        repo = FakeRiskAssessmentRepository()
        assessment = self._build_assessment()
        asyncio.run(repo.persist(assessment))
        assert repo.count == 1
        assert repo.last() is assessment

    def test_real_repository_no_clients_is_noop(self):
        # With both clients None, persist should skip cleanly (dev/degraded mode).
        repo = RiskAssessmentRepository(pg_pool=None, clickhouse_client=None)
        assessment = self._build_assessment()
        # Should not raise.
        asyncio.run(repo.persist(assessment))

    def test_real_repository_writes_to_fake_clients(self):
        # Fake asyncpg pool + connection capturing executed statements.
        class _FakeConn:
            def __init__(self):
                self.executed = []
                self.many = []

            async def execute(self, sql, *args):
                self.executed.append((sql, args))

            async def executemany(self, sql, rows):
                self.many.append((sql, list(rows)))

            def transaction(self):
                conn = self

                class _Tx:
                    async def __aenter__(self):
                        return conn

                    async def __aexit__(self, *exc):
                        return False

                return _Tx()

        class _FakePool:
            def __init__(self, conn):
                self._conn = conn

            def acquire(self):
                conn = self._conn

                class _Acq:
                    async def __aenter__(self):
                        return conn

                    async def __aexit__(self, *exc):
                        return False

                return _Acq()

        class _FakeClickHouse:
            def __init__(self):
                self.inserts = []

            def insert(self, table, rows, column_names=None):
                self.inserts.append((table, rows, column_names))

        conn = _FakeConn()
        ch = _FakeClickHouse()
        repo = RiskAssessmentRepository(
            pg_pool=_FakePool(conn), clickhouse_client=ch
        )
        assessment = self._build_assessment()
        asyncio.run(repo.persist(assessment))

        # PostgreSQL: one assessment insert + one executemany for attributions.
        assert any("risk_assessments" in sql for sql, _ in conn.executed)
        assert conn.many, "expected attribution executemany"
        # ClickHouse: analytics table always inserted; attributions when present.
        inserted_tables = {t for t, _, _ in ch.inserts}
        assert "risk_assessments_analytics" in inserted_tables
        assert "shap_attributions" in inserted_tables

    def test_persist_writes_engine_arrays_and_indicators(self):
        """The assessment INSERT carries engines_completed/partial, and the
        per-engine Indicators are written to the indicators table."""

        class _FakeConn:
            def __init__(self):
                self.executed = []
                self.many = []

            async def execute(self, sql, *args):
                self.executed.append((sql, args))

            async def executemany(self, sql, rows):
                self.many.append((sql, list(rows)))

            def transaction(self):
                conn = self

                class _Tx:
                    async def __aenter__(self):
                        return conn

                    async def __aexit__(self, *exc):
                        return False

                return _Tx()

        class _FakePool:
            def __init__(self, conn):
                self._conn = conn

            def acquire(self):
                conn = self._conn

                class _Acq:
                    async def __aenter__(self):
                        return conn

                    async def __aexit__(self, *exc):
                        return False

                return _Acq()

        conn = _FakeConn()
        repo = RiskAssessmentRepository(pg_pool=_FakePool(conn), clickhouse_client=None)

        # Two real detection-engine Indicators fed the assessment.
        indicators = [
            make_indicator(EngineType.REPETITIVE_PATTERN, Severity.HIGH),
            make_indicator(EngineType.REGIONAL_ANALYSIS, Severity.MEDIUM),
        ]
        scorer = EnsembleScorer(model_dir=Path("/nonexistent"))
        assessment = scorer.assess(indicators)

        asyncio.run(repo.persist(assessment, indicators))

        # 1. The assessment INSERT now includes the engine-completion arrays.
        assess_sql, assess_args = next(
            (sql, args) for sql, args in conn.executed if "risk_assessments" in sql
        )
        assert "engines_completed" in assess_sql
        assert "engines_partial" in assess_sql
        # The engines_completed arg is the derived list of the two engines.
        completed_arg = next(
            a for a in assess_args if isinstance(a, list)
            and set(a) & {"REPETITIVE_PATTERN", "REGIONAL_ANALYSIS"}
        )
        assert set(completed_arg) == {"REPETITIVE_PATTERN", "REGIONAL_ANALYSIS"}

        # 2. The per-engine Indicators were written to the indicators table.
        indicator_many = [
            rows for sql, rows in conn.many if "INTO indicators" in sql
        ]
        assert indicator_many, "expected an indicators executemany"
        assert len(indicator_many[0]) == 2
