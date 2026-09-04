"""
tests/property/test_p8_zero_indicators.py
==========================================
Property 8: Zero Indicators produces zero score.

**Validates: Requirements 9.7**

Statement
---------
``EnsembleScorer.score([])`` returns ``Risk_Score = 0`` and
``Risk_Decision = AUTO_APPROVE``, and ``EnsembleScorer.assess([])`` yields a
:class:`~shared.models.RiskAssessment` with ``risk_score == 0``,
``risk_decision == AUTO_APPROVE`` and an empty ``shap_attributions`` list.

The core claim is deterministic (the input is the empty list), so this module
combines a *direct* assertion of the invariant with a *Hypothesis-driven
robustness* property. The robustness property varies:

  - how the empty Indicator sequence is arrived at (literal ``[]``, a generated
    list cleared to empty, an emptied tuple/generator, etc.), and
  - how the scorer is *constructed* (fresh instances, default vs. non-existent
    ``model_dir`` — every option resolves to the deterministic heuristic
    fallback since no model artefacts are present),

so the invariant is exercised well beyond a single hard-coded call. The
empty-input result is also asserted to be *stable across many scorer
instances* (determinism).

The ensemble-scorer service directory contains a hyphen, so it is not
importable as a package. We add it to ``sys.path`` here (mirroring the existing
scorer property/unit tests) so ``scorer`` can be imported directly.
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
#
# Registering is idempotent-safe: another property module (or conftest) may
# already have registered "ci". Guard against a duplicate registration error.
try:
    settings.register_profile("ci", max_examples=200)
except Exception:  # noqa: BLE001 - already registered elsewhere
    pass
settings.load_profile("ci")


# The canonical result for the empty Indicator set.
_EMPTY_RESULT = (0, RiskDecision.AUTO_APPROVE)


# ---------------------------------------------------------------------------
# Direct (deterministic) assertion
# ---------------------------------------------------------------------------


def test_score_and_assess_empty_direct():
    """Property 8. Validates: Requirements 9.7

    Direct assertion that a default-constructed scorer returns
    ``(0, AUTO_APPROVE)`` for ``score([])`` and a zero-score AUTO_APPROVE
    assessment with no SHAP attributions for ``assess([])``.
    """
    scorer = EnsembleScorer()

    assert scorer.score([]) == (0, RiskDecision.AUTO_APPROVE)

    assessment = scorer.assess([])
    assert assessment.risk_score == 0
    assert assessment.risk_decision is RiskDecision.AUTO_APPROVE
    assert assessment.shap_attributions == []


# ---------------------------------------------------------------------------
# Strategies for the Hypothesis-driven robustness property
# ---------------------------------------------------------------------------
#
# Vary how the scorer is built. Every option resolves to a non-existent model
# directory (or the default one, which is typically absent), so the scorer
# transparently uses its deterministic heuristic fallback. Each draw builds a
# *fresh* scorer instance, demonstrating determinism across construction.

_model_dir_strategy = st.one_of(
    st.none(),  # default construction (<service>/models — typically absent)
    st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
        min_size=1,
        max_size=20,
    ).map(lambda name: f"/tmp/ei-ads-nonexistent-models/{name}"),
)


def _empty_via_literal(_seed):
    """The trivial empty list literal."""
    return []


def _empty_via_cleared_list(seed):
    """Build a list from a seed, then clear it to empty."""
    data = list(range(seed))
    data.clear()
    return data


def _empty_via_filtered_list(seed):
    """Build a list, then filter every element out."""
    return [x for x in range(seed) if False]


def _empty_via_emptied_tuple(_seed):
    """An empty tuple (an empty sequence that is not a list)."""
    return tuple()


def _empty_via_exhausted_generator(_seed):
    """An empty sequence materialised from an exhausted generator."""
    return list(x for x in [] if x)


# Each strategy returns a callable that, given an integer seed, produces an
# empty sequence via a different route. This makes "no indicators" the
# invariant regardless of how the caller arrived at emptiness.
_empty_builder_strategy = st.sampled_from(
    [
        _empty_via_literal,
        _empty_via_cleared_list,
        _empty_via_filtered_list,
        _empty_via_emptied_tuple,
        _empty_via_exhausted_generator,
    ]
)


# ---------------------------------------------------------------------------
# Property 8 — Hypothesis-driven robustness
# ---------------------------------------------------------------------------


@given(
    model_dir=_model_dir_strategy,
    build_empty=_empty_builder_strategy,
    seed=st.integers(min_value=0, max_value=50),
)
def test_score_empty_is_zero_auto_approve(model_dir, build_empty, seed):
    """Property 8. Validates: Requirements 9.7

    ``score(<empty>) == (0, AUTO_APPROVE)`` for a freshly constructed scorer,
    across varied construction options and varied ways of producing the empty
    Indicator sequence.
    """
    scorer = EnsembleScorer(model_dir=model_dir)  # fresh instance each example

    empty_indicators = build_empty(seed)
    assert len(empty_indicators) == 0  # confirm we really reduced to "no indicators"

    assert scorer.score(empty_indicators) == _EMPTY_RESULT


@given(
    model_dir=_model_dir_strategy,
    build_empty=_empty_builder_strategy,
    seed=st.integers(min_value=0, max_value=50),
)
def test_assess_empty_zero_score_no_attributions(model_dir, build_empty, seed):
    """Property 8. Validates: Requirements 9.7

    ``assess(<empty>)`` yields ``risk_score == 0``, ``AUTO_APPROVE`` and an
    empty ``shap_attributions`` list, across varied construction and varied
    empty-sequence routes.
    """
    scorer = EnsembleScorer(model_dir=model_dir)  # fresh instance each example

    empty_indicators = build_empty(seed)
    assert len(empty_indicators) == 0

    assessment = scorer.assess(empty_indicators)
    assert assessment.risk_score == 0
    assert assessment.risk_decision is RiskDecision.AUTO_APPROVE
    assert assessment.shap_attributions == []


@given(
    model_dir=_model_dir_strategy,
    instances=st.integers(min_value=2, max_value=25),
)
def test_score_empty_stable_across_many_instances(model_dir, instances):
    """Property 8. Validates: Requirements 9.7

    The empty-input result is stable across many *independent* scorer
    instances (determinism): every fresh scorer returns the identical
    ``(0, AUTO_APPROVE)`` for ``score([])``.
    """
    results = [
        EnsembleScorer(model_dir=model_dir).score([]) for _ in range(instances)
    ]
    assert all(r == _EMPTY_RESULT for r in results)
    # Every instance agrees with every other instance.
    assert all(r == results[0] for r in results)
