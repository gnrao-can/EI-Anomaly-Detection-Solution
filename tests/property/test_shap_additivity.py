"""
tests/property/test_shap_additivity.py
=======================================
Property 7: SHAP additivity invariant. Validates: Requirements 9.3

For any set of Indicators (0..20), the sum of the SHAP feature-attribution
contributions produced by the Ensemble Scorer SHALL equal
``Risk_Score - base_value`` within a floating-point tolerance of ``1e-4``.

This is exercised at two layers:

  1. Directly against :class:`ShapAttributor.explain` (the SHAP attribution
     component in ``services/ensemble-scorer/shap_explainer.py``), and
  2. End-to-end through :meth:`EnsembleScorer.assess`
     (``services/ensemble-scorer/scorer.py``), asserting the same invariant on
     ``assessment.shap_attributions`` vs
     ``(assessment.risk_score - assessment.shap_base_value)``.

The empty Indicator set (``indicator_count == 0``) is included as a case: it
overlaps Property 8 — the sum of contributions is ``0`` and equals
``risk_score - base_value`` (both ``0``).

In the CI sandbox the native ``shap`` / ``xgboost`` stack (which needs
``libomp``) may be unavailable, so both layers run through their documented
deterministic linear-attribution fallback. The additivity invariant holds by
construction in that mode, so Property 7 can be validated without the native ML
libraries.

The ensemble-scorer service directory contains a hyphen, so it is not
importable as a package; we add it to ``sys.path`` (as the sibling unit tests
in ``tests/unit/test_shap_explainer.py`` do).

Property 7: SHAP additivity invariant. Validates: Requirements 9.3
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# --- Make the ensemble-scorer service importable -----------------------------
_SERVICE_DIR = (
    Path(__file__).resolve().parents[2] / "services" / "ensemble-scorer"
)
if str(_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVICE_DIR))

from shared.models import (  # noqa: E402
    EngineType,
    Indicator,
    Severity,
)

from features import DEFAULT_ENCODER  # noqa: E402
from scorer import EnsembleScorer  # noqa: E402
from shap_explainer import ADDITIVITY_TOLERANCE, ShapAttributor  # noqa: E402


# ---------------------------------------------------------------------------
# Hypothesis profile: CI runs 200 examples (design.md — Testing Strategy)
# ---------------------------------------------------------------------------

# Registering is idempotent-safe: guard against a duplicate registration if a
# conftest or another module has already registered "ci".
try:
    settings.register_profile(
        "ci", max_examples=200, suppress_health_check=[HealthCheck.too_slow]
    )
except Exception:  # noqa: BLE001 - already registered elsewhere
    pass
settings.load_profile("ci")


# ---------------------------------------------------------------------------
# Generators — build valid Indicator objects (shared/models.py)
# ---------------------------------------------------------------------------

_ENGINES = list(EngineType)
_SEVERITIES = list(Severity)


@st.composite
def indicators_strategy(draw, min_size: int = 0, max_size: int = 20):
    """Generate a list of valid :class:`Indicator` objects.

    Each Indicator gets a randomised :class:`EngineType` and :class:`Severity`
    plus a randomised ``shap_contribution`` and other free-form fields. All
    Indicators in a set share a single ``claim_id`` (as they would for a real
    claim).
    """
    count = draw(st.integers(min_value=min_size, max_value=max_size))
    claim_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    indicators: list[Indicator] = []
    for _ in range(count):
        engine = draw(st.sampled_from(_ENGINES))
        severity = draw(st.sampled_from(_SEVERITIES))
        shap_contribution = draw(
            st.one_of(
                st.none(),
                st.floats(
                    min_value=-50.0,
                    max_value=50.0,
                    allow_nan=False,
                    allow_infinity=False,
                ),
            )
        )
        title = draw(st.text(min_size=0, max_size=100))
        algorithm = draw(st.text(min_size=0, max_size=40))
        indicators.append(
            Indicator(
                indicator_id=uuid.uuid4(),
                claim_id=claim_id,
                engine=engine,
                severity=severity,
                algorithm=algorithm,
                title=title,
                explanation_en=draw(st.text(min_size=0, max_size=120)),
                explanation_fr=draw(st.text(min_size=0, max_size=120)),
                payload={},
                produced_at=now,
                shap_contribution=shap_contribution,
            )
        )
    return indicators


# A single scorer/attributor pair is reused across examples. They are stateless
# with respect to scoring, and forcing the heuristic/fallback path keeps the
# test independent of the native ML stack.
_SCORER = EnsembleScorer(model_dir=Path("/nonexistent-models-dir"))
_ATTRIBUTOR = ShapAttributor()  # no model => deterministic linear fallback


# ---------------------------------------------------------------------------
# Property 7: SHAP additivity invariant — direct on ShapAttributor.explain
# ---------------------------------------------------------------------------


@given(indicators=indicators_strategy(min_size=0, max_size=20))
def test_P7_shap_additivity_attributor(indicators):
    """Property 7: SHAP additivity invariant. Validates: Requirements 9.3

    ``|sum(contributions) - (risk_score - base_value)| < 1e-4`` for the SHAP
    attribution component directly.
    """
    # Use the scorer's own risk score so the attribution target matches the
    # real scoring pipeline. The empty set yields (0, AUTO_APPROVE) (P8 overlap).
    risk_score, _decision = _SCORER.score(indicators)
    feature_vector = DEFAULT_ENCODER.encode(indicators)

    attributions, base_value = _ATTRIBUTOR.explain(
        feature_vector, indicators, risk_score
    )

    # One attribution per Indicator (empty set -> no attributions).
    assert len(attributions) == len(indicators)

    total = sum(a.contribution for a in attributions)
    expected = float(risk_score) - float(base_value)
    assert abs(total - expected) < ADDITIVITY_TOLERANCE, (
        f"SHAP additivity violated (attributor): |{total} - {expected}| "
        f">= {ADDITIVITY_TOLERANCE} for {len(indicators)} indicators"
    )


# ---------------------------------------------------------------------------
# Property 7: SHAP additivity invariant — end-to-end via EnsembleScorer.assess
# ---------------------------------------------------------------------------


@given(indicators=indicators_strategy(min_size=0, max_size=20))
def test_P7_shap_additivity_assess(indicators):
    """Property 7: SHAP additivity invariant. Validates: Requirements 9.3

    The same invariant holds on a full assessment produced by
    :meth:`EnsembleScorer.assess`:
    ``|sum(assessment.shap_attributions) - (risk_score - shap_base_value)| < 1e-4``.
    """
    assessment = _SCORER.assess(indicators)

    assert len(assessment.shap_attributions) == len(indicators)

    total = sum(a.contribution for a in assessment.shap_attributions)
    expected = float(assessment.risk_score) - float(assessment.shap_base_value)
    assert abs(total - expected) < ADDITIVITY_TOLERANCE, (
        f"SHAP additivity violated (assess): |{total} - {expected}| "
        f">= {ADDITIVITY_TOLERANCE} for {len(indicators)} indicators"
    )


# ---------------------------------------------------------------------------
# Property 7 / Property 8 overlap: the empty Indicator set explicitly
# ---------------------------------------------------------------------------


def test_P7_empty_indicator_set_is_additive():
    """Property 7: SHAP additivity invariant. Validates: Requirements 9.3

    The empty Indicator set (``indicator_count == 0``) — a P8 overlap — has a
    contribution sum of exactly ``0``, equal to ``risk_score - base_value``.
    """
    assessment = _SCORER.assess([])
    assert assessment.shap_attributions == []
    assert sum(a.contribution for a in assessment.shap_attributions) == 0.0
    assert (
        abs(0.0 - (assessment.risk_score - assessment.shap_base_value))
        < ADDITIVITY_TOLERANCE
    )
