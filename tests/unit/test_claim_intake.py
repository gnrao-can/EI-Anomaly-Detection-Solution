"""
tests/unit/test_claim_intake.py
===============================
Unit tests for the Claim Intake Service validation, SIN hashing, and
persistence logic (task 4.2).

Covers:
  - ``hash_sin`` determinism and salt-dependence, and that the raw SIN is never
    embedded in the digest output.
  - ``POST /api/v1/claims`` validation failures return HTTP 400 with a
    structured ``{"errors": [...]}`` body and persist nothing.
  - A valid claim is hashed, persisted with status RECEIVED via the fake
    repository, and returns 202 with a real ``claim_id`` — and the raw SIN never
    appears in the stored record.
  - The stored ``residential_postal_code`` is reduced to the FSA (first 3 chars).

Requirements: 1.2, 1.3, 1.4, 1.11, 19.1
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
        # Register under its own name before exec so pydantic can resolve the
        # deferred (``from __future__ import annotations``) type references on
        # the request/response models at request time.
        sys.modules["claim_intake_main"] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module
    finally:
        sys.modules.pop("main", None)
        sys.modules.pop("config", None)
        if added and _SERVICE_DIR in sys.path:
            sys.path.remove(_SERVICE_DIR)


def _load_module(name: str):
    """Load a sibling service module (``hashing`` / ``repository``) by name."""
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
    hashing_mod = _load_module("hashing")
    repository_mod = _load_module("repository")
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


# ---------------------------------------------------------------------------
# hash_sin — determinism and salt-dependence
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_CLAIM_INTAKE, reason="claim-intake not importable")
class TestHashSin:
    def test_deterministic(self):
        h1 = hashing_mod.hash_sin(_RAW_SIN, "salt-A")
        h2 = hashing_mod.hash_sin(_RAW_SIN, "salt-A")
        assert h1 == h2

    def test_salt_dependent(self):
        h_a = hashing_mod.hash_sin(_RAW_SIN, "salt-A")
        h_b = hashing_mod.hash_sin(_RAW_SIN, "salt-B")
        assert h_a != h_b

    def test_returns_sha256_hex_digest(self):
        digest = hashing_mod.hash_sin(_RAW_SIN, "salt-A")
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_raw_sin_not_in_digest(self):
        digest = hashing_mod.hash_sin(_RAW_SIN, "salt-A")
        assert _RAW_SIN not in digest

    def test_separators_normalized(self):
        # "123-456-789" and "123456789" must hash identically.
        assert hashing_mod.hash_sin("130-692-544", "s") == hashing_mod.hash_sin(
            "130692544", "s"
        )

    def test_empty_salt_rejected(self):
        with pytest.raises(ValueError):
            hashing_mod.hash_sin(_RAW_SIN, "")


# ---------------------------------------------------------------------------
# FakeClaimRepository
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_CLAIM_INTAKE, reason="claim-intake not importable")
class TestFakeClaimRepository:
    async def test_insert_records_status_received(self):
        from shared.models import Claim, ClaimStatus
        from datetime import date, datetime, timezone
        from decimal import Decimal
        import uuid as _uuid

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
            # Deliberately not RECEIVED to prove the repo forces RECEIVED.
            status=ClaimStatus.PROCESSING,
        )
        claim_id = await repo.insert_claim(claim)
        assert claim_id == claim.claim_id
        assert len(repo.claims) == 1
        assert repo.claims[0].status == ClaimStatus.RECEIVED


# ---------------------------------------------------------------------------
# POST /api/v1/claims — validation, hashing, persistence
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_CLAIM_INTAKE, reason="claim-intake not importable")
class TestCreateClaim:
    def setup_method(self):
        from shared.auth import get_current_user

        # Authenticate as a CLAIMANT for all these tests.
        claim_intake_main.app.dependency_overrides[get_current_user] = lambda: {
            "roles": ["CLAIMANT"]
        }
        # Ensure a fresh in-memory repository per test.
        self.repo = repository_mod.FakeClaimRepository()
        claim_intake_main.app.state.claim_repository = self.repo
        # Ensure settings has a usable salt (default is fine for tests).
        claim_intake_main.app.state.settings = claim_intake_main.get_settings()

    def teardown_method(self):
        claim_intake_main.app.dependency_overrides.clear()

    def _client(self):
        return TestClient(claim_intake_main.app)

    def test_valid_claim_returns_202_with_claim_id(self):
        resp = self._client().post("/api/v1/claims", data=_VALID_PAYLOAD)
        assert resp.status_code == 202
        body = resp.json()
        assert "claim_id" in body
        assert body["status"] == "RECEIVED"
        # Persisted exactly once.
        assert len(self.repo.claims) == 1

    def test_valid_claim_persisted_with_received_status(self):
        self._client().post("/api/v1/claims", data=_VALID_PAYLOAD)
        stored = self.repo.claims[0]
        from shared.models import ClaimStatus

        assert stored.status == ClaimStatus.RECEIVED

    def test_sin_is_hashed_and_raw_sin_never_stored(self):
        resp = self._client().post("/api/v1/claims", data=_VALID_PAYLOAD)
        assert resp.status_code == 202
        stored = self.repo.claims[0]

        # The stored sin_hash is a 64-char SHA-256 digest, not the raw SIN.
        assert stored.sin_hash != _RAW_SIN
        assert len(stored.sin_hash) == 64

        # The raw SIN must not appear anywhere in the stored record.
        serialized = str(self.repo.records[0])
        assert _RAW_SIN not in serialized
        assert _RAW_SIN not in str(stored.to_dict())

        # And it must match what hash_sin would produce with the env salt.
        expected = hashing_mod.hash_sin(
            _RAW_SIN, claim_intake_main.app.state.settings.sin_hash_salt
        )
        assert stored.sin_hash == expected

    def test_postal_code_reduced_to_fsa(self):
        self._client().post("/api/v1/claims", data=_VALID_PAYLOAD)
        stored = self.repo.claims[0]
        assert stored.residential_postal_code == "K1A"

    def test_invalid_sin_returns_400_structured_errors(self):
        payload = dict(_VALID_PAYLOAD, sin="000000000")
        resp = self._client().post("/api/v1/claims", data=payload)
        assert resp.status_code == 400
        body = resp.json()
        assert "errors" in body
        assert any(e["field"] == "sin" for e in body["errors"])
        # Nothing persisted on validation failure.
        assert len(self.repo.claims) == 0

    def test_missing_earnings_returns_400(self):
        payload = dict(_VALID_PAYLOAD)
        payload["insurable_earnings"] = 0  # invalid: must be > 0
        resp = self._client().post("/api/v1/claims", data=payload)
        assert resp.status_code == 400
        body = resp.json()
        assert any(e["field"] == "insurable_earnings" for e in body["errors"])
        assert len(self.repo.claims) == 0

    def test_future_separation_date_returns_400(self):
        payload = dict(_VALID_PAYLOAD, separation_date="2999-01-01")
        resp = self._client().post("/api/v1/claims", data=payload)
        assert resp.status_code == 400
        body = resp.json()
        assert any(e["field"] == "separation_date" for e in body["errors"])
        assert len(self.repo.claims) == 0

    def test_invalid_bn_returns_400(self):
        payload = dict(_VALID_PAYLOAD, employer_bn="not-a-bn")
        resp = self._client().post("/api/v1/claims", data=payload)
        assert resp.status_code == 400
        body = resp.json()
        assert any(e["field"] == "employer_bn" for e in body["errors"])
        assert len(self.repo.claims) == 0
