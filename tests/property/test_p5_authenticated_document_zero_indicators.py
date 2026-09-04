"""
tests/property/test_p5_authenticated_document_zero_indicators.py
================================================================
Property 5: Authenticated Document Zero Indicators.

**Validates: Requirements 7.12**

Statement
---------
*For any* ROE document D that is identical (byte-for-byte) to a previously
authenticated ROE in the employer's document registry, the
Document_Forgery_Engine SHALL produce **zero** forgery Indicators
(the authenticated-document invariant, Requirement 7.12).

An "authenticated" ROE is one that passes every forensic check the engine runs
(design "5. Document Forgery Engine", steps 1-7):

  1. OCR extraction (task 15.1) — every one of the 11 mandatory ROE fields is
     extracted with confidence >= 0.85, so no ``EXTRACTION_LOW_CONFIDENCE``
     Indicator (Requirement 7.1).
  2. Internal consistency (task 15.1) — ``total_insurable_hours x hourly_rate``
     exactly equals ``total_insurable_earnings`` (0% deviation), and
     ``last_day_paid >= first_day_worked``, so no consistency Indicator
     (Requirement 7.2).
  3. PDF metadata forensics (task 15.2) — the PDF creation timestamp is within
     24h of (never more than 24h after) the declared issue date, so no
     ``PDF_CREATED_AFTER_ISSUE`` Indicator (Requirement 7.3).
  4. Font fingerprint (task 15.2) — the observed font profile is byte-for-byte
     the employer's registry profile (family + point size), and the employer
     has >= 3 prior authenticated ROEs, so no ``FONT_PROFILE_MISMATCH``
     Indicator (Requirement 7.4).
  5. Digital signature (task 15.3) — the signer certificate hash matches the
     employer's registered certificate hash, so no signature Indicator
     (Requirements 7.5, 7.6).
  6. Payroll cross-reference (task 15.3) — the CRA Adapter (mocked) confirms the
     SIN is FOUND in the employer's payroll, so no ``SIN_NOT_IN_PAYROLL``
     Indicator (Requirement 7.7).
  7. LLM style analysis (task 15.3) — the LLM Service (mocked) classifies the
     termination narrative as consistent with the employer's style, and the
     employer has >= 5 prior authenticated ROEs, so no ``STYLE_INCONSISTENT``
     Indicator (Requirement 7.8).

The engine's own ``/engine/analyze`` pipeline (``main.analyze``) is driven end
to end so the invariant is asserted over the *composed* engine, not just the
individual check functions. The CRA Adapter and LLM Service are injected as
in-process fakes returning authenticated results — no live network is used.

Import isolation
----------------
The ``document-forgery-engine`` service directory is hyphenated and its modules
import each other by bare name, so it is not importable as a package. We use the
shared :func:`tests.conftest.load_service_module` helper to load ``main`` (and
its siblings) under a unique, collision-free namespaced key, keeping the whole
property/unit suite collision-free when run together.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from shared.models import EngineType
from tests.conftest import load_service_module

# ---------------------------------------------------------------------------
# Load the hyphenated document-forgery-engine ``main`` module (and its bare-name
# siblings) under unique namespaced keys via the shared helper.
# ---------------------------------------------------------------------------

_SIBLINGS = [
    "config",
    "ocr",
    "consistency",
    "metadata_forensics",
    "font_fingerprint",
    "signature_verification",
    "payroll_cross_reference",
    "style_analysis",
    "kafka_publisher",
    "main",
]

_main = load_service_module("document-forgery-engine", "main", siblings=_SIBLINGS)
_payroll = load_service_module(
    "document-forgery-engine", "payroll_cross_reference", siblings=_SIBLINGS
)
_style = load_service_module(
    "document-forgery-engine", "style_analysis", siblings=_SIBLINGS
)

# Task 29.1: /engine/analyze now enforces the INVESTIGATOR role via the shared
# GCKey dependency. Inject an authenticated principal so this property exercises
# the engine pipeline rather than the auth gate (the 401/403 boundary is covered
# in tests/unit/test_auth_service_wiring.py).
from shared.auth import get_current_user as _get_current_user  # noqa: E402

_main.app.dependency_overrides[_get_current_user] = lambda: {"roles": ["INVESTIGATOR"]}

PayrollLookup = _payroll.PayrollLookup
STATUS_FOUND = _payroll.STATUS_FOUND
StyleAnalysisChecker = _style.StyleAnalysisChecker

try:
    from fastapi.testclient import TestClient

    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001 - fastapi not importable in this sandbox
    _HAS_FASTAPI = False


# ---------------------------------------------------------------------------
# Hypothesis profile — consistent with the other property tests (P1-P15).
# Registering is idempotent-safe: another property module (or conftest) may
# already have registered "ci".
# ---------------------------------------------------------------------------
try:
    settings.register_profile("ci", max_examples=200)
except Exception:  # noqa: BLE001 - already registered elsewhere
    pass
settings.load_profile("ci")


# ---------------------------------------------------------------------------
# Authenticated-document fakes (mock CRA / LLM adapters -> authenticated result)
# ---------------------------------------------------------------------------


class _FoundPayrollClient:
    """CRA payroll client stub that always confirms the SIN is in payroll.

    Mirrors ``CRAPayrollClient.check_sin_in_payroll``'s async signature but does
    no I/O, so the payroll cross-reference (Requirement 7.7) produces no
    Indicator (SIN FOUND).
    """

    async def check_sin_in_payroll(
        self,
        sin_hash: str,
        employer_bn: str,
        employment_start: str,
        employment_end: str,
    ) -> "PayrollLookup":
        return PayrollLookup(status=STATUS_FOUND)


async def _consistent_style_client(narrative: str, corpus: list[str]) -> dict:
    """LLM style client stub that classifies the narrative as consistent.

    ``inconsistent=False`` means the async style analysis (Requirement 7.8)
    produces no ``STYLE_INCONSISTENT`` Indicator.
    """
    return {"inconsistent": False, "score": 0.02}


# ---------------------------------------------------------------------------
# Strategies for the fields of an *authenticated* ROE document.
# ---------------------------------------------------------------------------

# Two-decimal-place, finite, non-negative money / hours amounts. Earnings are
# derived exactly as hours * hourly_rate so the consistency check sees a 0%
# deviation (no Indicator).
_hours_strategy = st.decimals(
    min_value=Decimal("1.00"),
    max_value=Decimal("2080.00"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)
_rate_strategy = st.decimals(
    min_value=Decimal("15.00"),
    max_value=Decimal("500.00"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)

# Font families and point sizes an employer profile might use. The observed
# profile is set equal to the expected registry profile, so no mismatch.
_font_family_strategy = st.sampled_from(
    ["Helvetica", "Arial", "Times New Roman", "Calibri", "Courier New"]
)
_point_size_strategy = st.sampled_from([9.0, 9.5, 10.0, 10.5, 11.0, 12.0])

# The PDF was created at or before issue_date + 24h (never strictly after), so
# the metadata-forensics check does not flag. Offsets span "well before" to
# "exactly at the 24h boundary".
_creation_offset_hours_strategy = st.floats(
    min_value=-72.0, max_value=24.0, allow_nan=False, allow_infinity=False
)

# Employer registry maturity: at least 5 prior authenticated ROEs guarantees
# BOTH the font check (>= 3) and the LLM style check (>= 5) actually run, so we
# exercise the checks and still assert zero Indicators (not a trivial skip).
_prior_roe_count_strategy = st.integers(min_value=5, max_value=250)


def _authenticated_document(
    *,
    hours: Decimal,
    hourly_rate: Decimal,
    font_family: str,
    point_size: float,
    creation_offset_hours: float,
    prior_roe_count: int,
) -> dict:
    """Build a document payload representing an *authenticated* ROE.

    Every field is set so that each forensic check passes:
      * earnings == hours * hourly_rate (exact, 0% deviation)
      * last_day_paid >= first_day_worked
      * all 11 OCR field confidences >= 0.85
      * pdf_creation_timestamp <= issue_date + 24h
      * observed font profile == employer registry profile
      * signer cert hash == registered cert hash
      * SIN present + employment period supplied (CRA fake -> FOUND)
      * >= 5 prior authenticated ROEs (font + style checks run)
    """
    earnings = (hours * hourly_rate).quantize(Decimal("0.01"))

    first_day_worked = "2024-01-01"
    last_day_paid = "2024-06-30"  # strictly after first_day_worked
    issue_date = datetime(2024, 7, 2, 12, 0, 0, tzinfo=timezone.utc)
    creation_ts = issue_date + timedelta(hours=creation_offset_hours)

    registered_cert_hash = "a" * 64  # canonical lowercase-hex SHA-256 form

    # High confidence for every one of the 11 mandatory ROE fields (>= 0.85).
    mandatory_fields = (
        "employer_name",
        "employer_bn",
        "employee_name",
        "sin",
        "occupation",
        "first_day_worked",
        "last_day_paid",
        "total_insurable_hours",
        "total_insurable_earnings",
        "hourly_rate",
        "reason_for_separation",
    )
    confidences = {name: 0.99 for name in mandatory_fields}

    return {
        # OCR-visible field values (the stub extractor reads these).
        "fields": {
            "employer_bn": "123456789RT0001",
            "sin": "sin-hash-authenticated",
            "first_day_worked": first_day_worked,
            "last_day_paid": last_day_paid,
            "total_insurable_hours": str(hours),
            "hourly_rate": str(hourly_rate),
            "total_insurable_earnings": str(earnings),
        },
        "confidences": confidences,
        # Consistency inputs (top-level overrides used by the pipeline).
        "hours": str(hours),
        "hourly_rate": str(hourly_rate),
        "earnings": str(earnings),
        "first_day_worked": first_day_worked,
        "last_day_paid": last_day_paid,
        # Metadata forensics: creation within 24h of issue date.
        "issue_date": issue_date.isoformat(),
        "pdf_creation_timestamp": creation_ts.isoformat(),
        # Font fingerprint: observed profile == employer registry profile.
        "font_profile": {"font_family": font_family, "point_size": point_size},
        "document_registry": {
            "prior_authenticated_roe_count": prior_roe_count,
            "expected_font_profile": {
                "font_family": font_family,
                "point_size": point_size,
            },
            "prior_roe_narratives": ["Company-wide restructuring."] * prior_roe_count,
        },
        # Digital signature: signer cert hash == registered cert hash.
        "signature_cert_hash": registered_cert_hash,
        "signature_registry": {"registered_cert_hash": registered_cert_hash},
        # Payroll cross-reference inputs (CRA fake returns FOUND).
        "sin_hash": "sin-hash-authenticated",
        "employer_bn": "123456789RT0001",
        "employment_start": first_day_worked,
        "employment_end": last_day_paid,
        # Termination narrative for the LLM style check (fake -> consistent).
        "termination_narrative": "Company-wide restructuring.",
    }


def _build_client() -> "TestClient":
    """Build a TestClient with authenticated CRA / LLM adapters wired in.

    The lifespan runs on ``__enter__``; we override the injectable collaborators
    on ``app.state`` afterwards so the payroll and style checks see
    authenticated results (SIN FOUND, style consistent).
    """
    client = TestClient(_main.app)
    return client


# ---------------------------------------------------------------------------
# Property 5 — Hypothesis-driven invariant over the composed engine pipeline
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not importable")
@given(
    hours=_hours_strategy,
    hourly_rate=_rate_strategy,
    font_family=_font_family_strategy,
    point_size=_point_size_strategy,
    creation_offset_hours=_creation_offset_hours_strategy,
    prior_roe_count=_prior_roe_count_strategy,
)
def test_P5_authenticated_document_produces_zero_indicators(
    hours,
    hourly_rate,
    font_family,
    point_size,
    creation_offset_hours,
    prior_roe_count,
):
    """Property 5. Validates: Requirements 7.12

    An authenticated ROE (every forensic check passes) run through the full
    ``/engine/analyze`` pipeline yields zero forgery Indicators.
    """
    document = _authenticated_document(
        hours=hours,
        hourly_rate=hourly_rate,
        font_family=font_family,
        point_size=point_size,
        creation_offset_hours=creation_offset_hours,
        prior_roe_count=prior_roe_count,
    )

    with _build_client() as client:
        # Inject authenticated collaborators (mock CRA / LLM adapters).
        client.app.state.payroll_client = _FoundPayrollClient()
        client.app.state.style_checker = StyleAnalysisChecker(
            _consistent_style_client
        )

        response = client.post(
            "/engine/analyze",
            json={"claim_id": str(uuid.uuid4()), "document": document},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["indicator_count"] == 0, (
        f"expected zero forgery Indicators for an authenticated document, "
        f"got {body['indicator_count']}: {body['indicators']}"
    )
    assert body["indicators"] == []
    # Every Indicator (if any) would be a DOCUMENT_FORGERY Indicator; assert the
    # engine emitted none of them for the authenticated document.
    assert not any(
        ind["engine"] == EngineType.DOCUMENT_FORGERY.value
        for ind in body["indicators"]
    )


# ---------------------------------------------------------------------------
# Direct (deterministic) authenticated-document example complementing the
# property, so a regression is obvious even without the Hypothesis search.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not importable")
def test_P5_authenticated_document_direct_example():
    """Property 5. Validates: Requirements 7.12

    A concrete authenticated ROE (round earnings, exact 24h creation boundary,
    mature employer registry) produces zero forgery Indicators.
    """
    document = _authenticated_document(
        hours=Decimal("1000.00"),
        hourly_rate=Decimal("25.00"),
        font_family="Helvetica",
        point_size=10.0,
        creation_offset_hours=24.0,  # exactly at the 24h boundary -> no flag
        prior_roe_count=12,
    )

    with _build_client() as client:
        client.app.state.payroll_client = _FoundPayrollClient()
        client.app.state.style_checker = StyleAnalysisChecker(
            _consistent_style_client
        )
        response = client.post(
            "/engine/analyze",
            json={"claim_id": str(uuid.uuid4()), "document": document},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["indicator_count"] == 0
    assert body["indicators"] == []
