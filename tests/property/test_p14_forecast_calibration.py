"""
tests/property/test_p14_forecast_calibration.py
================================================
Property 14: Time-Series Forecast Calibration. Validates: Requirements 8.11

Statement (design.md — "Property 14: Time-Series Forecast Calibration")
-----------------------------------------------------------------------
*For any* historical regional unemployment time series of N >= 12 monthly
observations, the Regional_Analysis_Engine's time-series model SHALL produce a
forecast for month N+1 whose 95% prediction interval contains the actual
observed value, across at least 95% of all such series in the historical
validation dataset.

Note the shape of the claim: it is a **coverage rate** statement, not a
per-series guarantee. A well-calibrated 95% prediction interval is, by
definition, allowed to miss the actual value ~5% of the time; what must hold is
that *across many series* at least 95% of the N+1 actuals fall inside their
interval. This test therefore AGGREGATES coverage across all Hypothesis-drawn
series and asserts the aggregate coverage rate is >= 95%, rather than requiring
every single series to be contained.

What this test drives
---------------------
``services/regional-analysis-engine/forecast.py`` (task 16.2) exposes
:func:`fit_forecast(history)` which fits a Prophet model (config:
multiplicative seasonality, yearly seasonality, changepoint_prior_scale=0.05,
interval_width=0.95) and returns a :class:`ForecastResult` for month N+1 with a
95% prediction interval and a :meth:`ForecastResult.contains` predicate.

Prophet fallback path
---------------------
``forecast.py`` documents a normal-approximation fallback used when the
``prophet`` package is unavailable (as in the CI/test sandbox): the point
forecast is the trailing sample mean and the 95% interval is
``mean +/- z_0.95 * sigma``. The calibration invariant is designed to hold for
BOTH paths. To exercise the invariant fairly for whichever path runs, each
series is generated as N+1 observations drawn from the SAME stationary process
(a region-specific base rate plus i.i.d. normal month-to-month noise). We fit on
the first N observations and test whether observation N+1 — an independent draw
from the same distribution the history came from — lands inside the model's 95%
interval. For a correctly-scaled normal 95% interval this happens ~95% of the
time, so aggregating over many series yields a coverage rate at/above 0.95.

Module loading
--------------
``services/regional-analysis-engine`` is a hyphenated directory (not an
importable package) whose modules import each other by bare name. We load
``forecast`` through :func:`tests.conftest.load_service_module` with the FULL
sibling set so the whole property suite stays collision-free when run in a
single process.

Property 14: Time-Series Forecast Calibration. Validates: Requirements 8.11
"""

from __future__ import annotations

import math
from statistics import NormalDist

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Standard-normal inverse CDF (quantile function) used to turn Hypothesis
# uniform draws into i.i.d. standard-normal shocks.
_STD_NORMAL = NormalDist(0.0, 1.0)


def _inv_norm_cdf(u: float) -> float:
    """Map ``u`` in (0, 1) to a standard-normal quantile."""
    return _STD_NORMAL.inv_cdf(u)

from tests.conftest import load_service_module

# ---------------------------------------------------------------------------
# Load the hyphenated regional-analysis-engine ``forecast`` module with its
# full sibling set (collision-free, per tests/conftest.load_service_module).
# ---------------------------------------------------------------------------
_forecast = load_service_module(
    "regional-analysis-engine",
    "forecast",
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
fit_forecast = _forecast.fit_forecast
ForecastResult = _forecast.ForecastResult


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

# Calibration is inherently a statistical property that needs the full example
# budget: aborting early (e.g. on a per-example deadline triggered by a one-off
# cost such as the first SciPy import used to compute the t-quantile) would
# leave the coverage tally under-populated and make the aggregate check flaky.
# We therefore pin an explicit deadline-free settings object on the property
# case below rather than relying solely on the shared "ci" profile.
_P14_SETTINGS = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
#
# A regional unemployment series is generated from a stationary, region-specific
# process so that the (N+1)th observation is an independent draw from the SAME
# distribution as the N observations we fit on. This is exactly the setting in
# which a 95% prediction interval should cover ~95% of held-out actuals.
#
#   * ``base``  — the region's mean unemployment rate (a realistic 2%..15%).
#   * ``sigma`` — month-to-month noise scale (0.1..1.5 pp), the volatility the
#                 model must learn from the history.
#   * ``n``     — number of historical observations, N >= 12 per the property.
#   * ``noise`` — a list of length N+1 of standard-normal shocks; the series is
#                 ``base + sigma * noise[t]`` and observation N+1 uses noise[N].

_N_MIN = 12
_N_MAX = 36


@st.composite
def calibration_series(draw):
    """Draw ``(history, actual_next, sigma)`` for one region.

    ``history`` is a list of N (>= 12) monthly rates; ``actual_next`` is the
    (N+1)th rate drawn from the same stationary process; ``sigma`` is the true
    noise scale (returned for diagnostics only).
    """
    n = draw(st.integers(min_value=_N_MIN, max_value=_N_MAX))
    base = draw(st.floats(min_value=2.0, max_value=15.0))
    sigma = draw(st.floats(min_value=0.1, max_value=1.5))
    # N history shocks + 1 shock for the held-out month N+1. Hypothesis has no
    # native Gaussian primitive, so we draw uniforms in (0, 1) and map them
    # through the inverse standard-normal CDF to obtain i.i.d. standard-normal
    # shocks. This keeps the held-out (N+1)th observation an independent draw
    # from the SAME stationary process as the history — precisely the setting a
    # 95% prediction interval is meant to cover ~95% of the time.
    uniforms = draw(
        st.lists(
            st.floats(
                min_value=1e-6,
                max_value=1.0 - 1e-6,
                allow_nan=False,
                allow_infinity=False,
            ),
            min_size=n + 1,
            max_size=n + 1,
        )
    )
    shocks = [_inv_norm_cdf(u) for u in uniforms]
    series = [base + sigma * s for s in shocks]
    history = series[:n]
    actual_next = series[n]
    return history, actual_next, sigma


# ===========================================================================
# Property 14 — aggregate 95% prediction-interval coverage >= 95%.
# ===========================================================================
#
# Hypothesis drives ONE series per example; we accumulate coverage into a
# module-level tally and assert the aggregate rate at teardown. The per-example
# test also records that fit_forecast returns a well-formed 95% interval.

_COVERAGE = {"hits": 0, "total": 0}


@_P14_SETTINGS
@given(data=calibration_series())
def test_P14_forecast_interval_is_well_formed_and_records_coverage(data):
    """Property 14: Time-Series Forecast Calibration. Validates: Requirements 8.11

    For each generated series, ``fit_forecast`` on the first N observations
    yields a valid 95% prediction interval (lower <= yhat <= upper, nominal
    width 0.95), and we record whether the held-out month-(N+1) actual falls
    inside it. Aggregate coverage is asserted separately (see
    :func:`test_P14_aggregate_coverage_at_least_95_percent`).
    """
    history, actual_next, _sigma = data

    result = fit_forecast(history)

    # The interval is well-formed and carries the mandated 95% nominal width.
    assert result.interval_width == 0.95
    assert result.n_observations == len(history)
    assert result.yhat_lower <= result.yhat <= result.yhat_upper
    assert math.isfinite(result.yhat_lower)
    assert math.isfinite(result.yhat_upper)

    # Record coverage of the independent (N+1)th actual for the aggregate check.
    _COVERAGE["total"] += 1
    if result.contains(actual_next):
        _COVERAGE["hits"] += 1


def test_P14_aggregate_coverage_at_least_95_percent():
    """Property 14: Time-Series Forecast Calibration. Validates: Requirements 8.11

    Across all generated series, the fraction whose month-(N+1) actual falls
    within the model's 95% prediction interval SHALL be at least 95% — the
    coverage-rate calibration guarantee. This test depends on the property test
    above having populated the tally; pytest collects tests in file order so the
    ``@given`` case (which runs many examples) executes first.
    """
    total = _COVERAGE["total"]
    hits = _COVERAGE["hits"]

    assert total > 0, (
        "no coverage samples recorded — the property case must run before this "
        "aggregate check"
    )

    coverage = hits / total
    assert coverage >= 0.95, (
        f"forecast 95% interval coverage {coverage:.4f} "
        f"({hits}/{total}) fell below the 0.95 calibration threshold"
    )


# ===========================================================================
# Edge cases — enumerated explicitly (not property-generated).
# ===========================================================================


def test_P14_constant_series_contains_actual():
    """Property 14: Time-Series Forecast Calibration. Validates: Requirements 8.11

    A perfectly flat 12-month history forecasts the constant and its interval
    (which collapses to the point forecast under the fallback) contains the
    repeated value.
    """
    history = [5.5] * 12
    result = fit_forecast(history)
    assert result.contains(5.5)
    assert result.yhat_lower <= result.yhat <= result.yhat_upper


def test_P14_minimum_length_series_produces_valid_interval():
    """Property 14: Time-Series Forecast Calibration. Validates: Requirements 8.11

    The minimum N = 12 series still yields a well-formed 95% interval with the
    mandated nominal width.
    """
    history = [5.0, 5.2, 4.9, 5.1, 5.3, 5.0, 4.8, 5.2, 5.1, 4.9, 5.0, 5.4]
    result = fit_forecast(history)
    assert result.interval_width == 0.95
    assert result.n_observations == 12
    assert result.yhat_lower <= result.yhat <= result.yhat_upper
