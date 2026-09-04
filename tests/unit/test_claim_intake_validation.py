"""
tests/unit/test_claim_intake_validation.py
===========================================
Comprehensive unit tests for Claim Intake Service validation, persistence, and
transactional rollback (task 4.4).

This file complements — and deliberately does not duplicate — the coverage in
``test_claim_intake.py`` (task 4.2, hashing/basic validation/persistence) and
``test_claim_kafka_rollback.py`` (task 4.3, Kafka rollback). Its focus is:

  1. Exhaustive mandatory-field and boundary validation through the
     ``POST /api/v1/claims`` endpoint (TestClient, CLAIMANT role override):
       - every mandatory field missing -> 400 naming that field, nothing persisted
       - SIN Luhn / leading-zero / length rules
       - BN 9-digit and 15-char format rules
       - insurable_hours: > 0 and <= 3120 boundary rules
       - insurable_earnings: > 0 rule
       - separation_date: <= today and valid ISO format
       - postal_code: invalid first letter and format rules
  2. Persistence assertions on the success path: persisted exactly once with
     status RECEIVED, returned claim_id matches the stored record, raw SIN never
     stored, postal code reduced to the FSA.
  3. Transactional rollback (validation-layer invariant only): when the
     ``claim.received`` publish fails, the endpoint must not report success and
     must leave no persisted claim (and hand back no claim_id). The exhaustive
     two-phase-commit scenarios (success / publish failure / timeout / PG
     failure) are owned by ``test_claim_kafka_rollback.py`` (task 4.3) and are
     intentionally not duplicated here; this file injects the real
     ``FakeClaimEventProducer`` on ``app.state.event_producer`` to drive the
     endpoint's actual rollback path.

Requirements: 1.2, 1.3, 1.4, 1.5
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import sys
from datetime import date, timedelta

import pytest

# ---------------------------------------------------------------------------
# Load the claim-intake service module (its siblings import by bare name).
# Mirrors the loader in test_claim_intake.py but under distinct names to avoid
# collisions when both files run in the same session.
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


def _load_sibling(name: str):
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

    ci_main = _load_claim_intake_main()
    ci_hashing = _load_sibling("hashing")
    ci_repository = _load_sibling("repository")
    # Reuse the exact ``kafka_producer`` module object that ``main`` imported so
    # the ``KafkaPublishError`` / ``FakeClaimEventProducer`` class identities
    # match the ones the endpoint constructs and catches.
    ci_kafka = sys.modules[ci_main.KafkaPublishError.__module__]
    _HAS_CLAIM_INTAKE = True
except Exception:  # noqa: BLE001
    _HAS_CLAIM_INTAKE = False


# A valid SIN passing the Luhn checksum (differs from the one in
# test_claim_intake.py only in that it is re-declared locally here).
_VALID_SIN = "046454286"  # Luhn-valid, does not start with 0's forbidden position
# NB: SINs cannot start with 0; use a Luhn-valid 9-digit not starting with 0.
_VALID_SIN = "130692544"

_TODAY = date.today()
_YESTERDAY = _TODAY - timedelta(days=1)
_TOMORROW = _TODAY + timedelta(days=1)

# A complete, valid payload. Individual tests copy and mutate one field.
_BASE_PAYLOAD = {
    "sin": _VALID_SIN,
    "employer_bn": "123456789RT0001",
    "insurable_hours": 800,
    "insurable_earnings": 25000,
    "separation_date": _YESTERDAY.isoformat(),
    "separation_reason": "Shortage of work",
    "postal_code": "K1A 0A9",
}

# The seven mandatory fields the endpoint validates.
_MANDATORY_FIELDS = [
    "sin",
    "employer_bn",
    "insurable_hours",
    "insurable_earnings",
    "separation_date",
    "separation_reason",
]  # postal_code is OPTIONAL per Req 1.2 (validated only when provided)


def _payload(**overrides):
    """Return a copy of the base payload with *overrides* applied."""
    data = dict(_BASE_PAYLOAD)
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Base fixture wiring: CLAIMANT auth override + fresh in-memory repository.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_CLAIM_INTAKE, reason="claim-intake not importable")
class _IntakeTestBase:
    def setup_method(self):
        from shared.auth import get_current_user

        ci_main.app.dependency_overrides[get_current_user] = lambda: {
            "roles": ["CLAIMANT"]
        }
        self.repo = ci_repository.FakeClaimRepository()
        ci_main.app.state.claim_repository = self.repo
        ci_main.app.state.settings = ci_main.get_settings()

    def teardown_method(self):
        ci_main.app.dependency_overrides.clear()
        # Remove any producer a rollback test may have injected on app.state.
        if hasattr(ci_main.app.state, "kafka_producer"):
            ci_main.app.state.kafka_producer = None

    def _client(self):
        return TestClient(ci_main.app)

    def _fields_in(self, body):
        assert "errors" in body, f"expected structured errors, got {body}"
        return {e["field"] for e in body["errors"]}


# ---------------------------------------------------------------------------
# Mandatory field presence
# ---------------------------------------------------------------------------


class TestMandatoryFieldPresence(_IntakeTestBase):
    @pytest.mark.parametrize("field", _MANDATORY_FIELDS)
    def test_omitted_mandatory_field_is_rejected_and_nothing_persisted(self, field):
        # A truly-absent mandatory field is rejected by the request-schema layer
        # (pydantic -> HTTP 422) before the endpoint's own validate_claim_fields
        # runs. Either way the claim MUST be rejected (4xx) and nothing persisted.
        payload = _payload()
        payload.pop(field)
        resp = self._client().post("/api/v1/claims", data=payload)
        assert resp.status_code in (400, 422)
        # The rejected field must be identifiable in the error body.
        assert field in resp.text
        assert len(self.repo.claims) == 0

    @pytest.mark.parametrize(
        "field", ["sin", "employer_bn", "separation_reason"]
    )
    def test_blank_string_field_returns_structured_400(self, field):
        # A present-but-empty string reaches validate_claim_fields, which returns
        # the structured {"errors": [...]} body naming the offending field.
        resp = self._client().post("/api/v1/claims", data=_payload(**{field: "   "}))
        assert resp.status_code == 400
        assert field in self._fields_in(resp.json())
        assert len(self.repo.claims) == 0


# ---------------------------------------------------------------------------
# SIN validation
# ---------------------------------------------------------------------------


class TestSinValidation(_IntakeTestBase):
    def test_valid_luhn_accepted(self):
        resp = self._client().post("/api/v1/claims", data=_payload(sin=_VALID_SIN))
        assert resp.status_code == 202
        assert len(self.repo.claims) == 1

    def test_valid_luhn_with_separators_accepted(self):
        resp = self._client().post("/api/v1/claims", data=_payload(sin="130-692-544"))
        assert resp.status_code == 202

    def test_invalid_luhn_returns_400(self):
        # 130692545 fails the Luhn checksum (valid is ...544).
        resp = self._client().post("/api/v1/claims", data=_payload(sin="130692545"))
        assert resp.status_code == 400
        assert "sin" in self._fields_in(resp.json())
        assert len(self.repo.claims) == 0

    def test_leading_zero_returns_400(self):
        # Starts with 0 -> invalid even if it were Luhn-valid.
        resp = self._client().post("/api/v1/claims", data=_payload(sin="046454286"))
        assert resp.status_code == 400
        assert "sin" in self._fields_in(resp.json())
        assert len(self.repo.claims) == 0

    @pytest.mark.parametrize("bad_sin", ["1306925", "1306925444", "13069254"])
    def test_wrong_length_returns_400(self, bad_sin):
        resp = self._client().post("/api/v1/claims", data=_payload(sin=bad_sin))
        assert resp.status_code == 400
        assert "sin" in self._fields_in(resp.json())
        assert len(self.repo.claims) == 0

    def test_non_numeric_returns_400(self):
        resp = self._client().post("/api/v1/claims", data=_payload(sin="1306925AB"))
        assert resp.status_code == 400
        assert "sin" in self._fields_in(resp.json())


# ---------------------------------------------------------------------------
# BN validation
# ---------------------------------------------------------------------------


class TestBnValidation(_IntakeTestBase):
    def test_valid_9_digit_bn_accepted(self):
        resp = self._client().post("/api/v1/claims", data=_payload(employer_bn="123456789"))
        assert resp.status_code == 202

    def test_valid_15_char_bn_accepted(self):
        resp = self._client().post(
            "/api/v1/claims", data=_payload(employer_bn="123456789RP0002")
        )
        assert resp.status_code == 202

    @pytest.mark.parametrize(
        "bad_bn",
        [
            "not-a-bn",
            "12345678",  # 8 digits
            "1234567890",  # 10 digits
            "123456789XX0001",  # invalid program code
            "123456789RT00A1",  # non-numeric account
        ],
    )
    def test_invalid_bn_returns_400(self, bad_bn):
        resp = self._client().post("/api/v1/claims", data=_payload(employer_bn=bad_bn))
        assert resp.status_code == 400
        assert "employer_bn" in self._fields_in(resp.json())
        assert len(self.repo.claims) == 0


# ---------------------------------------------------------------------------
# insurable_hours validation
# ---------------------------------------------------------------------------


class TestInsurableHoursValidation(_IntakeTestBase):
    def test_zero_hours_returns_400(self):
        resp = self._client().post("/api/v1/claims", data=_payload(insurable_hours=0))
        assert resp.status_code == 400
        assert "insurable_hours" in self._fields_in(resp.json())
        assert len(self.repo.claims) == 0

    def test_negative_hours_returns_400(self):
        resp = self._client().post("/api/v1/claims", data=_payload(insurable_hours=-5))
        assert resp.status_code == 400
        assert "insurable_hours" in self._fields_in(resp.json())

    def test_hours_above_cap_returns_400(self):
        resp = self._client().post(
            "/api/v1/claims", data=_payload(insurable_hours=3121)
        )
        assert resp.status_code == 400
        assert "insurable_hours" in self._fields_in(resp.json())
        assert len(self.repo.claims) == 0

    def test_hours_at_upper_boundary_accepted(self):
        resp = self._client().post(
            "/api/v1/claims", data=_payload(insurable_hours=3120)
        )
        assert resp.status_code == 202

    def test_minimal_positive_hours_accepted(self):
        resp = self._client().post("/api/v1/claims", data=_payload(insurable_hours=1))
        assert resp.status_code == 202


# ---------------------------------------------------------------------------
# insurable_earnings validation
# ---------------------------------------------------------------------------


class TestInsurableEarningsValidation(_IntakeTestBase):
    def test_zero_earnings_returns_400(self):
        resp = self._client().post(
            "/api/v1/claims", data=_payload(insurable_earnings=0)
        )
        assert resp.status_code == 400
        assert "insurable_earnings" in self._fields_in(resp.json())
        assert len(self.repo.claims) == 0

    def test_negative_earnings_returns_400(self):
        resp = self._client().post(
            "/api/v1/claims", data=_payload(insurable_earnings=-0.01)
        )
        assert resp.status_code == 400
        assert "insurable_earnings" in self._fields_in(resp.json())

    def test_positive_earnings_accepted(self):
        resp = self._client().post(
            "/api/v1/claims", data=_payload(insurable_earnings=0.01)
        )
        assert resp.status_code == 202


# ---------------------------------------------------------------------------
# separation_date validation
# ---------------------------------------------------------------------------


class TestSeparationDateValidation(_IntakeTestBase):
    def test_future_date_returns_400(self):
        resp = self._client().post(
            "/api/v1/claims", data=_payload(separation_date=_TOMORROW.isoformat())
        )
        assert resp.status_code == 400
        assert "separation_date" in self._fields_in(resp.json())
        assert len(self.repo.claims) == 0

    def test_today_accepted(self):
        resp = self._client().post(
            "/api/v1/claims", data=_payload(separation_date=_TODAY.isoformat())
        )
        assert resp.status_code == 202

    @pytest.mark.parametrize("bad_date", ["2024/01/01", "01-01-2024", "not-a-date", "2024-13-40"])
    def test_invalid_format_returns_400(self, bad_date):
        resp = self._client().post(
            "/api/v1/claims", data=_payload(separation_date=bad_date)
        )
        assert resp.status_code == 400
        assert "separation_date" in self._fields_in(resp.json())
        assert len(self.repo.claims) == 0


# ---------------------------------------------------------------------------
# postal_code validation
# ---------------------------------------------------------------------------


class TestPostalCodeValidation(_IntakeTestBase):
    @pytest.mark.parametrize("bad_first", ["D1A 0A9", "F1A 0A9", "I1A 0A9", "O1A 0A9", "Q1A 0A9", "U1A 0A9"])
    def test_invalid_first_letter_returns_400(self, bad_first):
        resp = self._client().post("/api/v1/claims", data=_payload(postal_code=bad_first))
        assert resp.status_code == 400
        assert "postal_code" in self._fields_in(resp.json())
        assert len(self.repo.claims) == 0

    def test_valid_full_postal_code_accepted(self):
        resp = self._client().post("/api/v1/claims", data=_payload(postal_code="V6B 1A1"))
        assert resp.status_code == 202

    def test_valid_fsa_only_accepted(self):
        resp = self._client().post("/api/v1/claims", data=_payload(postal_code="K1A"))
        assert resp.status_code == 202

    @pytest.mark.parametrize("bad", ["12345", "K1", "KK1 0A9", "K1A 0A"])
    def test_malformed_postal_code_returns_400(self, bad):
        resp = self._client().post("/api/v1/claims", data=_payload(postal_code=bad))
        assert resp.status_code == 400
        assert "postal_code" in self._fields_in(resp.json())


# ---------------------------------------------------------------------------
# Success-path persistence assertions
# ---------------------------------------------------------------------------


class TestSuccessfulPersistence(_IntakeTestBase):
    def test_claim_persisted_exactly_once_with_received_status(self):
        from shared.models import ClaimStatus

        resp = self._client().post("/api/v1/claims", data=_payload())
        assert resp.status_code == 202
        assert len(self.repo.claims) == 1
        assert self.repo.claims[0].status == ClaimStatus.RECEIVED

    def test_returned_claim_id_matches_stored_record(self):
        resp = self._client().post("/api/v1/claims", data=_payload())
        assert resp.status_code == 202
        returned_id = resp.json()["claim_id"]
        assert str(self.repo.claims[0].claim_id) == returned_id

    def test_raw_sin_never_present_in_stored_record(self):
        resp = self._client().post("/api/v1/claims", data=_payload())
        assert resp.status_code == 202
        stored = self.repo.claims[0]
        assert _VALID_SIN not in stored.sin_hash
        assert len(stored.sin_hash) == 64
        # The raw SIN must not appear anywhere in the serialised stored record.
        assert _VALID_SIN not in str(self.repo.records[0])
        assert _VALID_SIN not in str(stored.to_dict())

    def test_postal_code_reduced_to_fsa(self):
        resp = self._client().post("/api/v1/claims", data=_payload(postal_code="K1A 0A9"))
        assert resp.status_code == 202
        assert self.repo.claims[0].residential_postal_code == "K1A"

    def test_response_body_reports_received_status(self):
        resp = self._client().post("/api/v1/claims", data=_payload())
        assert resp.status_code == 202
        assert resp.json()["status"] == "RECEIVED"


# ---------------------------------------------------------------------------
# Transactional rollback on Kafka publish failure (Requirement 1.5)
#
# The exhaustive two-phase-commit scenarios (success, publish failure, publish
# timeout, PG failure) live in ``test_claim_kafka_rollback.py`` (task 4.3). Here
# we assert only the validation-layer invariant that closes out the 4.4 matrix:
# when the ``claim.received`` publish fails, the endpoint must NOT report success
# and must NOT leave a persisted claim behind — i.e. a failed submission is
# indistinguishable, from a persistence standpoint, from a rejected one.
# ---------------------------------------------------------------------------


class TestPublishFailureLeavesNoPersistedClaim(_IntakeTestBase):
    def _failing_event_producer(self):
        producer = ci_kafka.FakeClaimEventProducer()
        producer.fail_next_publish()
        return producer

    def teardown_method(self):
        super().teardown_method()
        ci_main.app.state.event_producer = None

    def test_publish_failure_is_not_reported_as_accepted(self):
        # Wire the real rollback path: the endpoint publishes via
        # ``app.state.event_producer`` and rolls back the PG write on failure.
        ci_main.app.state.event_producer = self._failing_event_producer()

        resp = self._client().post("/api/v1/claims", data=_payload())

        # A publish failure must never be reported to the claimant as accepted.
        assert resp.status_code != 202, (
            "Kafka publish failed but the endpoint returned 202; the PG write "
            "was not rolled back."
        )
        # No usable claim_id should be returned once the write is rolled back.
        body = resp.json()
        assert "claim_id" not in body
        # And the rolled-back claim must not remain persisted.
        assert len(self.repo.claims) == 0
        assert len(self.repo.records) == 0
