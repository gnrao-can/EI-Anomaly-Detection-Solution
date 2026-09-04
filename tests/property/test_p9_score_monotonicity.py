"""
tests/property/test_p9_score_monotonicity.py
=============================================
Property 9: Risk Score monotonicity. Validates: Requirements 9.8

Statement
---------
For any two Indicator sets where I1 is a (super)set of I2 (I1 ⊇ I2), the
Ensemble_Scorer SHALL assign ``score(I1) >= score(I2)``. Adding evidence to a
claim must never *lower* its Risk_Score.

This is the documented guarantee of the deterministic heuristic fallback in
:meth:`EnsembleScorer.score` (see ``services/ensemble-scorer/scorer.py`` — "The
heuristic is monotonic in the Indicator set"). The heuristic derives its
components from the total ordinal severity, the number of distinct engines that
fired, and the CRITICAL count — all of which are monotone non-decreasing under
set union. Arbitrary *trained* XGBoost / Isolation Forest models are NOT
guaranteed monotonic unless trained with monotonic constraints on the
indicator-count features (a design consideration noted in design.md — "The
XGBoost model is trained with the constraint that adding more Indicators ...
cannot decrease its score"). This test therefore deliberately targets the
heuristic path by constructing the scorer with a non-existent ``model_dir``.

The ``ensemble-scorer`` service directory contains a hyphen, so it is not
importable as a package. We prepend it to ``sys.path`` (mirroring the existing
scorer tests) so ``scorer`` and ``features`` can be imported directly.

Requirements: 9.8
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

from shared.models import EngineType, Indicator, Severity  # noqa: E402

from scorer import EnsembleScorer  # noqa: E402


# ---------------------------------------------------------------------------
# Hypothesis profile — CI runs 200 examples (per plan / design)
# ---------------------------------------------------------------------------

settings.register_profile(
    "ci",
    max_examples=200,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
settings.load_profile("ci")


# ---------------------------------------------------------------------------
# A single heuristic scorer instance (heuristic fallback in the sandbox).
#
# Pointing at a non-existent model_dir forces the deterministic monotonic
# heuristic path — the mode whose monotonicity we guarantee (P9). The scorer is
# stateless w.r.t. scoring, so one instance is safe to reuse across examples.
# ---------------------------------------------------------------------------

SCORER = EnsembleScorer(model_dir=_SERVICE_DIR / "__no_such_model_dir__")
assert SCORER.using_fallback is True, (
    "P9 test requires the heuristic fallback path; a real model dir was found."
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_ENGINES = list(EngineType)
_SEVERITIES = list(Severity)


def _make_indicator(claim_id: uuid.UUID, engine: EngineType, severity: Severity) -> Indicator:
    """Build a valid Indicator with a fresh UUID; only engine/severity vary.

    ``claim_id`` is shared across an Indicator set (all Indicators for a claim
    carry the same ``claim_id``); ``indicator_id`` is a fresh valid UUID.
    """
    return Indicator(
        indicator_id=uuid.uuid4(),
        claim_id=claim_id,
        engine=engine,
        severity=severity,
        algorithm="p9_prop_test",
        title="p9_prop_test",
        explanation_en="p9_prop_test",
        explanation_fr="p9_prop_test",
        payload={},
        produced_at=datetime.now(timezone.utc),
    )


@st.composite
def _indicator_specs(draw: st.DrawFn, min_size: int, max_size: int) -> list[tuple]:
    """Generate a list of (engine, severity) pairs (the score-relevant fields)."""
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    specs: list[tuple] = []
    for _ in range(n):
        engine = draw(st.sampled_from(_ENGINES))
        severity = draw(st.sampled_from(_SEVERITIES))
        specs.append((engine, severity))
    return specs


@st.composite
def _superset_pair(draw: st.DrawFn) -> tuple[list[Indicator], list[Indicator]]:
    """Generate (I1, I2) with I1 ⊇ I2 by construction, sharing a claim_id.

    I2 is a base set of 0..15 Indicators. I1 = I2 + extra (>=0 extra
    Indicators), so I1 is a superset of I2 by construction. Engine and severity
    are randomised; the Indicators in I2 are shared *by identity* into I1 so the
    superset relation is exact.
    """
    claim_id = uuid.uuid4()

    base_specs = draw(_indicator_specs(min_size=0, max_size=15))
    extra_specs = draw(_indicator_specs(min_size=0, max_size=10))

    i2 = [_make_indicator(claim_id, e, s) for (e, s) in base_specs]
    # I1 reuses the exact same Indicator objects from I2, then appends extras.
    i1 = list(i2) + [_make_indicator(claim_id, e, s) for (e, s) in extra_specs]
    return i1, i2


# ---------------------------------------------------------------------------
# Property 9 — general superset monotonicity
# ---------------------------------------------------------------------------


@given(pair=_superset_pair())
def test_p9_score_monotonic_superset(pair: tuple[list[Indicator], list[Indicator]]) -> None:
    """For I1 ⊇ I2, score(I1) >= score(I2). Validates: Requirements 9.8"""
    i1, i2 = pair

    score_i1 = SCORER.score(i1)[0]
    score_i2 = SCORER.score(i2)[0]

    assert score_i1 >= score_i2, (
        f"Monotonicity violated: score(I1)={score_i1} < score(I2)={score_i2}; "
        f"|I2|={len(i2)}, |I1|={len(i1)}"
    )


# ---------------------------------------------------------------------------
# Property 9 — adding to the EMPTY set specifically
# ---------------------------------------------------------------------------


@given(extra=_indicator_specs(min_size=1, max_size=10))
def test_p9_score_monotonic_from_empty(extra: list[tuple]) -> None:
    """score([] + extra) >= score([]) == 0. Validates: Requirements 9.8"""
    empty_score = SCORER.score([])[0]
    assert empty_score == 0  # P8 sanity: empty set scores 0

    claim_id = uuid.uuid4()
    added = [_make_indicator(claim_id, e, s) for (e, s) in extra]
    added_score = SCORER.score(added)[0]

    assert added_score >= empty_score, (
        f"Adding indicators to the empty set lowered the score: "
        f"{added_score} < {empty_score}"
    )


# ---------------------------------------------------------------------------
# Property 9 — adding a CRITICAL indicator specifically never lowers the score
# ---------------------------------------------------------------------------


@given(
    base=_indicator_specs(min_size=0, max_size=15),
    engine=st.sampled_from(_ENGINES),
)
def test_p9_adding_critical_does_not_decrease(base: list[tuple], engine: EngineType) -> None:
    """Appending a CRITICAL indicator to any base set never lowers the score.

    Validates: Requirements 9.8
    """
    claim_id = uuid.uuid4()
    i2 = [_make_indicator(claim_id, e, s) for (e, s) in base]
    i1 = list(i2) + [_make_indicator(claim_id, engine, Severity.CRITICAL)]

    score_i2 = SCORER.score(i2)[0]
    score_i1 = SCORER.score(i1)[0]

    assert score_i1 >= score_i2, (
        f"Adding a CRITICAL indicator lowered the score: {score_i1} < {score_i2}"
    )


# ---------------------------------------------------------------------------
# Concrete regression cases
# ---------------------------------------------------------------------------


def _one(engine: EngineType, severity: Severity) -> Indicator:
    return _make_indicator(uuid.uuid4(), engine, severity)


def test_regression_empty_vs_one_indicator() -> None:
    """Empty set scores 0; a single indicator scores >= 0. Validates: Req 9.8"""
    empty_score = SCORER.score([])[0]
    one_score = SCORER.score([_one(EngineType.FRAUD_RING, Severity.LOW)])[0]
    assert empty_score == 0
    assert one_score >= empty_score


def test_regression_adding_critical_strictly_not_lower() -> None:
    """Adding a CRITICAL indicator to a set does not decrease the score.

    Validates: Requirements 9.8
    """
    claim_id = uuid.uuid4()
    base = [
        _make_indicator(claim_id, EngineType.REGIONAL_ANALYSIS, Severity.LOW),
        _make_indicator(claim_id, EngineType.GEOGRAPHIC_ANOMALY, Severity.MEDIUM),
    ]
    with_critical = list(base) + [
        _make_indicator(claim_id, EngineType.FRAUD_RING, Severity.CRITICAL)
    ]

    base_score = SCORER.score(base)[0]
    critical_score = SCORER.score(with_critical)[0]
    assert critical_score >= base_score


def test_regression_severity_upgrade_via_superset() -> None:
    """A superset that adds a HIGH engine indicator never lowers the score.

    Validates: Requirements 9.8
    """
    claim_id = uuid.uuid4()
    i2 = [_make_indicator(claim_id, EngineType.REPETITIVE_PATTERN, Severity.MEDIUM)]
    i1 = list(i2) + [
        _make_indicator(claim_id, EngineType.DOCUMENT_FORGERY, Severity.HIGH)
    ]
    assert SCORER.score(i1)[0] >= SCORER.score(i2)[0]
