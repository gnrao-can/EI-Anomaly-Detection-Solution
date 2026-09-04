"""
tests/property/test_p13_zero_rate_deviation.py
==============================================
Property 13: Zero Rate Deviation Invariant. Validates: Requirements 8.10

Statement (design.md — "Property 13: Zero Rate Deviation Invariant")
--------------------------------------------------------------------
*For any* pair ``(regional_rate, national_rate)`` where the two values are
numerically equal, the Regional_Analysis_Engine SHALL compute a deviation value
of exactly ``0`` and SHALL NOT generate a regional unemployment Indicator.

What this test drives
---------------------
The engine's rate-deviation logic lives in
``services/regional-analysis-engine/analysis.py`` (task 16.1):

  * :func:`rate_deviation(regional, national)` returns ``regional - national``
    using EXACT :class:`~decimal.Decimal` arithmetic; and
  * :func:`check_rate_deviation` emits a MEDIUM regional-unemployment Indicator
    *iff* the deviation is STRICTLY below the (negative) threshold. Equal rates
    give a deviation of exactly ``Decimal("0")`` and therefore never flag.

Using Hypothesis we draw a single ``rate`` and pass it as BOTH the regional and
national value (guaranteeing numeric equality). We then assert:

  1. ``rate_deviation(rate, rate) == Decimal("0")`` — the deviation is exactly
     zero (not merely close to zero), because Decimal subtraction of equal
     operands is exact; and
  2. ``check_rate_deviation(rate, rate)`` returns an empty list — no regional
     unemployment Indicator is produced.

We also cross the threshold with a range of ``threshold_pp`` values (all
negative, per the design) to show the "equal rates never flag" invariant holds
regardless of the exact threshold used.

Module loading
--------------
``services/regional-analysis-engine`` is a hyphenated directory (not an
importable package) and its modules import each other by bare name. We load
``analysis`` through :func:`tests.conftest.load_service_module` with the FULL
sibling set so the whole test suite stays collision-free when run in a single
process. ``analysis`` itself only depends on ``shared.models``; the remaining
siblings are provided for isolation parity with the rest of the engine's tests.

Property 13: Zero Rate Deviation Invariant. Validates: Requirements 8.10
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.conftest import load_service_module

# ---------------------------------------------------------------------------
# Load the hyphenated regional-analysis-engine ``analysis`` module with its
# full sibling set (collision-free, per tests/conftest.load_service_module).
# ---------------------------------------------------------------------------
_analysis = load_service_module(
    "regional-analysis-engine",
    "analysis",
    siblings=[
        "config",
        "analysis",
        "client",
        "forecast",
        "seasonality",
        "providers",
        "kafka_producer",
        "main",
    ],
)
rate_deviation = _analysis.rate_deviation
check_rate_deviation = _analysis.check_rate_deviation
DEFAULT_RATE_DEVIATION_THRESHOLD_PP = _analysis.DEFAULT_RATE_DEVIATION_THRESHOLD_PP


# ---------------------------------------------------------------------------
# Hypothesis profile: CI runs 200 examples (design.md — Testing Strategy;
# tasks.md Notes: settings.register_profile("ci", max_examples=200, ...)).
# Registration is guarded so a duplicate registration from a sibling
# property-test module already loaded in the same session does not raise; we
# then always (re)load the "ci" profile so this file runs 200 examples.
# ---------------------------------------------------------------------------
try:
    settings.register_profile(
        "ci", max_examples=200, suppress_health_check=[HealthCheck.too_slow]
    )
except Exception:  # noqa: BLE001 - already registered elsewhere
    pass
settings.load_profile("ci")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
#
# Unemployment rates are percentages. We draw finite, non-NaN Decimals in a
# bounded, realistic range [0, 100] with up to 4 fractional places (matching the
# >= 4 dp precision LabourForceData retains). The SAME drawn value is used for
# both the regional and national rate so the two are numerically equal.
rate_strategy = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("100"),
    places=4,
    allow_nan=False,
    allow_infinity=False,
)

# Detection thresholds are negative percentage-point values (design: default
# -1.5 pp; an Indicator fires only when deviation is strictly below this). We
# vary the threshold to show the invariant is threshold-independent for equal
# rates.
threshold_strategy = st.decimals(
    min_value=Decimal("-10"),
    max_value=Decimal("0"),
    places=4,
    allow_nan=False,
    allow_infinity=False,
)


# ===========================================================================
# Property 13 — equal rates yield exactly zero deviation and no Indicator.
# ===========================================================================


@given(rate=rate_strategy)
def test_P13_equal_rates_zero_deviation(rate):
    """Property 13: Zero Rate Deviation Invariant. Validates: Requirements 8.10

    For any rate, ``rate_deviation(rate, rate)`` is EXACTLY ``Decimal("0")``.
    Decimal subtraction of equal operands is exact, so this is a true zero, not
    an approximation.
    """
    deviation = rate_deviation(rate, rate)
    assert deviation == Decimal("0")
    # Exact-zero, not merely close: a Decimal that equals 0 has a zero coefficient.
    assert deviation.is_zero()


@given(rate=rate_strategy)
def test_P13_equal_rates_no_indicator_default_threshold(rate):
    """Property 13: Zero Rate Deviation Invariant. Validates: Requirements 8.10

    With equal regional and national rates, ``check_rate_deviation`` produces no
    regional unemployment Indicator under the default -1.5 pp threshold.
    """
    indicators = check_rate_deviation(rate, rate)
    assert indicators == []


@given(rate=rate_strategy, threshold_pp=threshold_strategy)
def test_P13_equal_rates_no_indicator_any_negative_threshold(rate, threshold_pp):
    """Property 13: Zero Rate Deviation Invariant. Validates: Requirements 8.10

    Equal rates give a zero deviation, which is never strictly below any
    negative threshold, so no Indicator is produced regardless of the exact
    (negative) threshold value in force.
    """
    # Sanity: the deviation feeding the check is exactly zero.
    assert rate_deviation(rate, rate) == Decimal("0")

    indicators = check_rate_deviation(rate, rate, threshold_pp=threshold_pp)
    assert indicators == []


# ===========================================================================
# Edge cases — enumerated explicitly (not property-generated).
# ===========================================================================


def test_P13_zero_rate_pair():
    """Property 13: Zero Rate Deviation Invariant. Validates: Requirements 8.10

    The degenerate 0.0 == 0.0 pair yields a zero deviation and no Indicator.
    """
    assert rate_deviation(Decimal("0"), Decimal("0")) == Decimal("0")
    assert check_rate_deviation(Decimal("0"), Decimal("0")) == []


def test_P13_high_precision_equal_rates():
    """Property 13: Zero Rate Deviation Invariant. Validates: Requirements 8.10

    A high-precision rate equal on both sides still yields an exact zero
    deviation and no Indicator (no float error is introduced).
    """
    rate = Decimal("6.12345678")
    assert rate_deviation(rate, rate) == Decimal("0")
    assert check_rate_deviation(rate, rate) == []


def test_P13_trailing_zero_equal_rates_are_exact_zero():
    """Property 13: Zero Rate Deviation Invariant. Validates: Requirements 8.10

    ``Decimal("5.50") - Decimal("5.50")`` compares equal to ``Decimal("0")``
    and reports as a true zero, and produces no Indicator.
    """
    rate = Decimal("5.50")
    deviation = rate_deviation(rate, rate)
    assert deviation == Decimal("0")
    assert deviation.is_zero()
    assert check_rate_deviation(rate, rate) == []
