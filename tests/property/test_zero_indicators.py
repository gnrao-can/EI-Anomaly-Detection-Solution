"""
tests/property/test_zero_indicators.py
======================================
Property 8: Zero Indicators produces zero score. Validates: Requirements 9.7

Statement
---------
``EnsembleScorer.score([])`` returns ``Risk_Score = 0`` and
``Risk_Decision = AUTO_APPROVE``, and ``EnsembleScorer.assess([])`` yields a
:class:`~shared.models.RiskAssessment` with ``risk_score == 0``,
``risk_decision == AUTO_APPROVE`` and an empty ``shap_attributions`` list.

This invariant is a fixed assertion at heart, but it is expressed robustly
here:

  - Hypothesis varies how the scorer is *constructed* (different ``model_dir``
    values that don't exist -> deterministic heuristic fallback, and the
    default construction) so the invariant is exercised beyond a single
    hard-coded call.
  - A generated indicator list is filtered down to empty, confirming the
    invariant holds regardless of how the empty sequence is arrived at.
  - Determinism / idempotence: repeated ``score([])`` calls return identical
    results.

The ensemble-scorer service directory contains a hyphen, so it is not
importable as a package. We add it to ``sys.path`` here (mirroring the existing
scorer tests) so ``scorer`` can be imported directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

# --- Make the ensemble-scorer service importable -----------------------------
_SERVICE_DIR = (
    Path(__file__).resolve().parents[2] / "services" / "ensemble-scorer"
)
if str(_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVICE_DIR))

from shared.models import RiskDecision  # noqa: E402

from scorer import EnsembleScorer  # noqa: E402


# ---------------------------------------------------------------------------
# Hypothesis profile — consistent with the other property tests (P1-P15)
# ---------------------------------------------------------------------------

settings.register_profile("ci", max_examples=200)
settings.load_profile("ci")


# ---------------------------------------------------------------------------
# Construction strategy
# ---------------------------------------------------------------------------
#
# Vary how the scorer is built. Every option resolves to a non-existent model
# directory, so the scorer transparently uses its deterministic heuristic
# fallback. This exercises the zero-indicator invariant across a range of
# construction inputs rather than a single hard-coded call.

_model_dir_strategy = st.one_of(
    st.none(),  # default construction (<service>/models — typically absent)
    st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
        min_size=1,
        max_size=20,
    ).map(lambda name: f"/tmp/ei-ads-nonexistent-models/{name}"),
)


def _build_scorer(model_dir):
    return EnsembleScorer(model_dir=model_dir)


# ---------------------------------------------------------------------------
# Property 8
# ---------------------------------------------------------------------------


@given(model_dir=_model_dir_strategy)
def test_score_empty_is_zero_auto_approve(model_dir):
    """score([]) == (0, AUTO_APPROVE) under varied scorer construction."""
    scorer = _build_scorer(model_dir)
    assert scorer.score([]) == (0, RiskDecision.AUTO_APPROVE)


@given(
    model_dir=_model_dir_strategy,
    # Generate a list then filter to empty, so the property is reached via a
    # non-trivial path as well as the trivial literal ``[]``.
    filler=st.lists(st.integers(), max_size=10),
)
def test_score_filtered_to_empty_is_zero(model_dir, filler):
    """A generated list filtered to empty still yields (0, AUTO_APPROVE)."""
    scorer = _build_scorer(model_dir)
    # Filter every element out — the resulting indicator sequence is empty.
    empty_indicators = [x for x in filler if False]
    assert empty_indicators == []
    score, decision = scorer.score(empty_indicators)
    assert score == 0
    assert decision is RiskDecision.AUTO_APPROVE


@given(model_dir=_model_dir_strategy)
def test_assess_empty_zero_score_no_attributions(model_dir):
    """assess([]) -> risk_score 0, AUTO_APPROVE, empty shap_attributions."""
    scorer = _build_scorer(model_dir)
    assessment = scorer.assess([])
    assert assessment.risk_score == 0
    assert assessment.risk_decision is RiskDecision.AUTO_APPROVE
    assert assessment.shap_attributions == []


@given(
    model_dir=_model_dir_strategy,
    repeats=st.integers(min_value=2, max_value=10),
)
def test_score_empty_is_idempotent(model_dir, repeats):
    """Repeated score([]) calls return identical results (determinism)."""
    scorer = _build_scorer(model_dir)
    results = [scorer.score([]) for _ in range(repeats)]
    assert all(r == (0, RiskDecision.AUTO_APPROVE) for r in results)
    # All results are identical to the first.
    assert all(r == results[0] for r in results)
