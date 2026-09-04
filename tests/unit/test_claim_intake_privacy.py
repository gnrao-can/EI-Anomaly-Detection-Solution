"""
tests/unit/test_claim_intake_privacy.py
=======================================
Unit tests for the Claim Intake Service Privacy Act compliance endpoints
(task 28.1):

  - ``GET  /api/v1/privacy/access``  — Requirement 19.4
  - ``DELETE /api/v1/privacy/delete`` — Requirement 19.5

Covers:
  - ``FakePrivacyRepository`` access returns every claim / indicator /
    risk_assessment (PostgreSQL) + ClickHouse analytics row for a ``sin_hash``,
    and only for that ``sin_hash`` (no cross-claimant leakage).
  - Anonymisation tombstones the identifying fields of claims / indicators /
    risk_assessments while preserving the aggregate/statistical columns and the
    rows themselves (nothing deleted).
  - HTTP wiring: 401 unauthenticated, 403 for a claimant targeting another
    claimant's data (own-data-only), 403 for a role without CLAIMANT/ADMIN,
    200 + machine-readable JSON for a claimant accessing/erasing their own data,
    and ADMINISTRATOR access to an explicitly-named target.

The claim-intake service directory is hyphenated, so its modules are loaded via
the same isolation pattern used by ``test_claim_intake.py`` / ``test_auth.py``.

Requirements: 19.4, 19.5, 1.11, 19.1
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Load the hyphenated claim-intake service modules in isolation.
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
        sys.modules.pop("privacy", None)
        if added and _SERVICE_DIR in sys.path:
            sys.path.remove(_SERVICE_DIR)


def _load_module(name: str):
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
    privacy_mod = _load_module("privacy")
    hashing_mod = _load_module("hashing")
    _HAS_CLAIM_INTAKE = True
except Exception:  # noqa: BLE001
    _HAS_CLAIM_INTAKE = False


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_SIN_HASH_A = "a" * 64
_SIN_HASH_B = "b" * 64


def _seed_repo(privacy_mod):
    """Build a FakePrivacyRepository with two claimants' worth of data."""
    repo = privacy_mod.FakePrivacyRepository()

    # Claimant A: one claim with an indicator + risk assessment + analytics row.
    repo.add_claim(
        {
            "claim_id": "claim-a1",
            "sin_hash": _SIN_HASH_A,
            "employer_bn": "123456789RT0001",
            "insurable_hours": "800.00",
            "insurable_earnings": "25000.00",
            "economic_region_code": "ON",
            "noc_code": "12345",
            "residential_postal_code": "K1A",
            "residential_address_hash": "deadbeef",
            "employer_work_location": "123 Main St",
            "separation_reason": "Shortage of work",
            "status": "SCORED",
        }
    )
    repo.add_indicator(
        {
            "indicator_id": "ind-a1",
            "claim_id": "claim-a1",
            "engine": "REPETITIVE_PATTERN",
            "severity": "HIGH",
            "title": "Repetitive pattern detected",
            "explanation_en": "Explanation EN",
            "explanation_fr": "Explication FR",
            "payload": {"amount": 25000, "name": "Jane Doe"},
        }
    )
    repo.add_risk_assessment(
        {
            "assessment_id": "ra-a1",
            "claim_id": "claim-a1",
            "risk_score": 72,
            "risk_decision": "INVESTIGATE",
            "explanation_en": "Risk EN",
            "explanation_fr": "Risque FR",
        }
    )
    repo.clickhouse["claims_analytics"].append(
        {"claim_id": "claim-a1", "economic_region": "ON", "insurable_hours": "800.00"}
    )

    # Claimant B: a separate claim that must never surface for A.
    repo.add_claim(
        {
            "claim_id": "claim-b1",
            "sin_hash": _SIN_HASH_B,
            "employer_bn": "999999999RT0001",
            "insurable_hours": "500.00",
            "insurable_earnings": "10000.00",
            "economic_region_code": "BC",
            "noc_code": "54321",
            "residential_postal_code": "V6B",
            "residential_address_hash": "cafebabe",
            "employer_work_location": "456 Elm St",
            "separation_reason": "Quit",
            "status": "RECEIVED",
        }
    )
    return repo


# ---------------------------------------------------------------------------
# FakePrivacyRepository — access
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_CLAIM_INTAKE, reason="claim-intake not importable")
class TestPrivacyRepositoryAccess:
    async def test_access_returns_own_records_only(self):
        repo = _seed_repo(privacy_mod)
        report = await repo.access_records(_SIN_HASH_A)

        assert report["sin_hash"] == _SIN_HASH_A
        assert len(report["postgres"]["claims"]) == 1
        assert report["postgres"]["claims"][0]["claim_id"] == "claim-a1"
        assert len(report["postgres"]["indicators"]) == 1
        assert len(report["postgres"]["risk_assessments"]) == 1
        assert len(report["clickhouse"]["claims_analytics"]) == 1
        # record_count sums PG + ClickHouse rows: 1+1+1+1 = 4.
        assert report["record_count"] == 4

        # No claimant-B data leaks into claimant-A's report.
        serialised = str(report)
        assert "claim-b1" not in serialised
        assert _SIN_HASH_B not in serialised

    async def test_access_unknown_sin_hash_is_empty(self):
        repo = _seed_repo(privacy_mod)
        report = await repo.access_records("f" * 64)
        assert report["record_count"] == 0
        assert report["postgres"]["claims"] == []

    async def test_access_report_is_machine_readable_json(self):
        import json

        repo = _seed_repo(privacy_mod)
        report = await repo.access_records(_SIN_HASH_A)
        # Must round-trip through JSON without error.
        encoded = json.dumps(report)
        assert json.loads(encoded)["sin_hash"] == _SIN_HASH_A


# ---------------------------------------------------------------------------
# FakePrivacyRepository — anonymisation
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_CLAIM_INTAKE, reason="claim-intake not importable")
class TestPrivacyRepositoryAnonymise:
    async def test_anonymises_identifying_fields(self):
        repo = _seed_repo(privacy_mod)
        receipt = await repo.anonymise_records(_SIN_HASH_A)

        assert receipt["sin_hash"] == _SIN_HASH_A
        assert receipt["anonymised"] == {
            "claims": 1,
            "indicators": 1,
            "risk_assessments": 1,
        }
        assert receipt["preserved_aggregates"] is True

        claim = repo.claims[0]
        token = privacy_mod.ANONYMISED_TOKEN
        # Identifying fields tombstoned.
        assert claim["sin_hash"] == token
        assert claim["employer_bn"] == token
        assert claim["residential_postal_code"] == token
        assert claim["residential_address_hash"] == token
        assert claim["employer_work_location"] == token
        assert claim["separation_reason"] == token

        ind = repo.indicators[0]
        assert ind["title"] == token
        assert ind["explanation_en"] == token
        assert ind["explanation_fr"] == token
        assert ind["payload"] == {}

        ra = repo.risk_assessments[0]
        assert ra["explanation_en"] == token
        assert ra["explanation_fr"] == token

    async def test_preserves_aggregate_statistical_fields(self):
        repo = _seed_repo(privacy_mod)
        await repo.anonymise_records(_SIN_HASH_A)

        claim = repo.claims[0]
        # Numeric / coded / status columns retained for statistics.
        assert claim["insurable_hours"] == "800.00"
        assert claim["insurable_earnings"] == "25000.00"
        assert claim["economic_region_code"] == "ON"
        assert claim["noc_code"] == "12345"
        assert claim["status"] == "SCORED"

        ra = repo.risk_assessments[0]
        assert ra["risk_score"] == 72
        assert ra["risk_decision"] == "INVESTIGATE"

        ind = repo.indicators[0]
        assert ind["engine"] == "REPETITIVE_PATTERN"
        assert ind["severity"] == "HIGH"

    async def test_rows_are_not_deleted(self):
        repo = _seed_repo(privacy_mod)
        await repo.anonymise_records(_SIN_HASH_A)
        # Nothing removed — rows preserved for the 7-year audit retention.
        assert len(repo.claims) == 2
        assert len(repo.indicators) == 1
        assert len(repo.risk_assessments) == 1

    async def test_does_not_touch_other_claimant(self):
        repo = _seed_repo(privacy_mod)
        await repo.anonymise_records(_SIN_HASH_A)
        other = next(c for c in repo.claims if c["claim_id"] == "claim-b1")
        assert other["sin_hash"] == _SIN_HASH_B
        assert other["employer_bn"] == "999999999RT0001"

    async def test_anonymise_is_idempotent(self):
        repo = _seed_repo(privacy_mod)
        await repo.anonymise_records(_SIN_HASH_A)
        # Second run finds no rows still keyed by the original sin_hash.
        receipt = await repo.anonymise_records(_SIN_HASH_A)
        assert receipt["anonymised"]["claims"] == 0


# ---------------------------------------------------------------------------
# HTTP wiring — role enforcement + own-data-only
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_CLAIM_INTAKE, reason="claim-intake not importable")
class TestPrivacyEndpointsHttp:
    def setup_method(self):
        self.repo = _seed_repo(privacy_mod)
        claim_intake_main.app.state.privacy_repository = self.repo
        claim_intake_main.app.state.settings = claim_intake_main.get_settings()

    def teardown_method(self):
        claim_intake_main.app.dependency_overrides.clear()

    def _client(self):
        return TestClient(claim_intake_main.app)

    def _auth_as(self, claims: dict):
        from shared.auth import get_current_user

        claim_intake_main.app.dependency_overrides[get_current_user] = lambda: claims

    def test_access_without_token_returns_401(self):
        resp = self._client().get("/api/v1/privacy/access")
        assert resp.status_code == 401

    def test_delete_without_token_returns_401(self):
        resp = self._client().delete("/api/v1/privacy/delete")
        assert resp.status_code == 401

    def test_reviewer_role_denied_403(self):
        self._auth_as({"roles": ["REVIEWER"], "sin_hash": _SIN_HASH_A})
        resp = self._client().get("/api/v1/privacy/access")
        assert resp.status_code == 403

    def test_claimant_accesses_own_data_200(self):
        self._auth_as({"roles": ["CLAIMANT"], "sin_hash": _SIN_HASH_A})
        resp = self._client().get("/api/v1/privacy/access")
        assert resp.status_code == 200
        body = resp.json()
        assert body["sin_hash"] == _SIN_HASH_A
        assert body["record_count"] == 4

    def test_claimant_cannot_access_other_claimant_403(self):
        self._auth_as({"roles": ["CLAIMANT"], "sin_hash": _SIN_HASH_A})
        resp = self._client().get(
            "/api/v1/privacy/access", params={"sin_hash": _SIN_HASH_B}
        )
        assert resp.status_code == 403

    def test_claimant_identity_via_raw_sin_claim(self):
        # Token carries a raw SIN claim; the endpoint hashes it with the salt.
        raw_sin = "130692544"
        salt = claim_intake_main.app.state.settings.sin_hash_salt
        derived = hashing_mod.hash_sin(raw_sin, salt)
        self.repo.claims[0]["sin_hash"] = derived
        self._auth_as({"roles": ["CLAIMANT"], "sin": raw_sin})
        resp = self._client().get("/api/v1/privacy/access")
        assert resp.status_code == 200
        assert resp.json()["sin_hash"] == derived

    def test_claimant_without_identity_returns_400(self):
        self._auth_as({"roles": ["CLAIMANT"]})
        resp = self._client().get("/api/v1/privacy/access")
        assert resp.status_code == 400

    def test_administrator_accesses_named_target_200(self):
        self._auth_as({"roles": ["ADMINISTRATOR"]})
        resp = self._client().get(
            "/api/v1/privacy/access", params={"sin_hash": _SIN_HASH_B}
        )
        assert resp.status_code == 200
        assert resp.json()["sin_hash"] == _SIN_HASH_B

    def test_administrator_without_target_returns_400(self):
        self._auth_as({"roles": ["ADMINISTRATOR"]})
        resp = self._client().get("/api/v1/privacy/access")
        assert resp.status_code == 400

    def test_claimant_deletes_own_data_200(self):
        self._auth_as({"roles": ["CLAIMANT"], "sin_hash": _SIN_HASH_A})
        resp = self._client().delete("/api/v1/privacy/delete")
        assert resp.status_code == 200
        body = resp.json()
        assert body["sin_hash"] == _SIN_HASH_A
        assert body["anonymised"]["claims"] == 1
        assert body["preserved_aggregates"] is True
        # The claim was anonymised in the shared repo.
        assert self.repo.claims[0]["sin_hash"] == privacy_mod.ANONYMISED_TOKEN

    def test_claimant_cannot_delete_other_claimant_403(self):
        self._auth_as({"roles": ["CLAIMANT"], "sin_hash": _SIN_HASH_A})
        resp = self._client().delete(
            "/api/v1/privacy/delete", params={"sin_hash": _SIN_HASH_B}
        )
        assert resp.status_code == 403
        # Claimant B untouched.
        other = next(c for c in self.repo.claims if c["claim_id"] == "claim-b1")
        assert other["sin_hash"] == _SIN_HASH_B
