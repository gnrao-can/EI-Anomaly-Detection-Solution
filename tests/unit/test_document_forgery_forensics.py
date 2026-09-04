"""
tests/unit/test_document_forgery_forensics.py
==============================================
Unit + property tests for the Document Forgery Engine PDF metadata forensics
and font fingerprinting (task 15.2).

Coverage (design "5. Document Forgery Engine", steps 3-4):

  * PDF creation-timestamp vs issue-date "> 24h" boundary (Requirement 7.3):
      - creation exactly issue + 24h        -> NO Indicator
      - creation issue + 24h + 1s           -> CRITICAL Indicator
      - creation before / equal to issue    -> NO Indicator
  * pypdf round-trip: a PDF whose /CreationDate we set is read back with the
    same creation timestamp (uses the pinned pypdf 5.1.0 when available).
  * pdfminer.six font extraction: a hand-built PDF with a known BaseFont / Tf
    size yields the expected FontProfile (uses pinned pdfminer.six 20231228).
  * Font profile mismatch (Requirement 7.4):
      - matching family + size              -> NO Indicator
      - font family mismatch                -> CRITICAL Indicator
      - point size mismatch                 -> CRITICAL Indicator
  * Font check skipped when employer has < 3 prior authenticated ROEs
    (design step 4 / Requirement 7.4).

The service directory is hyphenated (``services/document-forgery-engine``) and
its modules import each other by bare name, so we load them under unique
namespaced keys via the shared ``load_service_module`` helper.

Requirements: 7.3, 7.4
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import pytest

from shared.models import EngineType, Severity
from tests.conftest import load_service_module

# ---------------------------------------------------------------------------
# Load the hyphenated document-forgery-engine service modules under unique
# namespaced keys.
# ---------------------------------------------------------------------------

_metadata = load_service_module(
    "document-forgery-engine", "metadata_forensics", siblings=["metadata_forensics"]
)
_font = load_service_module(
    "document-forgery-engine", "font_fingerprint", siblings=["font_fingerprint"]
)

extract_pdf_creation_timestamp = _metadata.extract_pdf_creation_timestamp
check_pdf_creation_vs_issue_date = _metadata.check_pdf_creation_vs_issue_date
CREATION_GRACE = _metadata.CREATION_GRACE

FontProfile = _font.FontProfile
extract_font_profile = _font.extract_font_profile
check_font_profile = _font.check_font_profile
MIN_ROES = _font.MIN_AUTHENTICATED_ROES_FOR_FONT_CHECK


# ---------------------------------------------------------------------------
# Helpers: build tiny in-memory PDFs
# ---------------------------------------------------------------------------


def _build_pdf_with_font(font: str = "Helvetica", size: int = 12) -> bytes:
    """Hand-build a minimal single-page PDF drawing text in the given font/size."""
    content = f"BT /F1 {size} Tf 72 100 Td (Hello ROE) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /" + font.encode() + b" >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref_pos = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF".encode()
    )
    return out.getvalue()


def _build_pdf_with_creation_date(date_str: str) -> bytes:
    """Build a minimal PDF carrying a /CreationDate in standard PDF date form."""
    info = b"<< /CreationDate (" + date_str.encode() + b") >>"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] >>",
        info,
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref_pos = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info 4 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF".encode()
    )
    return out.getvalue()


# ---------------------------------------------------------------------------
# PDF creation-timestamp vs issue-date boundary (Requirement 7.3)
# ---------------------------------------------------------------------------

_ISSUE = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def test_creation_exactly_24h_after_issue_no_flag():
    creation = _ISSUE + timedelta(hours=24)
    assert check_pdf_creation_vs_issue_date(creation, _ISSUE) == []


def test_creation_24h_plus_one_second_flags_critical():
    creation = _ISSUE + timedelta(hours=24, seconds=1)
    indicators = check_pdf_creation_vs_issue_date(creation, _ISSUE)
    assert len(indicators) == 1
    ind = indicators[0]
    assert ind.severity == Severity.CRITICAL
    assert ind.engine == EngineType.DOCUMENT_FORGERY
    assert ind.algorithm == "pdf_creation_timestamp_forensics"
    # Both timestamps must appear in the Indicator (Requirement 7.3).
    assert ind.payload["creation_timestamp"] == creation.isoformat()
    assert ind.payload["issue_date"] == _ISSUE.isoformat()


def test_creation_before_issue_no_flag():
    creation = _ISSUE - timedelta(days=2)
    assert check_pdf_creation_vs_issue_date(creation, _ISSUE) == []


def test_creation_equal_to_issue_no_flag():
    assert check_pdf_creation_vs_issue_date(_ISSUE, _ISSUE) == []


def test_missing_timestamps_skip_check():
    assert check_pdf_creation_vs_issue_date(None, _ISSUE) == []
    assert check_pdf_creation_vs_issue_date(_ISSUE, None) == []


def test_naive_datetimes_treated_as_utc():
    naive_issue = datetime(2024, 1, 15, 12, 0, 0)
    naive_creation = naive_issue + timedelta(hours=25)
    indicators = check_pdf_creation_vs_issue_date(naive_creation, naive_issue)
    assert len(indicators) == 1


# ---------------------------------------------------------------------------
# pypdf creation-date extraction round-trip
# ---------------------------------------------------------------------------


def test_extract_creation_timestamp_from_pdf_bytes():
    pdf = _build_pdf_with_creation_date("D:20240115120000+00'00'")
    creation = extract_pdf_creation_timestamp(pdf)
    assert creation is not None
    assert creation.year == 2024 and creation.month == 1 and creation.day == 15
    assert creation.hour == 12


def test_extract_creation_timestamp_none_when_absent():
    pdf = _build_pdf_with_font("Helvetica", 12)  # no /CreationDate
    assert extract_pdf_creation_timestamp(pdf) is None


def test_pypdf_roundtrip_creation_date():
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/CreationDate": "D:20240601083000+00'00'"})
    buf = io.BytesIO()
    writer.write(buf)
    creation = extract_pdf_creation_timestamp(buf.getvalue())
    assert creation is not None
    assert (creation.year, creation.month, creation.day) == (2024, 6, 1)
    assert (creation.hour, creation.minute) == (8, 30)


# ---------------------------------------------------------------------------
# pdfminer.six font extraction
# ---------------------------------------------------------------------------


def test_extract_font_profile_reads_family_and_size():
    pytest.importorskip("pdfminer")
    pdf = _build_pdf_with_font("Helvetica", 12)
    profile = extract_font_profile(pdf)
    assert profile is not None
    assert profile.font_family == "Helvetica"
    assert abs(profile.point_size - 12.0) < 0.5


def test_extract_font_profile_different_font():
    pytest.importorskip("pdfminer")
    pdf = _build_pdf_with_font("Courier", 10)
    profile = extract_font_profile(pdf)
    assert profile is not None
    assert profile.font_family == "Courier"
    assert abs(profile.point_size - 10.0) < 0.5


# ---------------------------------------------------------------------------
# Font profile mismatch (Requirement 7.4)
# ---------------------------------------------------------------------------


def test_matching_font_profile_no_flag():
    observed = FontProfile(font_family="Helvetica", point_size=12.0)
    expected = FontProfile(font_family="Helvetica", point_size=12.0)
    indicators = check_font_profile(
        observed, expected, prior_authenticated_roe_count=5
    )
    assert indicators == []


def test_font_family_mismatch_flags_critical():
    observed = FontProfile(font_family="Courier", point_size=12.0)
    expected = FontProfile(font_family="Helvetica", point_size=12.0)
    indicators = check_font_profile(
        observed, expected, prior_authenticated_roe_count=5
    )
    assert len(indicators) == 1
    ind = indicators[0]
    assert ind.severity == Severity.CRITICAL
    assert ind.algorithm == "font_fingerprint"
    assert ind.payload["expected_font_family"] == "Helvetica"
    assert ind.payload["observed_font_family"] == "Courier"
    assert ind.payload["family_mismatch"] is True


def test_point_size_mismatch_flags_critical():
    observed = FontProfile(font_family="Helvetica", point_size=14.0)
    expected = FontProfile(font_family="Helvetica", point_size=12.0)
    indicators = check_font_profile(
        observed, expected, prior_authenticated_roe_count=3
    )
    assert len(indicators) == 1
    assert indicators[0].payload["size_mismatch"] is True
    assert indicators[0].payload["expected_point_size"] == 12.0
    assert indicators[0].payload["observed_point_size"] == 14.0


def test_font_family_case_insensitive_match():
    observed = FontProfile(font_family="helvetica", point_size=12.0)
    expected = FontProfile(font_family="Helvetica", point_size=12.0)
    assert check_font_profile(observed, expected, prior_authenticated_roe_count=4) == []


def test_point_size_within_tolerance_no_flag():
    observed = FontProfile(font_family="Helvetica", point_size=12.3)
    expected = FontProfile(font_family="Helvetica", point_size=12.0)
    # default tolerance is 0.5pt
    assert check_font_profile(observed, expected, prior_authenticated_roe_count=5) == []


# ---------------------------------------------------------------------------
# Font check skipped when < 3 prior authenticated ROEs (Requirement 7.4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("count", [0, 1, 2])
def test_font_check_skipped_below_three_prior_roes(count):
    observed = FontProfile(font_family="Courier", point_size=20.0)
    expected = FontProfile(font_family="Helvetica", point_size=12.0)
    # Even though this is a blatant mismatch, the check must be skipped.
    indicators = check_font_profile(
        observed, expected, prior_authenticated_roe_count=count
    )
    assert indicators == []


def test_font_check_runs_at_exactly_three_prior_roes():
    observed = FontProfile(font_family="Courier", point_size=20.0)
    expected = FontProfile(font_family="Helvetica", point_size=12.0)
    indicators = check_font_profile(
        observed, expected, prior_authenticated_roe_count=MIN_ROES
    )
    assert len(indicators) == 1


def test_font_check_skipped_when_profile_missing():
    expected = FontProfile(font_family="Helvetica", point_size=12.0)
    assert check_font_profile(None, expected, prior_authenticated_roe_count=5) == []
    observed = FontProfile(font_family="Helvetica", point_size=12.0)
    assert check_font_profile(observed, None, prior_authenticated_roe_count=5) == []
