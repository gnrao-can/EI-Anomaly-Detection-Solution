"""
tests/unit/test_claim_intake_kafka.py
=====================================
Unit tests for the Claim Intake Service Kafka publish and two-phase commit /
transactional rollback (task 4.3).

These tests exercise ``POST /api/v1/claims`` end-to-end through FastAPI's
``TestClient`` using the in-memory ``FakeClaimRepository`` and
``FakeClaimEventProducer`` shipped alongside the service, so no live Kafka broker
or PostgreSQL instance is required.

Scenarios covered:
  1. Successful publish -> HTTP 202, and BOTH the PG (fake repo) and Kafka
     (fake producer) received the record, keyed by ``claim_id``.
  2. Kafka publish failure -> the PG write is rolled back (no claim persisted,
     ``delete_claim`` removed the row) and HTTP 503-class error is returned with
     no ``claim_id`` in the body.
  3. Kafka publish timeout (slow producer exceeding 500 ms) -> same rollback and
     structured error.
  4. PG write failure -> structured error is returned and nothing is published.

Requirements: 1.4, 1.5
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Load the claim-intake service module. Its sibling modules (config, hashing,
# repository, kafka_producer) import each other by bare name, so the service
# directory must be on ``sys.path`` while ``main.py`` is executed.
# ---------------------------------------------------------------------------

_SERVICE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "services", "claim-intake")
)


def _load_claim_intake_main():
    added = False
    if os.path.isdir(_SERVICE_DIR) and _SERVICE_DIR not in sys.path:
        sys.path.insert(0, _SERVICE_DIR)
        added = True
    try:
        spec = importlib.util.spec_from_file_location(
            "claim_intake_main", os.path.join(_SERVICE_DIR, "main.py")
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["claim_intake_main"] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module
    finally:
        sys.modules.pop("main", None)
        sys.modules.pop("config", None)
        if added and _SERVICE_DIR in sys.path:
            sys.path.remove(_SERVICE_DIR)


def _load_module(name: str):
    """Load a sibling service module (``repository`` / ``kafka_producer``)."""
    added = False
    if os.path.isdir(_SERVICE_DIR) and _SERVICE_DIR not in sys.path:
        sys.path.insert(0, _SERVICE_DIR)
        added = True
    try:
        spec = importlib.util.spec_from_file_location(
            f"claim_intake_{name}", os.path.join(_SERVICE_DIR, f"{name}.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module
    finally:
        if added and _SERVICE_DIR in sys.path:
            sys.path.remove(_SERVICE_DIR)


try:
    from fastapi.testclient import TestClient

    claim_intake_main = _load_claim_intake_main()
    repository_mod = _load_module("repository")
    # Reuse the exact ``kafka_producer`` module object that ``main`` imported so
    # ``KafkaPublishError``'s class identity matches the one the endpoint's
    # ``except KafkaPublishError`` catches. Loading a second copy would create a
    # distinct exception class the endpoint would not intercept.
    kafka_mod = sys.modules[claim_intake_main.KafkaPublishError.__module__]
    _HAS_CLAIM_INTAKE = True
except Exception:  # noqa: BLE001
    _HAS_CLAIM_INTAKE = False


# A valid SIN passing the Luhn checksum, plus otherwise-valid claim fields.
_RAW_SIN = "130692544"
_VALID_PAYLOAD = {
    "sin": _RAW_SIN,
    "employer_bn": "123456789RT0001",
    "insurable_hours": 800,
    "insurable_earnings": 25000,
    "separation_date": "2024-01-01",
    "separation_reason": "Shortage of work",
    "postal_code": "K1A 0A9",
}


pytestmark = pytest.mark.skipif(
    not _HAS_CLAIM_INTAKE, reason="claim-intake not importable"
)


# ---------------------------------------------------------------------------
# A repository wrapper that records rollback (delete_claim) invocations so the
# tests can assert the rollback branch actually fired.
# ---------------------------------------------------------------------------


def _make_tracking_repo():
    """Return a FakeClaimRepository subclass that records delete_claim calls."""

    class _TrackingRepo(repository_mod.FakeClaimRepository):
        def __init__(self) -> None:
            super().__init__()
            self.deleted: list = []

        async def delete_claim(self, claim_id):  # type: ignore[override]
            self.deleted.append(claim_id)
            return await super().delete_claim(claim_id)

    return _TrackingRepo()


# ---------------------------------------------------------------------------
# POST /api/v1/claims — two-phase commit through the endpoint
# ---------------------------------------------------------------------------


class TestClaimIntakeKafkaTwoPhaseCommit:
    def setup_method(self):
        from shared.auth import get_current_user

        # Authenticate as a CLAIMANT for all these tests.
        claim_intake_main.app.dependency_overrides[get_current_user] = lambda: {
            "roles": ["CLAIMANT"]
        }
        self.repo = _make_tracking_repo()
        claim_intake_main.app.state.claim_repository = self.repo
        self.producer = kafka_mod.FakeClaimEventProducer()
        claim_intake_main.app.state.event_producer = self.producer
        claim_intake_main.app.state.settings = claim_intake_main.get_settings()

    def teardown_method(self):
        claim_intake_main.app.dependency_overrides.clear()
        claim_intake_main.app.state.event_producer = None

    def _client(self):
        return TestClient(claim_intake_main.app)

    # -- 1. Success: PG + Kafka both receive the record ------------------

    def test_successful_publish_persists_and_publishes(self):
        resp = self._client().post("/api/v1/claims", data=_VALID_PAYLOAD)
        assert resp.status_code == 202
        body = resp.json()
        claim_id = body["claim_id"]
        assert body["status"] == "RECEIVED"

        # PG (fake repo) has exactly one persisted claim with this id.
        assert len(self.repo.claims) == 1
        assert str(self.repo.claims[0].claim_id) == claim_id

        # Kafka (fake producer) got exactly one event keyed by the same id.
        assert len(self.producer.published) == 1
        assert self.producer.published[0]["claim_id"] == claim_id
        assert self.producer.published[0]["event_type"] == "claim.received"

        # No rollback occurred on the happy path.
        assert self.repo.deleted == []

    # -- 2. Kafka publish failure -> rollback the PG write ---------------

    def test_kafka_failure_rolls_back_pg_write(self):
        self.producer.fail_next_publish()
        resp = self._client().post("/api/v1/claims", data=_VALID_PAYLOAD)

        # 503-class error (502 Bad Gateway) with no claim_id on rollback.
        assert resp.status_code >= 500
        assert resp.status_code == 502
        body = resp.json()
        assert "error" in body
        assert body.get("claim_id") is None

        # The just-written PG row was rolled back via delete_claim.
        assert len(self.repo.deleted) == 1
        assert self.repo.claims == []
        assert self.repo.records == []

        # Nothing was published.
        assert self.producer.published == []

    # -- 3. Kafka publish timeout (slow producer) -> same rollback -------

    def test_kafka_timeout_rolls_back_pg_write(self):
        # Producer whose publish takes longer than its (sub-500ms) timeout.
        self.producer = kafka_mod.FakeClaimEventProducer(
            timeout_seconds=0.05, delay_seconds=0.2
        )
        claim_intake_main.app.state.event_producer = self.producer

        resp = self._client().post("/api/v1/claims", data=_VALID_PAYLOAD)

        assert resp.status_code >= 500
        assert resp.status_code == 502
        body = resp.json()
        assert "error" in body
        assert body.get("claim_id") is None

        # PG write rolled back after the timeout; nothing published.
        assert len(self.repo.deleted) == 1
        assert self.repo.claims == []
        assert self.producer.published == []

    # -- 4. PG write failure -> nothing published, no rollback needed ----

    def test_pg_failure_returns_error_and_never_publishes(self):
        self.repo.fail_next_insert()
        resp = self._client().post("/api/v1/claims", data=_VALID_PAYLOAD)

        assert resp.status_code >= 500
        assert resp.status_code == 500
        body = resp.json()
        assert "error" in body
        assert body.get("claim_id") is None

        # Nothing persisted, no rollback attempted, nothing published.
        assert self.repo.claims == []
        assert self.repo.deleted == []
        assert self.producer.published == []
