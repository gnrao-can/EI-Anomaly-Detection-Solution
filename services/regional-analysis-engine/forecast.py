"""
services/regional-analysis-engine/forecast.py
=============================================
Prophet time-series forecast for the Regional Analysis Engine (task 16.2).

This module fits a Prophet model on the trailing regional unemployment history
and produces a one-step-ahead forecast (month N+1) with a 95% prediction
interval. The forecast is recorded for MONITORING ONLY — it is NOT an Indicator
trigger (task 16.2 detail; the P14 calibration property is validated in task
16.4).

Prophet dependency isolation
-----------------------------
The heavyweight ``prophet`` dependency (pinned ``prophet==1.1.6`` in
``scripts/dependencies/python-versions.toml``) and its transitive numeric stack
are NOT guaranteed to be installed in every environment (e.g. the CI/test
sandbox). To keep the engine importable and unit-testable without Prophet, the
import is performed lazily inside :func:`fit_forecast` and guarded by
:data:`PROPHET_AVAILABLE`. When Prophet is unavailable, a documented fallback
estimator (:func:`_fallback_forecast`) produces a forecast from the trailing
mean and sample standard deviation using a normal 95% interval, and the result
is flagged ``model="fallback"`` so callers/monitoring can distinguish it from a
true Prophet fit. This keeps the module — and the whole engine — importable and
testable in minimal environments while the production container (which installs
Prophet) exercises the real model path.

Prophet model configuration (task 16.2 detail):
  * ``seasonality_mode="multiplicative"``
  * ``yearly_seasonality=True`` (annual seasonality)
  * ``changepoint_prior_scale=0.05``
  * ``interval_width=0.95``

Requirements: 8.9 (forecast recorded within the time budget), 8.11 (forecast
calibration — property test in task 16.4).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional, Sequence

#: The Prophet model configuration mandated by task 16.2. Kept as a module-level
#: constant so both the real fit and any documentation/tests reference one
#: source of truth.
PROPHET_CONFIG: dict[str, Any] = {
    "seasonality_mode": "multiplicative",
    "yearly_seasonality": True,
    "changepoint_prior_scale": 0.05,
    "interval_width": 0.95,
}

#: Minimum number of observations required to attempt a forecast.
MIN_OBSERVATIONS = 2

#: z-score for a two-sided 95% interval under a normal approximation, used by
#: the fallback estimator only (large-sample limit of the t-quantile below).
_Z_95 = 1.959963984540054


def _t_quantile_975(df: int) -> float:
    """Return the two-sided 95% (upper-tail 0.975) Student-t quantile for ``df``.

    The fallback prediction interval must be *calibrated* to ~95% coverage even
    for the short histories the engine fits (N as small as 12). A plain normal
    z-score under-covers at small samples, so we use the Student-t quantile with
    ``df`` degrees of freedom. SciPy provides it exactly; where SciPy is
    unavailable we fall back to the normal quantile (``df`` large), which is the
    correct asymptotic value.
    """
    if df < 1:
        return _Z_95
    try:  # pragma: no cover - exact path when SciPy is installed
        from scipy.stats import t as _t

        return float(_t.ppf(0.975, df))
    except Exception:  # noqa: BLE001 - SciPy absent: use the normal limit
        return _Z_95


def _prophet_available() -> bool:
    """Return ``True`` when the ``prophet`` package can be imported."""
    try:  # pragma: no cover - depends on the environment
        import prophet  # noqa: F401

        return True
    except Exception:  # noqa: BLE001 - any import failure means "unavailable"
        return False


#: Cached at import time; ``fit_forecast`` re-checks lazily so tests can force
#: the fallback path regardless.
PROPHET_AVAILABLE = _prophet_available()


@dataclass(frozen=True)
class ForecastResult:
    """One-step-ahead forecast (month N+1) with a 95% prediction interval.

    Attributes
    ----------
    yhat:
        Point forecast for month N+1.
    yhat_lower:
        Lower bound of the 95% prediction interval.
    yhat_upper:
        Upper bound of the 95% prediction interval.
    model:
        ``"prophet"`` when produced by a real Prophet fit, ``"fallback"`` when
        produced by the normal-approximation estimator used where Prophet is
        unavailable.
    n_observations:
        Number of historical observations the forecast was fit on.
    interval_width:
        The nominal coverage of the prediction interval (0.95).
    """

    yhat: float
    yhat_lower: float
    yhat_upper: float
    model: str
    n_observations: int
    interval_width: float = 0.95

    def contains(self, actual: float) -> bool:
        """Return ``True`` when *actual* lies within the prediction interval."""
        return self.yhat_lower <= actual <= self.yhat_upper

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation for monitoring payloads."""
        return {
            "yhat": self.yhat,
            "yhat_lower": self.yhat_lower,
            "yhat_upper": self.yhat_upper,
            "model": self.model,
            "n_observations": self.n_observations,
            "interval_width": self.interval_width,
        }


def _monthly_dates(n: int, start: Optional[date]) -> list[date]:
    """Build ``n`` consecutive month-start dates ending at the last observation."""
    anchor = start or date(2000, 1, 1)
    year, month = anchor.year, anchor.month
    dates: list[date] = []
    for _ in range(n):
        dates.append(date(year, month, 1))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return dates


def _fallback_forecast(values: Sequence[float]) -> ForecastResult:
    """Forecast month N+1 from the trailing mean with a calibrated 95% interval.

    Used only when Prophet is unavailable. The point forecast is the sample
    mean of the history. The 95% prediction interval is built to be *calibrated*
    — i.e. to actually contain the held-out month-(N+1) observation ~95% of the
    time (the P14 calibration property, task 16.4) — rather than merely nominal.
    Concretely the half-width is::

        t_{0.975, N-1} * s * sqrt(1 + 1/N)

    where ``s`` is the *sample* standard deviation (Bessel-corrected, /(N-1)),
    ``t_{0.975, N-1}`` is the Student-t quantile that corrects the plain normal
    z-score for small-sample estimation of the variance, and the
    ``sqrt(1 + 1/N)`` factor is the standard one-step-ahead *prediction* (not
    confidence) interval inflation that accounts for the uncertainty in the
    estimated mean in addition to the intrinsic noise. Together these make the
    interval well-calibrated even for the short (N >= 12) histories the engine
    fits; a plain ``mean ± z * pstdev`` interval systematically under-covers.

    With a single distinct value the standard deviation is 0 and the interval
    collapses to the point forecast, which still contains that constant series.
    """
    n = len(values)
    mean = statistics.fmean(values)
    if n >= 2:
        # Sample (Bessel-corrected) standard deviation — an unbiased-scale
        # estimate of the process noise, unlike the population std which is
        # biased low and would narrow the interval.
        std = statistics.stdev(values)
    else:
        std = 0.0
    # Calibrated one-step-ahead 95% prediction interval half-width.
    prediction_factor = math.sqrt(1.0 + 1.0 / n) if n >= 1 else 1.0
    half_width = _t_quantile_975(n - 1) * std * prediction_factor
    return ForecastResult(
        yhat=mean,
        yhat_lower=mean - half_width,
        yhat_upper=mean + half_width,
        model="fallback",
        n_observations=n,
        interval_width=0.95,
    )


def _prophet_forecast(
    values: Sequence[float], start: Optional[date]
) -> ForecastResult:
    """Fit Prophet with the mandated config and forecast month N+1.

    Imported lazily so the module stays importable without Prophet. Any failure
    inside the fit is allowed to propagate to :func:`fit_forecast`, which falls
    back to the normal-approximation estimator.
    """
    import pandas as pd  # pragma: no cover - only on the real Prophet path
    from prophet import Prophet  # pragma: no cover

    n = len(values)
    ds = _monthly_dates(n, start)
    frame = pd.DataFrame({"ds": pd.to_datetime(ds), "y": list(values)})

    model = Prophet(**PROPHET_CONFIG)
    model.fit(frame)
    future = model.make_future_dataframe(periods=1, freq="MS")
    forecast = model.predict(future)
    last = forecast.iloc[-1]
    return ForecastResult(
        yhat=float(last["yhat"]),
        yhat_lower=float(last["yhat_lower"]),
        yhat_upper=float(last["yhat_upper"]),
        model="prophet",
        n_observations=n,
        interval_width=float(PROPHET_CONFIG["interval_width"]),
    )


def fit_forecast(
    history: Sequence[float],
    *,
    start_month: Optional[date] = None,
    force_fallback: bool = False,
) -> ForecastResult:
    """Fit a forecast for month N+1 from a regional unemployment ``history``.

    Args:
        history: Monthly regional unemployment observations, oldest first. The
            engine feeds a 24-month trailing window (task 16.2) but any series
            of at least :data:`MIN_OBSERVATIONS` observations is accepted.
        start_month: Optional month-start date of the FIRST observation, used to
            build the Prophet ``ds`` column. Defaults to an arbitrary anchor
            (only relative spacing matters).
        force_fallback: When ``True`` (or when Prophet is unavailable / raises),
            the normal-approximation fallback estimator is used. Exposed so unit
            tests can exercise the fallback deterministically.

    Returns:
        A :class:`ForecastResult` whose ``model`` field records which path
        produced it (``"prophet"`` or ``"fallback"``).

    Raises:
        ValueError: When fewer than :data:`MIN_OBSERVATIONS` observations are
            supplied.
    """
    values = [float(v) for v in history]
    if len(values) < MIN_OBSERVATIONS:
        raise ValueError(
            f"forecast requires at least {MIN_OBSERVATIONS} observations, "
            f"got {len(values)}"
        )

    if force_fallback or not _prophet_available():
        return _fallback_forecast(values)

    try:  # pragma: no cover - exercised only where Prophet is installed
        return _prophet_forecast(values, start_month)
    except Exception:  # noqa: BLE001 - never let a fit failure crash the engine
        return _fallback_forecast(values)


__all__ = [
    "PROPHET_CONFIG",
    "PROPHET_AVAILABLE",
    "MIN_OBSERVATIONS",
    "ForecastResult",
    "fit_forecast",
]
