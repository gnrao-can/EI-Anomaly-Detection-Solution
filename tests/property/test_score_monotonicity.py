"""
tests/property/test_score_monotonicity.py
==========================================
Property 9: Risk Score monotonicity. Validates: Requirements 9.8

Statement
---------
For indicator sets I1 ⊃ I2 (I1 is a superset of I2), ``score(I1) >= score(I2)``.

Adding indicators to a claim's evidence set must never *lower* its Risk_Score.
This is the documented guarantee of the deterministic heuristic fallback in
:meth:`EnsembleScorer.score` (see ``scorer.py`` — "The heuristic is monotonic in
the Indicator set"). Arbitrary *trained* XGBoost / Isolation Forest models are
not guaranteed monotonic, so this property deliberately targets the heuristic
path by constructing the scorer with a non-existent ``model_dir``.

The ensemble-scorer service directory contains a hyphen, so it is not importable
as a package. We add it to ``sys.path`` here (mirroring ``tests/unit/test_scorer.py``)
so ``features`` and ``scorer`` can be imported directly.

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
# A single heuristic scorer instance.
#
# Pointing at a non-existent model_dir forces the deterministic monotonic
# heuristic path — the mode whose monotonicity we guarantee (P9). The scorer is
# stateless w.r.t. scoring, so one instance is safe to reuse across examples.
# ---------------------------------------------------------------------------

_HEURISTIC_SCORER = EnsembleScorer(model_dir=_SERVICE_DIR / "__no_such_model_dir__")
assert _HEURISTIC_SCORER.using_fallback is True, (
    "Test requires the heuristic fallback path; a real model dir was found."
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_ENGINES = list(EngineType)
_SEVERITIES = list(Severity)


@st.composite
def indicators(draw: st.DrawFn, min_size: int, max_size: int) -> list[Indicator]:
    """Generate a list of valid :class:`Indicator` objects.

    Each Indicator has a randomised :class:`EngineType` and :class:`Severity`;
    the remaining fields are fixed placeholders, as the scorer only reads
    ``engine`` and ``severity``.
    """
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    claim_id = uuid.uuid4()
    result: list[Indicator] = []
    for _ in range(n):
        engine = draw(st.sampled_from(_ENGINES))
        severity = draw(st.sampled_from(_SEVERITIES))
        result.append(
            Indicator(
                indicator_id=uuid.uuid4(),
                claim_id=claim_id,
                engine=engine,
                severity=severity,
                algorithm="prop_test",
                title="prop_test",
                explanation_en="prop_test",
                explanation_fr="prop_test",
                payload={},
                produced_at=datetime.now(timezone.utc),
            )
        )
    return result


# ---------------------------------------------------------------------------
# Property 9 — general superset monotonicity
# ---------------------------------------------------------------------------


@given(
    base=indicators(min_size=0, max_size=15),
    extra=indicators(min_size=1, max_size=10),
)
def test_score_monotonic_superset(base: list[Indicator], extra: list[Indicator]) -> None:
    """For I1 = I2 + extra (I1 ⊃ I2), score(I1) >= score(I2).

    Covers adding indicators to both the empty set and non-empty sets, since
    ``base`` may be empty or populated. ``extra`` is always non-empty, so I1 is
    a strict superset of I2.
    """
    i2 = base
    i1 = base + extra  # I1 ⊃ I2

    score_i2 = _HEURISTIC_SCORER.score(i2)[0]
    score_i1 = _HEURISTIC_SCORER.score(i1)[0]

    assert score_i1 >= score_i2, (
        f"Monotonicity violated: score(I1)={score_i1} < score(I2)={score_i2}; "
        f"|I2|={len(i2)}, |extra|={len(extra)}"
    )


# ---------------------------------------------------------------------------
# Property 9 — adding to the EMPTY set specifically
# ---------------------------------------------------------------------------


@given(extra=indicators(min_size=1, max_size=10))
def test_score_monotonic_from_empty(extra: list[Indicator]) -> None:
    """score([] + extra) >= score([]) == 0."""
    empty_score = _HEURISTIC_SCORER.score([])[0]
    assert empty_score == 0  # P8 sanity: empty set scores 0

    added_score = _HEURISTIC_SCORER.score(extra)[0]
    assert added_score >= empty_score, (
        f"Adding indicators to the empty set lowered the score: "
        f"{added_score} < {empty_score}"
    )


# ---------------------------------------------------------------------------
# Property 9 — adding HIGHER-severity indicators never lowers the score
# ---------------------------------------------------------------------------


@given(
    base=indicators(min_size=0, max_size=15),
    n_extra=st.integers(min_value=1, max_value=8),
    high_severity=st.sampled_from([Severity.HIGH, Severity.CRITICAL]),
    engine=st.sampled_from(_ENGINES),
)
def test_score_monotonic_adding_high_severity(
    base: list[Indicator],
    n_extra: int,
    high_severity: Severity,
    engine: EngineType,
) -> None:
    """Appending HIGH/CRITICAL indicators to any base set never lowers score."""
    claim_id = base[0].claim_id if base else uuid.uuid4()
    extra = [
        Indicator(
            indicator_id=uuid.uuid4(),
            claim_id=claim_id,
            engine=engine,
            severity=high_severity,
            algorithm="prop_test_high",
            title="prop_test_high",
            explanation_en="prop_test_high",
            explanation_fr="prop_test_high",
            payload={},
            produced_at=datetime.now(timezone.utc),
        )
        for _ in range(n_extra)
    ]

    i2 = base
    i1 = base + extra

    score_i2 = _HEURISTIC_SCORER.score(i2)[0]
    score_i1 = _HEURISTIC_SCORER.score(i1)[0]

    assert score_i1 >= score_i2, (
        f"Adding {n_extra} {high_severity.value} indicator(s) lowered the score: "
        f"{score_i1} < {score_i2}"
    )
