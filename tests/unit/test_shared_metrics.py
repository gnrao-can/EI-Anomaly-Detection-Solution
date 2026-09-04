"""
tests/unit/test_shared_metrics.py
=================================
Unit tests for ``shared/metrics.py`` (task 31.1).

These tests verify:
  * every design-mandated metric object exists with the correct
    ``ei_ads_*`` name (Requirements 21.1, 21.2, 21.3),
  * the metric handles expose the ``labels/inc/observe/set`` surface used by
    call sites (whether or not ``prometheus_client`` is installed),
  * :func:`instrument_app` records the ``kind`` on ``app.state``, rejects
    invalid kinds, and degrades to a no-op when the FastAPI instrumentator is
    absent,
  * when both optional deps are present, ``instrument_app`` adds a ``/metrics``
    endpoint that serves the Prometheus text exposition format.

The suite runs cleanly in a slim sandbox (no prometheus packages) and also
covers the "packages present" path when they happen to be installed.
"""

from __future__ import annotations

import pytest

from shared import metrics


# ---------------------------------------------------------------------------
# Metric definitions (names + label sets) — Requirements 21.1, 21.2, 21.3
# ---------------------------------------------------------------------------

# (attribute, expected metric name)
_EXPECTED_METRICS = [
    # Engine (Req 21.1)
    ("ENGINE_REQUESTS_TOTAL", "ei_ads_engine_requests_total"),
    ("ENGINE_LATENCY_SECONDS", "ei_ads_engine_latency_seconds"),
    ("ENGINE_INDICATORS_GENERATED_TOTAL", "ei_ads_engine_indicators_generated_total"),
    ("ENGINE_ERRORS_TOTAL", "ei_ads_engine_errors_total"),
    # Adapter (Req 21.3)
    ("ADAPTER_REQUESTS_TOTAL", "ei_ads_adapter_requests_total"),
    ("ADAPTER_LATENCY_SECONDS", "ei_ads_adapter_latency_seconds"),
    ("ADAPTER_CACHE_HITS_TOTAL", "ei_ads_adapter_cache_hits_total"),
    ("ADAPTER_CACHE_MISSES_TOTAL", "ei_ads_adapter_cache_misses_total"),
    ("ADAPTER_ERROR_RATE", "ei_ads_adapter_error_rate"),
    # Model quality (Req 21.2)
    ("MODEL_FRAUD_DETECTION_RATE", "ei_ads_model_fraud_detection_rate"),
    ("MODEL_FALSE_POSITIVE_RATE", "ei_ads_model_false_positive_rate"),
    ("MODEL_BRIER_SCORE", "ei_ads_model_brier_score"),
    ("MODEL_DRIFT_DETECTED", "ei_ads_model_drift_detected"),
]


@pytest.mark.parametrize("attr,name", _EXPECTED_METRICS, ids=[a for a, _ in _EXPECTED_METRICS])
def test_metric_object_exists(attr, name):
    """Every design-listed metric is exported from shared.metrics."""
    assert hasattr(metrics, attr), f"missing metric object {attr}"


def test_metric_names_match_design_exposition():
    """When prometheus_client is present, the metric names exposed in the
    Prometheus text format exactly match the design's ``ei_ads_*`` names.

    (We check the exposition output rather than the private ``_name`` attribute
    because prometheus_client strips the ``_total`` suffix from a Counter's
    ``_name`` while still exposing the full ``*_total`` series.)
    """
    if not metrics.prometheus_available():
        pytest.skip("prometheus_client not installed; names not introspectable")
    from prometheus_client import generate_latest

    # Touch each family so it appears in the exposition output.
    metrics.ENGINE_REQUESTS_TOTAL.labels(engine="e", status="ok").inc()
    metrics.ENGINE_LATENCY_SECONDS.labels(engine="e").observe(0.1)
    metrics.ENGINE_INDICATORS_GENERATED_TOTAL.labels(engine="e", severity="HIGH").inc()
    metrics.ENGINE_ERRORS_TOTAL.labels(engine="e", error_type="timeout").inc()
    metrics.ADAPTER_REQUESTS_TOTAL.labels(adapter="a", status="ok").inc()
    metrics.ADAPTER_LATENCY_SECONDS.labels(adapter="a").observe(0.1)
    metrics.ADAPTER_CACHE_HITS_TOTAL.labels(adapter="a").inc()
    metrics.ADAPTER_CACHE_MISSES_TOTAL.labels(adapter="a").inc()
    metrics.ADAPTER_ERROR_RATE.labels(adapter="a").set(0.01)
    metrics.MODEL_FRAUD_DETECTION_RATE.set(0.9)
    metrics.MODEL_FALSE_POSITIVE_RATE.set(0.03)
    metrics.MODEL_BRIER_SCORE.set(0.08)
    metrics.MODEL_DRIFT_DETECTED.set(0)

    exposition = generate_latest().decode()
    exposed_names = {
        line.split(" ", 3)[2]
        for line in exposition.splitlines()
        if line.startswith("# HELP ")
    }
    for _attr, name in _EXPECTED_METRICS:
        assert name in exposed_names, f"metric {name} not present in exposition"


def test_labelled_counter_supports_inc():
    """Labelled counter handle exposes labels(...).inc() without raising."""
    metrics.ENGINE_REQUESTS_TOTAL.labels(engine="repetitive_pattern", status="ok").inc()


def test_histogram_supports_observe():
    metrics.ADAPTER_LATENCY_SECONDS.labels(adapter="cra").observe(0.123)


def test_gauge_supports_set():
    metrics.MODEL_DRIFT_DETECTED.set(1)
    metrics.MODEL_DRIFT_DETECTED.set(0)
    metrics.ADAPTER_ERROR_RATE.labels(adapter="mvr").set(0.02)


# ---------------------------------------------------------------------------
# instrument_app behaviour
# ---------------------------------------------------------------------------


class _FakeState:
    pass


class _FakeApp:
    """Minimal stand-in with a ``.state`` attribute, like a FastAPI app."""

    def __init__(self) -> None:
        self.state = _FakeState()


def _make_app():
    """Return a real FastAPI app when available, else a minimal fake.

    The real instrumentator requires a genuine FastAPI/Starlette app; the fake
    is sufficient only for the no-op degradation path (instrumentator absent).
    """
    if metrics.instrumentation_available():
        fastapi = pytest.importorskip("fastapi")
        return fastapi.FastAPI(title="metrics-test")
    return _FakeApp()


def test_instrument_app_records_kind():
    app = _make_app()
    returned = metrics.instrument_app(app, kind="engine")
    assert returned is app
    assert app.state.metrics_kind == "engine"


@pytest.mark.parametrize("kind", ["engine", "adapter", "service"])
def test_instrument_app_accepts_valid_kinds(kind):
    app = _make_app()
    metrics.instrument_app(app, kind=kind)
    assert app.state.metrics_kind == kind


def test_instrument_app_rejects_invalid_kind():
    app = _make_app()
    with pytest.raises(ValueError):
        metrics.instrument_app(app, kind="not-a-kind")


def test_instrument_app_no_op_when_instrumentator_absent():
    """Without the instrumentator installed, instrument_app must not raise and
    must not add a /metrics route (graceful degradation)."""
    if metrics.instrumentation_available():
        pytest.skip("instrumentator installed; no-op path not exercised")
    app = _FakeApp()
    # Should simply return the app; no exception, no route side effects.
    assert metrics.instrument_app(app, kind="adapter") is app


def test_metrics_endpoint_present_when_instrumentator_available():
    """With both optional deps present, /metrics serves Prometheus text."""
    if not metrics.instrumentation_available():
        pytest.skip("prometheus-fastapi-instrumentator not installed")
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    app = fastapi.FastAPI(title="metrics-test")
    metrics.instrument_app(app, kind="service")

    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    # Prometheus exposition responses are plain text.
    assert "text/plain" in resp.headers.get("content-type", "")
