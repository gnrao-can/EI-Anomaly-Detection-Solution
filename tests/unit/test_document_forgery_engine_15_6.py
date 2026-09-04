"""
tests/unit/test_document_forgery_engine_15_6.py
===============================================
Task-15.6 unit tests for the Document Forgery Engine.

This file collects the *specific boundary scenarios* enumerated by task 15.6,
verifying the forensic decision logic already implemented in task 15.2
(PDF metadata creation-timestamp forensics + font fingerprinting) and task 15.3
(digital signature verification + LLM termination-narrative style analysis).

Task 15.6 scenarios:

  * No digital signature -> CRITICAL Indicator (Requirement 7.6).
  * PDF creation_timestamp within 24h of issue_date -> NO flag; exactly
    24h + 1s later -> CRITICAL flag (Requirement 7.3, exercised here to anchor
    the font/signature boundary suite around the engine's temporal rule).
  * Font match -> NO flag; font mismatch (family or point size) -> CRITICAL
    flag (Requirement 7.4).
  * Employer with < 3 prior authenticated ROEs -> font check SKIPPED
    (Requirement 7.4 profile cannot be established); employer with < 5 prior
    authenticated ROEs -> LLM style check SKIPPED (Requirement 7.9).

These are authored as a distinct file focused on the task-15.6 boundary matrix.
The exhaustive extraction / pdfminer / pypdf / cryptography round-trip coverage
lives in the sibling files ``test_document_forgery_forensics.py`` (15.2) and
``test_document_forgery_signature_payroll_style.py`` (15.3); this file does not
duplicate those and asserts only the four boundary behaviours the task calls
out, plus the immediately adjacent boundary points that give each scenario its
"no flag vs flag" meaning.

The hyphenated ``services/document-forgery-engine`` modules import each other by
bare name, so they are loaded under unique namespaced keys via the shared
``load_service_module`` helper (test isolation). The LLM Service client is a
mock async callable; no live network / LLM / CRA calls are made, and only tiny
in-memory font profiles / injected timestamps are used.

Requirements: 7.4, 7.5, 7.6, 7.9
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from shared.models import EngineType, Severity
from tests.conftest import load_service_module

# ---------------------------------------------------------------------------
# Load the hyphenated document-forgery-engine service modules under unique,
# collision-free namespaced keys (see tests/conftest.load_service_module).
# ---------------------------------------------------------------------------

_metadata = load_service_module(
    "document-forgery-engine", "metadata_forensics", siblings=["metadata_forensics"]
)
_font = load_service_module(
    "document-forgery-engine", "font_fingerprint", siblings=["font_fingerprint"]
)
_signature = load_service_module(
    "document-forgery-engine",
    "signature_verification",
    siblings=["signature_verification"],
)
_style = load_service_module(
    "document-forgery-engine", "style_analysis", siblings=["style_analysis"]
)

# Metadata / creation-timestamp surface (Requirement 7.3)
check_pdf_creation_vs_issue_date = _metadata.check_pdf_creation_vs_issue_date

# Font fingerprint surface (Requirement 7.4)
FontProfile = _font.FontProfile
check_font_profile = _font.check_font_profile
MIN_FONT_ROES = _font.MIN_AUTHENTICATED_ROES_FOR_FONT_CHECK

# Signature surface (Requirements 7.5, 7.6)
extract_signature_certificate_hash = _signature.extract_signature_certificate_hash
check_digital_signature = _signature.check_digital_signature
REASON_NO_SIGNATURE = _signature.REASON_NO_SIGNATURE

# Style surface (Requirement 7.9)
StyleAnalysisChecker = _style.StyleAnalysisChecker
MIN_STYLE_ROES = _style.MIN_AUTHENTICATED_ROES_FOR_STYLE_CHECK


# ===========================================================================
# Scenario 1 — No digital signature -> CRITICAL Indicator (Requirement 7.6)
# ===========================================================================


def test_no_digital_signature_flags_critical():
    """A document explicitly carrying no signature yields one CRITICAL flag."""
    extraction = extract_signature_certificate_hash({"signature_present": False})
    indicators = check_digital_signature(
        extraction, registered_cert_hash="deadbeef", claim_id="claim-15-6-a"
    )
    assert len(indicators) == 1
    ind = indicators[0]
    assert ind.severity == Severity.CRITICAL
    assert ind.engine == EngineType.DOCUMENT_FORGERY
    assert ind.algorithm == "digital_signature_verification"
    assert ind.payload["flag"] == REASON_NO_SIGNATURE
    assert ind.payload["signature_present"] is False


def test_present_matching_signature_no_flag():
    """A present signature whose hash matches the registry is NOT flagged."""
    extraction = extract_signature_certificate_hash(
        {"signature_cert_hash": "ABCD1234"}
    )
    assert check_digital_signature(extraction, registered_cert_hash="abcd1234") == []


# ===========================================================================
# Scenario 2 — creation_timestamp vs issue_date 24h boundary (Requirement 7.3)
# ===========================================================================

_ISSUE_DATE = datetime(2024, 3, 1, 9, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "offset",
    [
        timedelta(0),
        timedelta(hours=1),
        timedelta(hours=23, minutes=59, seconds=59),
        timedelta(hours=24),  # exactly at the 24h cutoff -> still no flag
    ],
)
def test_creation_within_24h_no_flag(offset):
    creation = _ISSUE_DATE + offset
    assert check_pdf_creation_vs_issue_date(creation, _ISSUE_DATE) == []


def test_creation_24h_plus_one_second_flags_critical():
    creation = _ISSUE_DATE + timedelta(hours=24, seconds=1)
    indicators = check_pdf_creation_vs_issue_date(
        creation, _ISSUE_DATE, claim_id="claim-15-6-b"
    )
    assert len(indicators) == 1
    ind = indicators[0]
    assert ind.severity == Severity.CRITICAL
    assert ind.engine == EngineType.DOCUMENT_FORGERY
    assert ind.algorithm == "pdf_creation_timestamp_forensics"
    # Requirement 7.3: the Indicator must specify BOTH timestamps.
    assert ind.payload["creation_timestamp"] == creation.isoformat()
    assert ind.payload["issue_date"] == _ISSUE_DATE.isoformat()


# ===========================================================================
# Scenario 3 — Font match (no flag) vs font mismatch (flag) (Requirement 7.4)
# ===========================================================================

# An established employer profile: >= 3 prior authenticated ROEs so the check
# actually runs (the skip boundary is exercised separately in scenario 4).
_ESTABLISHED_ROES = MIN_FONT_ROES + 2


def test_font_match_no_flag():
    observed = FontProfile(font_family="Helvetica", point_size=12.0)
    expected = FontProfile(font_family="Helvetica", point_size=12.0)
    assert (
        check_font_profile(
            observed, expected, prior_authenticated_roe_count=_ESTABLISHED_ROES
        )
        == []
    )


def test_font_family_mismatch_flags_critical():
    observed = FontProfile(font_family="Arial", point_size=12.0)
    expected = FontProfile(font_family="Helvetica", point_size=12.0)
    indicators = check_font_profile(
        observed,
        expected,
        prior_authenticated_roe_count=_ESTABLISHED_ROES,
        claim_id="claim-15-6-c",
    )
    assert len(indicators) == 1
    ind = indicators[0]
    assert ind.severity == Severity.CRITICAL
    assert ind.engine == EngineType.DOCUMENT_FORGERY
    assert ind.algorithm == "font_fingerprint"
    # Requirement 7.4: name both expected and observed family + size.
    assert ind.payload["expected_font_family"] == "Helvetica"
    assert ind.payload["observed_font_family"] == "Arial"
    assert ind.payload["family_mismatch"] is True
    assert ind.payload["size_mismatch"] is False


def test_font_point_size_mismatch_flags_critical():
    observed = FontProfile(font_family="Helvetica", point_size=11.0)
    expected = FontProfile(font_family="Helvetica", point_size=12.0)
    indicators = check_font_profile(
        observed, expected, prior_authenticated_roe_count=_ESTABLISHED_ROES
    )
    assert len(indicators) == 1
    ind = indicators[0]
    assert ind.severity == Severity.CRITICAL
    assert ind.payload["size_mismatch"] is True
    assert ind.payload["family_mismatch"] is False
    assert ind.payload["expected_point_size"] == 12.0
    assert ind.payload["observed_point_size"] == 11.0


# ===========================================================================
# Scenario 4a — Employer with < 3 prior ROEs -> font check SKIPPED (Req 7.4)
# ===========================================================================


@pytest.mark.parametrize("prior_roe_count", [0, 1, MIN_FONT_ROES - 1])
def test_font_check_skipped_below_three_prior_roes(prior_roe_count):
    """A blatant font mismatch must NOT flag when the profile is not established."""
    observed = FontProfile(font_family="Comic Sans", point_size=48.0)
    expected = FontProfile(font_family="Helvetica", point_size=12.0)
    assert (
        check_font_profile(
            observed, expected, prior_authenticated_roe_count=prior_roe_count
        )
        == []
    )


def test_font_check_runs_at_three_prior_roes_boundary():
    """At exactly the 3-ROE threshold the profile IS established and mismatch flags."""
    observed = FontProfile(font_family="Comic Sans", point_size=48.0)
    expected = FontProfile(font_family="Helvetica", point_size=12.0)
    indicators = check_font_profile(
        observed, expected, prior_authenticated_roe_count=MIN_FONT_ROES
    )
    assert len(indicators) == 1
    assert indicators[0].severity == Severity.CRITICAL


# ===========================================================================
# Scenario 4b — Employer with < 5 prior ROEs -> LLM style check SKIPPED (Req 7.9)
# ===========================================================================


@pytest.mark.parametrize("prior_roe_count", [0, 1, MIN_STYLE_ROES - 1])
async def test_style_check_skipped_below_five_prior_roes(prior_roe_count):
    """The LLM must not even be invoked below the 5-ROE corpus minimum (Req 7.9)."""
    llm_invoked = False

    async def mock_llm(narrative, corpus):
        nonlocal llm_invoked
        llm_invoked = True
        return {"inconsistent": True, "score": 0.99}

    checker = StyleAnalysisChecker(mock_llm)
    corpus = [f"prior narrative {i}" for i in range(prior_roe_count)]
    indicators = await checker.check(
        "You are terminated.", corpus, claim_id="claim-15-6-d"
    )
    assert indicators == []
    assert llm_invoked is False


async def test_style_check_runs_at_five_prior_roes_boundary_flags_medium():
    """At exactly 5 prior ROEs the mocked LLM runs; an inconsistent verdict -> MEDIUM."""
    llm_invoked = False

    async def mock_llm(narrative, corpus):
        nonlocal llm_invoked
        llm_invoked = True
        assert len(corpus) == MIN_STYLE_ROES
        return {"inconsistent": True, "score": 0.91}

    checker = StyleAnalysisChecker(mock_llm)
    corpus = [f"prior narrative {i}" for i in range(MIN_STYLE_ROES)]
    indicators = await checker.check(
        "You are terminated.", corpus, claim_id="claim-15-6-e"
    )
    assert llm_invoked is True
    assert len(indicators) == 1
    ind = indicators[0]
    assert ind.severity == Severity.MEDIUM
    assert ind.engine == EngineType.DOCUMENT_FORGERY
    assert ind.algorithm == "llm_style_analysis"
    assert ind.payload["flag"] == "STYLE_INCONSISTENT"
    assert ind.payload["corpus_size"] == MIN_STYLE_ROES
