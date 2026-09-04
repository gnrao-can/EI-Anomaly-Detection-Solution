"""
tests/unit/test_document_forgery_engine.py
==========================================
Unit tests for the Document Forgery Engine (task 15.1).

Coverage (design "5. Document Forgery Engine", steps 1-2):
  * hours * hourly_rate vs earnings boundary (Requirement 7.2):
      - deviation exactly 2%   -> NO flag
      - deviation 2.01%        -> CRITICAL flag
  * last_day_paid < first_day_worked -> CRITICAL; same-day / normal order -> no flag
    (Requirement 7.2).
  * OCR extraction confidence boundary (Requirement 7.1):
      - confidence exactly 0.85 -> NO EXTRACTION_LOW_CONFIDENCE flag
      - confidence 0.84         -> one EXTRACTION_LOW_CONFIDENCE flag
  * StubOCRExtractor returns all 11 mandatory ROE fields, each with a confidence.

All money/hours arithmetic is exact ``Decimal``. Tests use the deterministic
StubOCRExtractor (DI) — no model download, no network.

The service directory is hyphenated (``services/document-forgery-engine``) and
its modules import each other by bare name (``from ocr import ...``), so we load
them under unique namespaced keys via the shared ``load_service_module`` helper,
which restores any pre-existing bare-name modules afterwards. This keeps the
unit suite collision-free when run alongside the other engine tests.

Requirements: 7.1, 7.2
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from shared.models import EngineType, Severity
from tests.conftest import load_service_module

# ---------------------------------------------------------------------------
# Load the hyphenated document-forgery-engine service modules under unique
# namespaced keys. `consistency` imports `ocr` by bare name, so both siblings
# are loaded together; bare names are snapshotted/restored by the helper.
# ---------------------------------------------------------------------------

_ocr = load_service_module(
    "document-forgery-engine", "ocr", siblings=["ocr"]
)
_consistency = load_service_module(
    "document-forgery-engine", "consistency", siblings=["ocr", "consistency"]
)

MANDATORY_ROE_FIELDS = _ocr.MANDATORY_ROE_FIELDS
StubOCRExtractor = _ocr.StubOCRExtractor
low_confidence_indicators = _ocr.low_confidence_indicators
check_hours_rate_earnings = _consistency.check_hours_rate_earnings
check_date_order = _consistency.check_date_order


# ---------------------------------------------------------------------------
# hours * hourly_rate vs earnings boundary (Requirement 7.2)
# ---------------------------------------------------------------------------


def test_hours_rate_earnings_exactly_two_percent_no_flag():
    # expected = 100 * 10.20 = 1020.00 ; earnings = 1000.00 -> deviation 2.00%.
    indicators = check_hours_rate_earnings(
        Decimal("100"), Decimal("10.20"), Decimal("1000.00")
    )
    assert indicators == []


def test_hours_rate_earnings_just_over_two_percent_flags_critical():
    # expected = 100 * 10.21 = 1021.00 ; earnings = 1000.00 -> deviation 2.10%.
    indicators = check_hours_rate_earnings(
        Decimal("100"), Decimal("10.21"), Decimal("1000.00")
    )
    assert len(indicators) == 1
    ind = indicators[0]
    assert ind.severity == Severity.CRITICAL
    assert ind.engine == EngineType.DOCUMENT_FORGERY
    assert ind.algorithm == "hours_rate_earnings_consistency"


def test_hours_rate_earnings_exact_two_point_zero_one_percent_boundary():
    # Construct exactly 2.01% deviation: earnings = 10000, expected = 10201.
    # 201 / 10000 = 0.0201 > 0.02 -> flag. And 200/10000 = 0.0200 -> no flag.
    over = check_hours_rate_earnings(
        Decimal("1"), Decimal("10201"), Decimal("10000")
    )
    assert len(over) == 1
    at = check_hours_rate_earnings(
        Decimal("1"), Decimal("10200"), Decimal("10000")
    )
    assert at == []


def test_hours_rate_earnings_exact_match_no_flag():
    indicators = check_hours_rate_earnings(
        Decimal("40"), Decimal("25.00"), Decimal("1000.00")
    )
    assert indicators == []


# ---------------------------------------------------------------------------
# date order (Requirement 7.2)
# ---------------------------------------------------------------------------


def test_last_day_paid_before_first_day_worked_flags_critical():
    indicators = check_date_order(
        last_day_paid=date(2024, 1, 1), first_day_worked=date(2024, 6, 1)
    )
    assert len(indicators) == 1
    ind = indicators[0]
    assert ind.severity == Severity.CRITICAL
    assert ind.engine == EngineType.DOCUMENT_FORGERY
    assert ind.algorithm == "date_order_consistency"


def test_last_day_paid_same_as_first_day_worked_no_flag():
    indicators = check_date_order(
        last_day_paid=date(2024, 6, 1), first_day_worked=date(2024, 6, 1)
    )
    assert indicators == []


def test_last_day_paid_after_first_day_worked_no_flag():
    indicators = check_date_order(
        last_day_paid=date(2024, 6, 30), first_day_worked=date(2024, 6, 1)
    )
    assert indicators == []


# ---------------------------------------------------------------------------
# OCR extraction confidence boundary (Requirement 7.1)
# ---------------------------------------------------------------------------


def test_confidence_exactly_threshold_no_flag():
    extractor = StubOCRExtractor()
    doc = {
        "document_id": "doc-1",
        "confidences": {name: 0.90 for name in MANDATORY_ROE_FIELDS},
    }
    # Set one field to exactly 0.85 (the threshold).
    doc["confidences"]["employer_name"] = 0.85
    extracted = extractor.extract(doc)
    indicators = low_confidence_indicators(extracted, threshold=0.85)
    assert indicators == []


def test_confidence_just_below_threshold_flags_low_confidence():
    extractor = StubOCRExtractor()
    doc = {
        "document_id": "doc-2",
        "confidences": {name: 0.90 for name in MANDATORY_ROE_FIELDS},
    }
    doc["confidences"]["employer_name"] = 0.84
    extracted = extractor.extract(doc)
    indicators = low_confidence_indicators(extracted, threshold=0.85)
    assert len(indicators) == 1
    ind = indicators[0]
    assert ind.payload["flag"] == "EXTRACTION_LOW_CONFIDENCE"
    assert ind.payload["field"] == "employer_name"
    assert ind.engine == EngineType.DOCUMENT_FORGERY
    assert ind.algorithm == "ocr_extraction_confidence"


def test_multiple_low_confidence_fields_flagged():
    extractor = StubOCRExtractor()
    doc = {
        "document_id": "doc-3",
        "confidences": {name: 0.99 for name in MANDATORY_ROE_FIELDS},
    }
    doc["confidences"]["sin"] = 0.10
    doc["confidences"]["hourly_rate"] = 0.50
    extracted = extractor.extract(doc)
    indicators = low_confidence_indicators(extracted, threshold=0.85)
    flagged_fields = {i.payload["field"] for i in indicators}
    assert flagged_fields == {"sin", "hourly_rate"}


# ---------------------------------------------------------------------------
# Stub extractor returns all 11 fields with confidences
# ---------------------------------------------------------------------------


def test_stub_extractor_returns_all_eleven_fields():
    extractor = StubOCRExtractor()
    extracted = extractor.extract({"document_id": "doc-full"})
    assert len(MANDATORY_ROE_FIELDS) == 11
    # Every mandatory field has both a value and a confidence.
    assert set(extracted.values.keys()) == set(MANDATORY_ROE_FIELDS)
    assert set(extracted.confidences.keys()) == set(MANDATORY_ROE_FIELDS)
    for name in MANDATORY_ROE_FIELDS:
        assert extracted.values[name] != ""
        assert 0.0 <= extracted.confidences[name] <= 1.0
    assert extracted.source == "stub"


def test_stub_extractor_is_deterministic():
    a = StubOCRExtractor().extract({"document_id": "same-seed"})
    b = StubOCRExtractor().extract({"document_id": "same-seed"})
    assert a.values == b.values
    assert a.confidences == b.confidences


def test_stub_clean_document_has_no_low_confidence_flags():
    extracted = StubOCRExtractor().extract({"document_id": "clean"})
    # Default deterministic confidences are in [0.90, 0.99] -> no flags.
    assert low_confidence_indicators(extracted, threshold=0.85) == []
