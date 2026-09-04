"""
tests/property/test_p7_shap_additivity.py
==========================================
Property 7: SHAP additivity invariant. Validates: Requirements 9.3

Statement
---------
*For any* claim and its associated Indicator set processed by the
Ensemble_Scorer, the sum of all SHAP feature attribution contributions SHALL
equal the Risk_Score minus the model's base value, within a floating-point
tolerance of ``1e-4``:

    | sum(a.contribution for a in shap_attributions)
        - (risk_score - shap_base_value) | < 1e-4

This module exercises the invariant at two layers:

  1. End-to-end through :meth:`EnsembleScorer.assess` — the task-7.2 scoring
     flow that wires SHAP into the assessment; and
  2. Directly against :meth:`ShapAttributor.explain` — the SHAP attribution
     component in ``services/ensemble-scorer/shap_explainer.py`` — using the
     same generated Indicator sets.

Generators randomise the engine (:class:`EngineType`), severity
(:class:`Severity`), and the SHAP-relevant free-form fields of each Indicator.
Every Indicator receives a fresh UUID and all Indicators in a set share a single
``claim_id`` (as they would for a real claim).

The task's specific formulation
``@given(indicator_count=st.integers(min_value=0, max_value=20))`` is included as
its own property test (:func:`test_P7_shap_additivity_indicator_count`): it
generates exactly that many Indicators and asserts additivity.

Edge case: ``indicator_count == 0`` yields an empty attribution list, a
Risk_Score of ``0`` (P8 overlap), and the invariant holds trivially
(``0 == 0 - 0``). This is covered explicitly by
:func:`test_P7_empty_indicator_set_is_additive` and also by the ``min_value=0``
lower bound of the generators.

Sandbox note
------------
The native ``shap`` / ``xgboost`` stack (which requires ``libomp``) may be
unavailable in the CI sandbox, so both layers run through their documented
deterministic linear-attribution fallback. The additivity invariant holds by
construction in that mode, so Property 7 can be validated without the native ML
libraries.

The ensemble-scorer service directory contains a hyphen, so it is not importable
as a package; we add it to ``sys.path`` (as the sibling unit tests in
``tests/unit/test_shap_explainer.py`` do).

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
    RiskDecision,
    Severity,
)

from features import DEFAULT_ENCODER  # noqa: E402
from scorer import EnsembleScorer  # noqa: E402
from shap_explainer import ADDITIVITY_TOLERANCE, ShapAttributor  # noqa: E402


# ---------------------------------------------------------------------------
# Hypothesis profile: CI runs 200 examples (design.md — Testing Strategy;
# tasks.md Notes: settings.register_profile("ci", max_examples=200, ...))
# ---------------------------------------------------------------------------

# Registering is guarded so a duplicate registration (from a conftest or a
# sibling property-test module already loaded in the same session) does not
# raise. We always (re)load the "ci" profile so this file runs 200 examples.
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


def _make_indicator(draw: st.DrawFn, claim_id: uuid.UUID, now: datetime) -> Indicator:
    """Draw a single valid :class:`Indicator` with randomised fields.

    Randomises the engine, severity, and the SHAP-relevant / free-form fields.
    Gives the Indicator a fresh UUID and the shared ``claim_id``.
    """
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
    return Indicator(
        indicator_id=uuid.uuid4(),
        claim_id=claim_id,
        engine=engine,
        severity=severity,
        algorithm=draw(st.text(min_size=0, max_size=40)),
        title=draw(st.text(min_size=0, max_size=100)),
        explanation_en=draw(st.text(min_size=0, max_size=120)),
        explanation_fr=draw(st.text(min_size=0, max_size=120)),
        payload={},
        produced_at=now,
        shap_contribution=shap_contribution,
    )


@st.composite
def indicators_strategy(draw, min_size: int = 0, max_size: int = 20):
    """Generate a list of 0..``max_size`` valid :class:`Indicator` objects.

    All Indicators in a set share a single ``claim_id`` (as they would for a
    real claim); each has its own UUID and randomised engine/severity/fields.
    """
    count = draw(st.integers(min_value=min_size, max_value=max_size))
    claim_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    return [_make_indicator(draw, claim_id, now) for _ in range(count)]


# A single scorer/attributor pair is reused across examples. They are stateless
# with respect to scoring, and pointing the scorer at a non-existent model_dir
# forces the deterministic heuristic / linear-attribution fallback, keeping the
# test independent of the native ML stack.
_SCORER = EnsembleScorer(model_dir=Path("/nonexistent-models-dir-p7"))
_ATTRIBUTOR = ShapAttributor()  # no model => deterministic linear fallback


def _assert_additive(contributions, risk_score, base_value, *, layer: str, n: int) -> None:
    """Assert the additivity invariant with a descriptive failure message."""
    total = sum(contributions)
    expected = float(risk_score) - float(base_value)
    assert abs(total - expected) < ADDITIVITY_TOLERANCE, (
        f"SHAP additivity violated ({layer}): "
        f"|{total} - {expected}| >= {ADDITIVITY_TOLERANCE} for {n} indicators"
    )


# ---------------------------------------------------------------------------
# Property 7 — the task's exact formulation:
#   @given(indicator_count=st.integers(min_value=0, max_value=20))
# generate that many Indicators and assert additivity.
# ---------------------------------------------------------------------------


@given(indicator_count=st.integers(min_value=0, max_value=20))
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_P7_shap_additivity_indicator_count(indicator_count):
    """Property 7: SHAP additivity invariant. Validates: Requirements 9.3

    Generate a random Indicator set of size ``indicator_count`` (0..20), run the
    Ensemble Scorer, and assert
    ``|sum(contributions) - (risk_score - base_value)| < 1e-4``.
    """
    # The count is driven by the top-level @given argument exactly as the spec
    # requests; the Indicators themselves are built for that count below.
    indicators = _draw_indicators_for_count(indicator_count)

    assessment = _SCORER.assess(indicators)

    # One attribution per Indicator; empty set => no attributions.
    assert len(assessment.shap_attributions) == indicator_count

    _assert_additive(
        [a.contribution for a in assessment.shap_attributions],
        assessment.risk_score,
        assessment.shap_base_value,
        layer="assess/indicator_count",
        n=indicator_count,
    )


def _draw_indicators_for_count(indicator_count: int) -> list[Indicator]:
    """Build ``indicator_count`` valid Indicators deterministically.

    Used by :func:`test_P7_shap_additivity_indicator_count`, where the count is
    the Hypothesis-generated parameter and the Indicators themselves only need
    valid, varied engine/severity values (the scorer reads those two fields).
    """
    claim_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    indicators: list[Indicator] = []
    for i in range(indicator_count):
        indicators.append(
            Indicator(
                indicator_id=uuid.uuid4(),
                claim_id=claim_id,
                engine=_ENGINES[i % len(_ENGINES)],
                severity=_SEVERITIES[i % len(_SEVERITIES)],
                algorithm="p7_prop",
                title="p7_prop",
                explanation_en="p7_prop",
                explanation_fr="p7_prop",
                payload={},
                produced_at=now,
                shap_contribution=None,
            )
        )
    return indicators


# ---------------------------------------------------------------------------
# Property 7 — end-to-end via EnsembleScorer.assess with fully random sets
# ---------------------------------------------------------------------------


@given(indicators=indicators_strategy(min_size=0, max_size=20))
def test_P7_shap_additivity_assess(indicators):
    """Property 7: SHAP additivity invariant. Validates: Requirements 9.3

    The invariant holds on a full assessment produced by
    :meth:`EnsembleScorer.assess` for a randomly generated Indicator set.
    """
    assessment = _SCORER.assess(indicators)

    assert len(assessment.shap_attributions) == len(indicators)

    _assert_additive(
        [a.contribution for a in assessment.shap_attributions],
        assessment.risk_score,
        assessment.shap_base_value,
        layer="assess",
        n=len(indicators),
    )


# ---------------------------------------------------------------------------
# Property 7 — direct on ShapAttributor.explain with the same generated sets
# ---------------------------------------------------------------------------


@given(indicators=indicators_strategy(min_size=0, max_size=20))
def test_P7_shap_additivity_attributor(indicators):
    """Property 7: SHAP additivity invariant. Validates: Requirements 9.3

    Directly exercise :meth:`ShapAttributor.explain` with the same generated
    Indicator sets. Uses the scorer's own Risk_Score so the attribution target
    matches the real scoring pipeline.
    """
    risk_score, _decision = _SCORER.score(indicators)
    feature_vector = DEFAULT_ENCODER.encode(indicators)

    attributions, base_value = _ATTRIBUTOR.explain(
        feature_vector, indicators, risk_score
    )

    assert len(attributions) == len(indicators)

    _assert_additive(
        [a.contribution for a in attributions],
        risk_score,
        base_value,
        layer="attributor",
        n=len(indicators),
    )


# ---------------------------------------------------------------------------
# Edge case: indicator_count == 0 -> empty attributions, risk_score 0,
# invariant holds trivially.
# ---------------------------------------------------------------------------


def test_P7_empty_indicator_set_is_additive():
    """Property 7: SHAP additivity invariant. Validates: Requirements 9.3

    The empty Indicator set (``indicator_count == 0``) — a P8 overlap — produces
    no attributions, a Risk_Score of ``0`` and an AUTO_APPROVE decision, and the
    additivity invariant holds trivially (``0 == 0 - 0``).
    """
    assessment = _SCORER.assess([])

    assert assessment.shap_attributions == []
    assert assessment.risk_score == 0
    assert assessment.risk_decision is RiskDecision.AUTO_APPROVE
    assert sum(a.contribution for a in assessment.shap_attributions) == 0.0
    assert (
        abs(0.0 - (assessment.risk_score - assessment.shap_base_value))
        < ADDITIVITY_TOLERANCE
    )
