"""
tests/unit/test_roe_adapter_healthz.py
======================================
Unit tests for the ROE Adapter ``GET /healthz`` endpoint (task 31.2, Req 21.4).

The ROE Adapter's health depends on its aiokafka producer. These tests exercise
the 200/503 contract:

  * Kafka unreachable + graceful degradation enabled  -> 200 (best-effort dev).
  * Kafka unreachable + graceful degradation disabled -> 503 (dependency down).
  * Kafka connected                                    -> 200.

Kafka is mocked throughout (no broker required). The service module is loaded
via the shared, collision-free ``load_service_module`` helper.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import load_service_module

# Load the ROE Adapter's main module (and its bare-name siblings) without
# polluting sys.modules for other service tests.
main_mod = load_service_module(
    "roe-adapter",
    "main",
    siblings=["config", "normalizer", "main"],
)


@pytest.fixture(autouse=True)
def _reset_producer():
    """Ensure a clean module-level producer handle around every test."""
    main_mod._producer = None
    yield
    main_mod._producer = None


def _client() -> TestClient:
    return TestClient(main_mod.app)


class _FakeSender:
    def __init__(self, closed: bool) -> None:
        self._closed = closed


class _FakeProducer:
    """Minimal stand-in exposing the ``_sender`` surface healthz inspects."""

    def __init__(self, *, closed: bool) -> None:
        self._sender = _FakeSender(closed)


def test_healthz_degrades_gracefully_when_kafka_absent(monkeypatch):
    # No producer created yet (dev): graceful degradation -> 200.
    settings = main_mod.get_settings()
    monkeypatch.setattr(settings, "kafka_degrade_gracefully", True, raising=False)

    resp = _client().get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["kafka"] is False
    assert body["checks"]["degrade_gracefully"] is True


def test_healthz_503_when_kafka_required_but_down(monkeypatch):
    # Kafka required (no graceful degradation) and unreachable -> 503.
    settings = main_mod.get_settings()
    monkeypatch.setattr(settings, "kafka_degrade_gracefully", False, raising=False)
    main_mod._producer = None

    resp = _client().get("/healthz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["kafka"] is False


def test_healthz_ok_when_kafka_connected(monkeypatch):
    # A live producer (open sender) -> Kafka connected -> 200.
    settings = main_mod.get_settings()
    monkeypatch.setattr(settings, "kafka_degrade_gracefully", False, raising=False)
    main_mod._producer = _FakeProducer(closed=False)

    resp = _client().get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["kafka"] is True
