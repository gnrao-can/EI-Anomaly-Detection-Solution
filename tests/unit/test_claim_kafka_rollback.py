"""
tests/unit/test_claim_kafka_rollback.py
========================================
Unit tests for the Claim Intake Service two-phase commit and transactional
rollback (task 4.3).

Covers:
  - Success path: a valid claim is persisted to PG (fake repo) AND published to
    Kafka (fake producer), keyed by ``claim_id``; the claim remains stored and
    the endpoint returns 202.
  - Kafka publish failure: the PG write is rolled back (claim deleted), a
    structured error body is returned, and the HTTP status is not 202.
  - Kafka publish timeout (> 500 ms): treated like a publish failure — PG write
    rolled back, structured error, non-202 status.
  - PG write failure: a structured error is returned, nothing is published, and
    no claim is persisted.

Both the PostgreSQL repository and the Kafka producer are replaced with the
in-memory fakes shipped alongside the service so no real broker or database is
required.

Requirements: 1.4, 1.5
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Load the claim-intake service module (its siblings import by bare name).
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
    # the ``KafkaPublishError`` class identity matches the one ``main`` catches.
    # (Loading a second copy would create a distinct exception class that the
    # endpoint's ``except KafkaPublishError`` would not intercept.)
    kafka_mod = sys.modules[claim_intake_main.KafkaPublishError.__module__]
    _HAS_CLAIM_INTAKE = True
except Exception:  # noqa: BLE001
    _HAS_CLAIM_INTAKE = False


# A valid SIN passing the Luhn checksum, plus otherwise-valid claim fields.
_RAW_SIN = "130692544"
# The /api/v1/claims endpoint accepts multipart/form-data (design line 145;
# Req 1.2), so claim fields are posted as form values (all strings on the wire).
_VALID_FORM = {
    "sin": _RAW_SIN,
    "employer_bn": "123456789RT0001",
    "insurable_hours": "800",
    "insurable_earnings": "25000",
    "separation_date": "2024-01-01",
    "separation_reason": "Shortage of work",
    "postal_code": "K1A 0A9",
}


pytestmark = pytest.mark.skipif(
    not _HAS_CLAIM_INTAKE, reason="claim-intake not importable"
)


# ---------------------------------------------------------------------------
# FakeClaimEventProducer / build_claim_received_event — producer-level tests
# ---------------------------------------------------------------------------


class TestClaimEventProducerEnvelope:
    def _make_claim(self):
        from datetime import date, datetime, timezone
        from decimal import Decimal
        import uuid as _uuid

        from shared.models import Claim, ClaimStatus

        return Claim(
            claim_id=_uuid.uuid4(),
            sin_hash="a" * 64,
            employer_bn="123456789RT0001",
            insurable_hours=Decimal("800"),
            insurable_earnings=Decimal("25000"),
            separation_date=date(2024, 1, 1),
            separation_reason="Shortage of work",
            separation_reason_code="A",
            claim_start_date=date(2024, 1, 1),
            economic_region_code="ON",
            noc_code="12345",
            residential_postal_code="K1A",
            residential_address_hash="",
            employer_work_location="",
            received_at=datetime.now(timezone.utc),
            status=ClaimStatus.RECEIVED,
        )

    def test_envelope_keyed_and_named_correctly(self):
        claim = self._make_claim()
        event = kafka_mod.build_claim_received_event(claim)
        assert event["event_type"] == "claim.received"
        assert event["claim_id"] == str(claim.claim_id)
        assert "timestamp" in event
        assert event["payload"]["claim_id"] == str(claim.claim_id)
        assert event["payload"]["status"] == "RECEIVED"

    def test_envelope_contains_sin_hash_not_raw_sin(self):
        claim = self._make_claim()
        event = kafka_mod.build_claim_received_event(claim)
        assert event["payload"]["sin_hash"] == claim.sin_hash
        assert _RAW_SIN not in str(event)

    async def test_fake_producer_records_published_event(self):
        producer = kafka_mod.FakeClaimEventProducer()
        claim = self._make_claim()
        await producer.publish_claim_received(claim)
        assert len(producer.published) == 1
        assert producer.published[0]["claim_id"] == str(claim.claim_id)

    async def test_fake_producer_failure_raises(self):
        producer = kafka_mod.FakeClaimEventProducer()
        producer.fail_next_publish()
        with pytest.raises(kafka_mod.KafkaPublishError):
            await producer.publish_claim_received(self._make_claim())

    async def test_fake_producer_timeout_raises(self):
        # Delay longer than the timeout to force a KafkaPublishError.
        producer = kafka_mod.FakeClaimEventProducer(
            timeout_seconds=0.05, delay_seconds=0.2
        )
        with pytest.raises(kafka_mod.KafkaPublishError):
            await producer.publish_claim_received(self._make_claim())


# ---------------------------------------------------------------------------
# FakeClaimRepository.delete_claim — rollback primitive
# ---------------------------------------------------------------------------


class TestFakeRepositoryDelete:
    async def test_delete_removes_claim_and_record(self):
        from datetime import date, datetime, timezone
        from decimal import Decimal
        import uuid as _uuid

        from shared.models import Claim, ClaimStatus

        repo = repository_mod.FakeClaimRepository()
        claim = Claim(
            claim_id=_uuid.uuid4(),
            sin_hash="a" * 64,
            employer_bn="123456789RT0001",
            insurable_hours=Decimal("800"),
            insurable_earnings=Decimal("25000"),
            separation_date=date(2024, 1, 1),
            separation_reason="Shortage of work",
            separation_reason_code="A",
            claim_start_date=date(2024, 1, 1),
            economic_region_code="ON",
            noc_code="12345",
            residential_postal_code="K1A",
            residential_address_hash="",
            employer_work_location="",
            received_at=datetime.now(timezone.utc),
            status=ClaimStatus.RECEIVED,
        )
        claim_id = await repo.insert_claim(claim)
        assert len(repo.claims) == 1

        removed = await repo.delete_claim(claim_id)
        assert removed is True
        assert repo.claims == []
        assert repo.records == []
        assert repo.get(claim_id) is None

    async def test_delete_missing_returns_false(self):
        import uuid as _uuid

        repo = repository_mod.FakeClaimRepository()
        assert await repo.delete_claim(_uuid.uuid4()) is False


# ---------------------------------------------------------------------------
# POST /api/v1/claims — end-to-end two-phase commit through the endpoint
# ---------------------------------------------------------------------------


class TestTwoPhaseCommit:
    def setup_method(self):
        from shared.auth import get_current_user

        claim_intake_main.app.dependency_overrides[get_current_user] = lambda: {
            "roles": ["CLAIMANT"]
        }
        self.repo = repository_mod.FakeClaimRepository()
        claim_intake_main.app.state.claim_repository = self.repo
        self.producer = kafka_mod.FakeClaimEventProducer()
        claim_intake_main.app.state.event_producer = self.producer
        claim_intake_main.app.state.settings = claim_intake_main.get_settings()

    def teardown_method(self):
        claim_intake_main.app.dependency_overrides.clear()
        claim_intake_main.app.state.event_producer = None

    def _client(self):
        return TestClient(claim_intake_main.app)

    def test_success_persists_and_publishes(self):
        resp = self._client().post("/api/v1/claims", data=_VALID_FORM)
        assert resp.status_code == 202
        body = resp.json()
        claim_id = body["claim_id"]

        # Persisted exactly once and still present (no rollback).
        assert len(self.repo.claims) == 1
        assert str(self.repo.claims[0].claim_id) == claim_id

        # Published exactly once, keyed by the same claim_id.
        assert len(self.producer.published) == 1
        assert self.producer.published[0]["claim_id"] == claim_id

    def test_publish_failure_triggers_rollback(self):
        self.producer.fail_next_publish()
        resp = self._client().post("/api/v1/claims", data=_VALID_FORM)

        # Not accepted; structured error body; PG row rolled back.
        assert resp.status_code != 202
        assert resp.status_code == 502
        body = resp.json()
        assert "error" in body
        assert "claim_id" not in body
        assert self.repo.claims == []
        assert self.producer.published == []

    def test_publish_timeout_triggers_rollback(self):
        # Force the publish to exceed the producer's timeout.
        self.producer = kafka_mod.FakeClaimEventProducer(
            timeout_seconds=0.05, delay_seconds=0.2
        )
        claim_intake_main.app.state.event_producer = self.producer

        resp = self._client().post("/api/v1/claims", data=_VALID_FORM)
        assert resp.status_code != 202
        assert resp.status_code == 502
        body = resp.json()
        assert "error" in body
        # PG write rolled back after the timeout.
        assert self.repo.claims == []
        assert self.producer.published == []

    def test_pg_failure_returns_error_and_never_publishes(self):
        self.repo.fail_next_insert()
        resp = self._client().post("/api/v1/claims", data=_VALID_FORM)

        assert resp.status_code != 202
        assert resp.status_code == 500
        body = resp.json()
        assert "error" in body
        assert "claim_id" not in body
        # Nothing persisted and nothing published.
        assert self.repo.claims == []
        assert self.producer.published == []
