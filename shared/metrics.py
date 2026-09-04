"""
shared/metrics.py
=================
Single source of truth for EI-ADS Prometheus metrics and FastAPI
instrumentation (task 31.1).

This module defines every application-level metric named in the design's
"Prometheus Metric Definitions" section and exposes a single helper,
:func:`instrument_app`, that each microservice calls once, right after it
constructs its ``FastAPI`` app, to:

  * attach ``prometheus-fastapi-instrumentator`` (default HTTP request/latency
    metrics), and
  * expose a ``GET /metrics`` endpoint in the Prometheus text exposition format.

Metric families (design §"Prometheus Metric Definitions", Requirements 21.1–21.3)
---------------------------------------------------------------------------------
Detection engines (Req 21.1):
  * ``ei_ads_engine_requests_total{engine, status}``            — Counter
  * ``ei_ads_engine_latency_seconds{engine}``                   — Histogram
  * ``ei_ads_engine_indicators_generated_total{engine, severity}`` — Counter
  * ``ei_ads_engine_errors_total{engine, error_type}``          — Counter

External data adapters (Req 21.3):
  * ``ei_ads_adapter_requests_total{adapter, status}``          — Counter
  * ``ei_ads_adapter_latency_seconds{adapter}``                 — Histogram
  * ``ei_ads_adapter_cache_hits_total{adapter}``                — Counter
  * ``ei_ads_adapter_cache_misses_total{adapter}``              — Counter
  * ``ei_ads_adapter_error_rate{adapter}``                      — Gauge (5-min window)

Model quality (Req 21.2, 21.6):
  * ``ei_ads_model_fraud_detection_rate``                       — Gauge
  * ``ei_ads_model_false_positive_rate``                        — Gauge
  * ``ei_ads_model_brier_score``                                — Gauge
  * ``ei_ads_model_drift_detected``                             — Gauge (0|1)

Graceful degradation
---------------------
``prometheus_client`` and ``prometheus_fastapi_instrumentator`` are optional at
import time. When either is absent (e.g. a slim sandbox), this module falls back
to inert no-op stand-ins so that **importing a service never fails** for lack of
these packages. :func:`instrument_app` then becomes a no-op and no ``/metrics``
route is added. Pinned versions live in
``scripts/dependencies/python-versions.toml`` (``[runtime.observability]``).

Requirements: 21.1, 21.2, 21.3
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Optional-dependency import guards
# ---------------------------------------------------------------------------
try:  # pragma: no cover - exercised only when prometheus_client is present
    from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
    from prometheus_client import REGISTRY as _DEFAULT_REGISTRY

    _PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover - slim sandbox without prometheus_client
    CollectorRegistry = None  # type: ignore[assignment,misc]
    _DEFAULT_REGISTRY = None  # type: ignore[assignment]
    _PROMETHEUS_AVAILABLE = False

    class _NoopMetric:
        """Inert metric used when ``prometheus_client`` is unavailable.

        Supports the ``labels(...)`` / ``inc()`` / ``observe()`` / ``set()``
        surface used across the codebase so call sites never need to branch on
        whether Prometheus is installed.
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
            pass

        def labels(self, *args: Any, **kwargs: Any) -> "_NoopMetric":
            return self

        def inc(self, *args: Any, **kwargs: Any) -> None:
            pass

        def observe(self, *args: Any, **kwargs: Any) -> None:
            pass

        def set(self, *args: Any, **kwargs: Any) -> None:
            pass

    # Constructors are shadowed by the no-op so metric definitions below are
    # always valid, whether or not the real library is installed.
    Counter = Gauge = Histogram = _NoopMetric  # type: ignore[assignment,misc]

try:  # pragma: no cover - exercised only when the instrumentator is present
    from prometheus_fastapi_instrumentator import Instrumentator

    _INSTRUMENTATOR_AVAILABLE = True
except ImportError:  # pragma: no cover - slim sandbox without the instrumentator
    Instrumentator = None  # type: ignore[assignment,misc]
    _INSTRUMENTATOR_AVAILABLE = False


# ---------------------------------------------------------------------------
# Latency histogram buckets (seconds)
# ---------------------------------------------------------------------------
# Chosen to give useful resolution around the design's latency targets: the
# 400 ms per-engine budget and the 3 s end-to-end p99 ceiling.
_LATENCY_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.2,
    0.4,
    0.8,
    1.5,
    3.0,
    5.0,
    10.0,
)


# ---------------------------------------------------------------------------
# Detection engine metrics (Requirement 21.1)
# ---------------------------------------------------------------------------
ENGINE_REQUESTS_TOTAL = Counter(
    "ei_ads_engine_requests_total",
    "Total analysis requests handled by a Detection Engine.",
    ["engine", "status"],
)
ENGINE_LATENCY_SECONDS = Histogram(
    "ei_ads_engine_latency_seconds",
    "Detection Engine analysis latency in seconds.",
    ["engine"],
    buckets=_LATENCY_BUCKETS,
)
ENGINE_INDICATORS_GENERATED_TOTAL = Counter(
    "ei_ads_engine_indicators_generated_total",
    "Total Indicators generated by a Detection Engine.",
    ["engine", "severity"],
)
ENGINE_ERRORS_TOTAL = Counter(
    "ei_ads_engine_errors_total",
    "Total errors raised while a Detection Engine processed a request.",
    ["engine", "error_type"],
)


# ---------------------------------------------------------------------------
# External data adapter metrics (Requirement 21.3)
# ---------------------------------------------------------------------------
ADAPTER_REQUESTS_TOTAL = Counter(
    "ei_ads_adapter_requests_total",
    "Total upstream requests made by an External Data Adapter.",
    ["adapter", "status"],
)
ADAPTER_LATENCY_SECONDS = Histogram(
    "ei_ads_adapter_latency_seconds",
    "External Data Adapter upstream call latency in seconds.",
    ["adapter"],
    buckets=_LATENCY_BUCKETS,
)
ADAPTER_CACHE_HITS_TOTAL = Counter(
    "ei_ads_adapter_cache_hits_total",
    "Total adapter responses served from cache.",
    ["adapter"],
)
ADAPTER_CACHE_MISSES_TOTAL = Counter(
    "ei_ads_adapter_cache_misses_total",
    "Total adapter cache misses requiring an upstream fetch.",
    ["adapter"],
)
ADAPTER_ERROR_RATE = Gauge(
    "ei_ads_adapter_error_rate",
    "External Data Adapter error rate over the trailing 5-minute window (0..1).",
    ["adapter"],
)


# ---------------------------------------------------------------------------
# Model quality metrics (Requirements 21.2, 21.6)
# ---------------------------------------------------------------------------
MODEL_FRAUD_DETECTION_RATE = Gauge(
    "ei_ads_model_fraud_detection_rate",
    "Rolling 7-day fraud detection rate for the Ensemble Scorer (0..1).",
)
MODEL_FALSE_POSITIVE_RATE = Gauge(
    "ei_ads_model_false_positive_rate",
    "Rolling 7-day false positive rate for the Ensemble Scorer (0..1).",
)
MODEL_BRIER_SCORE = Gauge(
    "ei_ads_model_brier_score",
    "Rolling 7-day Brier calibration score for the Ensemble Scorer (0..1).",
)
MODEL_DRIFT_DETECTED = Gauge(
    "ei_ads_model_drift_detected",
    "Whether model drift is currently detected (1) or not (0).",
)


# ---------------------------------------------------------------------------
# Instrumentation helper
# ---------------------------------------------------------------------------
_VALID_KINDS = frozenset({"engine", "adapter", "service"})

# Track which app objects have already been instrumented so a double call
# (e.g. re-import under a test loader) does not register duplicate routes.
_INSTRUMENTED: "set[int]" = set()


def prometheus_available() -> bool:
    """Return ``True`` when the real Prometheus client is importable."""
    return _PROMETHEUS_AVAILABLE


def instrumentation_available() -> bool:
    """Return ``True`` when the FastAPI instrumentator is importable."""
    return _INSTRUMENTATOR_AVAILABLE


def instrument_app(app: Any, *, kind: str = "service", service_name: str | None = None) -> Any:
    """Attach Prometheus instrumentation and a ``/metrics`` endpoint to ``app``.

    Call this exactly once, immediately after constructing a service's
    ``FastAPI`` app. It wires ``prometheus-fastapi-instrumentator`` (default HTTP
    request count / latency metrics) and exposes ``GET /metrics`` in the
    Prometheus text exposition format.

    Parameters
    ----------
    app:
        The FastAPI application instance.
    kind:
        One of ``"engine"``, ``"adapter"`` or ``"service"``. Recorded on the
        app state (``app.state.metrics_kind``) so downstream code can pick the
        right metric family; it also namespaces the default instrumentator
        metric name.
    service_name:
        Optional explicit service name; defaults to ``app.title`` (or
        ``"ei_ads"``). Used only for labelling the default HTTP metrics.

    Returns
    -------
    The same ``app`` object, so the call can be chained if desired.

    Notes
    -----
    When ``prometheus-fastapi-instrumentator`` is not installed this function is
    a no-op (no ``/metrics`` route is added) and the service still starts
    normally. The application-level metric objects defined in this module remain
    usable (as inert no-ops) regardless.
    """
    if kind not in _VALID_KINDS:
        raise ValueError(f"kind must be one of {sorted(_VALID_KINDS)}, got {kind!r}")

    # Record intent on app state even when instrumentation is unavailable so
    # call sites and tests can introspect it.
    try:
        app.state.metrics_kind = kind
    except AttributeError:  # pragma: no cover - defensive; FastAPI always has .state
        pass

    if not _INSTRUMENTATOR_AVAILABLE:
        # Graceful degradation: no metrics endpoint, but the service imports and
        # runs. See scripts/dependencies for the pinned optional dependency.
        return app

    app_id = id(app)
    if app_id in _INSTRUMENTED:
        return app

    instrumentator = Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        excluded_handlers=["/metrics", "/healthz"],
    )
    instrumentator.instrument(
        app,
        metric_namespace="ei_ads",
        metric_subsystem=kind,
    ).expose(app, endpoint="/metrics", include_in_schema=False)

    _INSTRUMENTED.add(app_id)
    return app


__all__ = [
    # Engine metrics
    "ENGINE_REQUESTS_TOTAL",
    "ENGINE_LATENCY_SECONDS",
    "ENGINE_INDICATORS_GENERATED_TOTAL",
    "ENGINE_ERRORS_TOTAL",
    # Adapter metrics
    "ADAPTER_REQUESTS_TOTAL",
    "ADAPTER_LATENCY_SECONDS",
    "ADAPTER_CACHE_HITS_TOTAL",
    "ADAPTER_CACHE_MISSES_TOTAL",
    "ADAPTER_ERROR_RATE",
    # Model quality metrics
    "MODEL_FRAUD_DETECTION_RATE",
    "MODEL_FALSE_POSITIVE_RATE",
    "MODEL_BRIER_SCORE",
    "MODEL_DRIFT_DETECTED",
    # Helpers
    "instrument_app",
    "prometheus_available",
    "instrumentation_available",
]
