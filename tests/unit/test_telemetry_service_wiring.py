"""
tests/unit/test_telemetry_service_wiring.py
===========================================
Service-touching tests for the OpenTelemetry tracing wiring (task 32.1 / Req
21.5).

Each EI-ADS service's ``main.py`` gains a single, self-contained block near app
creation that calls ``shared.telemetry.instrument_tracing(app, service_name=...)``.
These tests confirm that:

  * the shared ``instrument_tracing`` hook imports and is callable, and returns
    a bool (``False`` in the sandbox where the OpenTelemetry SDK is absent);
  * a representative service module loads WITHOUT raising even though the OTel
    packages are not installed (guarded imports keep the service importable),
    and exposes a FastAPI ``app`` with tracing wired in.

The service directory is hyphenated, so we use the shared
``load_service_module`` helper from ``tests/conftest.py``.

Requirements: 21.5
"""

from __future__ import annotations

import pytest

from shared import telemetry
from tests.conftest import load_service_module


def test_instrument_tracing_is_callable_and_returns_bool():
    """The shared hook is importable, callable, and returns a bool.

    In the sandbox the OpenTelemetry SDK is absent, so tracing degrades to a
    no-op and the call returns ``False`` — without raising.
    """
    result = telemetry.instrument_tracing(None, service_name="unit-test-service")
    assert isinstance(result, bool)


def test_instrument_tracing_tolerates_none_app():
    """The LLM service builds ``app = None`` in a bare sandbox; the hook copes."""
    assert telemetry.instrument_tracing(app=None, service_name="llm-service") is False


def test_cbsa_adapter_main_imports_with_tracing_wired():
    """The CBSA adapter main loads with the tracing block; app is a FastAPI app.

    This exercises the single contiguous ``instrument_tracing(app, ...)`` block
    added to the service near app creation. The service must import even though
    the OpenTelemetry packages are not installed (guarded imports).
    """
    fastapi = pytest.importorskip("fastapi")

    main = load_service_module(
        "cbsa-adapter",
        "main",
        siblings=["absence", "client", "config", "main"],
    )
    assert isinstance(main.app, fastapi.FastAPI)
