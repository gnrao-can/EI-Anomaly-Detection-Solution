"""
tests/unit/test_scorer.py
=========================
Unit tests for the Ensemble Scorer service (task 7.1):

  - FeatureEncoder determinism and fixed vector length
  - EnsembleScorer empty-indicator behaviour (0 / AUTO_APPROVE)
  - Decision-threshold boundary mapping ([0,29] / [30,69] / [70,100])
  - Heuristic monotonicity (superset never lowers the score)

The ensemble-scorer service directory contains a hyphen, so it is not importable
as a package. We add it to ``sys.path`` here so ``features`` and ``scorer`` can
be imported directly.

Requirements: 9.1, 9.2
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

# --- Make the ensemble-scorer service importable -----------------------------
_SERVICE_DIR = (
    Path(__file__).resolve().parents[2] / "services" / "ensemble-scorer"
)
if str(_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVICE_DIR))

from shared.models import EngineType, Indicator, RiskDecision, Severity  # noqa: E402

from features import (  # noqa: E402
    FEATURE_VECTOR_LENGTH,
    FeatureEncoder,
)
from scorer import EnsembleScorer, decision_for_score  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_indicator(
    engine: EngineType = EngineType.REPETITIVE_PATTERN,
    severity: Severity = Severity.HIGH,
) -> Indicator:
    """Build a minimal valid Indicator for tests."""
    return Indicator(
        indicator_id=uuid.uuid4(),
        claim_id=uuid.uuid4(),
        engine=engine,
        severity=severity,
        algorithm="test",
        title="test",
        explanation_en="test",
        explanation_fr="test",
        payload={},
        produced_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# FeatureEncoder
# ---------------------------------------------------------------------------


class TestFeatureEncoder:
    def test_empty_vector_has_fixed_length(self):
        enc = FeatureEncoder()
        vec = enc.encode([])
        assert vec.shape == (FEATURE_VECTOR_LENGTH,)

    def test_empty_vector_is_all_zeros(self):
        enc = FeatureEncoder()
        vec = enc.encode([])
        assert vec.sum() == 0.0

    def test_vector_length_constant_across_inputs(self):
        enc = FeatureEncoder()
        v0 = enc.encode([])
        v1 = enc.encode([make_indicator()])
        v2 = enc.encode([make_indicator(), make_indicator(EngineType.FRAUD_RING)])
        assert len(v0) == len(v1) == len(v2) == FEATURE_VECTOR_LENGTH

    def test_determinism_same_input_same_vector(self):
        enc = FeatureEncoder()
        # Reuse the SAME indicator objects so ordering/content is identical.
        indicators = [
            make_indicator(EngineType.GEOGRAPHIC_ANOMALY, Severity.CRITICAL),
            make_indicator(EngineType.FRAUD_RING, Severity.MEDIUM),
        ]
        v_a = enc.encode(indicators)
        v_b = enc.encode(indicators)
        assert (v_a == v_b).all()

    def test_presence_and_count_reflect_indicators(self):
        enc = FeatureEncoder()
        indicators = [
            make_indicator(EngineType.REPETITIVE_PATTERN, Severity.HIGH),
            make_indicator(EngineType.REPETITIVE_PATTERN, Severity.LOW),
        ]
        vec = enc.encode(indicators)
        names = enc.feature_names
        present_idx = names.index("repetitive_pattern_present")
        count_idx = names.index("repetitive_pattern_count")
        total_idx = names.index("total_indicator_count")
        assert vec[present_idx] == 1.0
        assert vec[count_idx] == 2.0
        assert vec[total_idx] == 2.0

    def test_severity_aggregates(self):
        enc = FeatureEncoder()
        indicators = [
            make_indicator(EngineType.FRAUD_RING, Severity.CRITICAL),  # 4
            make_indicator(EngineType.DOCUMENT_FORGERY, Severity.LOW),  # 1
        ]
        vec = enc.encode(indicators)
        names = enc.feature_names
        assert vec[names.index("severity_ordinal_sum")] == 5.0
        assert vec[names.index("severity_ordinal_max")] == 4.0
        assert vec[names.index("high_or_critical_count")] == 1.0


# ---------------------------------------------------------------------------
# decision_for_score — threshold boundaries
# ---------------------------------------------------------------------------


class TestDecisionThresholds:
    @pytest.mark.parametrize("score", [0, 1, 15, 29])
    def test_auto_approve_range(self, score):
        assert decision_for_score(score) is RiskDecision.AUTO_APPROVE

    @pytest.mark.parametrize("score", [30, 50, 69])
    def test_flag_for_review_range(self, score):
        assert decision_for_score(score) is RiskDecision.FLAG_FOR_REVIEW

    @pytest.mark.parametrize("score", [70, 85, 100])
    def test_investigate_range(self, score):
        assert decision_for_score(score) is RiskDecision.INVESTIGATE

    def test_boundary_29_30(self):
        assert decision_for_score(29) is RiskDecision.AUTO_APPROVE
        assert decision_for_score(30) is RiskDecision.FLAG_FOR_REVIEW

    def test_boundary_69_70(self):
        assert decision_for_score(69) is RiskDecision.FLAG_FOR_REVIEW
        assert decision_for_score(70) is RiskDecision.INVESTIGATE


# ---------------------------------------------------------------------------
# EnsembleScorer — behaviour (heuristic path via nonexistent model dir)
# ---------------------------------------------------------------------------


@pytest.fixture
def heuristic_scorer(tmp_path) -> EnsembleScorer:
    """A scorer pointed at an empty model dir -> heuristic fallback."""
    return EnsembleScorer(model_dir=tmp_path / "no_models")


class TestEnsembleScorerEmpty:
    def test_empty_indicators_zero_auto_approve(self, heuristic_scorer):
        score, decision = heuristic_scorer.score([])
        assert score == 0
        assert decision is RiskDecision.AUTO_APPROVE

    def test_empty_indicators_zero_even_with_trained_models(self, tmp_path):
        # Even if models were present, empty input short-circuits to 0.
        scorer = EnsembleScorer(model_dir=tmp_path)
        score, decision = scorer.score([])
        assert score == 0
        assert decision is RiskDecision.AUTO_APPROVE


class TestEnsembleScorerHeuristic:
    def test_score_in_range(self, heuristic_scorer):
        indicators = [make_indicator(EngineType.FRAUD_RING, Severity.CRITICAL)]
        score, decision = heuristic_scorer.score(indicators)
        assert 0 <= score <= 100
        assert isinstance(decision, RiskDecision)

    def test_uses_fallback_when_no_models(self, heuristic_scorer):
        assert heuristic_scorer.using_fallback is True
        assert heuristic_scorer.model_version == "heuristic_fallback_v1"

    def test_more_severe_scores_higher(self, heuristic_scorer):
        low = [make_indicator(EngineType.REGIONAL_ANALYSIS, Severity.LOW)]
        crit = [
            make_indicator(EngineType.FRAUD_RING, Severity.CRITICAL),
            make_indicator(EngineType.DOCUMENT_FORGERY, Severity.CRITICAL),
            make_indicator(EngineType.HISTORY_INCONSISTENCY, Severity.CRITICAL),
        ]
        low_score, _ = heuristic_scorer.score(low)
        crit_score, _ = heuristic_scorer.score(crit)
        assert crit_score > low_score

    def test_monotonicity_superset_not_lower(self, heuristic_scorer):
        base = [
            make_indicator(EngineType.FRAUD_RING, Severity.HIGH),
            make_indicator(EngineType.GEOGRAPHIC_ANOMALY, Severity.MEDIUM),
        ]
        superset = base + [make_indicator(EngineType.DOCUMENT_FORGERY, Severity.CRITICAL)]
        base_score, _ = heuristic_scorer.score(base)
        superset_score, _ = heuristic_scorer.score(superset)
        assert superset_score >= base_score

    def test_all_engines_critical_investigate(self, heuristic_scorer):
        indicators = [
            make_indicator(e, Severity.CRITICAL)
            for e in EngineType
        ]
        # Add several more critical indicators to push toward the top band.
        indicators += [make_indicator(EngineType.FRAUD_RING, Severity.CRITICAL)] * 6
        score, decision = heuristic_scorer.score(indicators)
        assert score >= 70
        assert decision is RiskDecision.INVESTIGATE
