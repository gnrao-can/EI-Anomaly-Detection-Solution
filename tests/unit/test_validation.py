"""
tests/unit/test_validation.py
==============================
Unit tests for shared/validation.py.

Covers:
  - luhn_checksum
  - validate_sin / normalize_sin / mask_sin
  - validate_bn / normalize_bn
  - validate_postal_code
  - validate_claim_fields

Requirements: 1.2, 1.3
"""

from __future__ import annotations

import pytest
from datetime import date, timedelta

from shared.validation import (
    luhn_checksum,
    mask_sin,
    normalize_bn,
    normalize_sin,
    validate_bn,
    validate_claim_fields,
    validate_postal_code,
    validate_sin,
)


# ===========================================================================
# luhn_checksum
# ===========================================================================


class TestLuhnChecksum:
    """Tests for the raw Luhn algorithm."""

    def test_valid_luhn_number(self):
        # 4532015112830366 is a well-known valid Luhn test number
        assert luhn_checksum("4532015112830366") is True

    def test_single_digit_zero(self):
        # "0" passes Luhn (trivially)
        assert luhn_checksum("0") is True

    def test_invalid_luhn_number(self):
        assert luhn_checksum("1234567890") is False

    def test_non_digit_returns_false(self):
        assert luhn_checksum("123ABC456") is False

    def test_empty_string_returns_false(self):
        # luhn_checksum requires all-digit input; empty string fails the isdigit() guard
        assert luhn_checksum("") is False

    def test_known_valid_sin_luhn(self):
        # 046 454 286 is a canonical test SIN that passes Luhn
        assert luhn_checksum("046454286") is True

    def test_known_invalid_luhn(self):
        assert luhn_checksum("046454287") is False


# ===========================================================================
# validate_sin
# ===========================================================================


class TestValidateSin:
    """Tests for validate_sin()."""

    # --- Valid SINs ---
    def test_valid_sin_no_separators(self):
        # 130692544 is a Luhn-valid SIN that does not start with 0
        assert validate_sin("130692544") is True

    def test_valid_sin_with_spaces(self):
        assert validate_sin("130 692 544") is True

    def test_valid_sin_with_dashes(self):
        assert validate_sin("130-692-544") is True

    def test_valid_sin_mixed_separators(self):
        assert validate_sin("130 692-544") is True

    # --- Invalid SINs ---
    def test_invalid_sin_wrong_length_short(self):
        assert validate_sin("12345678") is False

    def test_invalid_sin_wrong_length_long(self):
        assert validate_sin("1234567890") is False

    def test_invalid_sin_non_numeric(self):
        assert validate_sin("12345678A") is False

    def test_invalid_sin_failed_luhn(self):
        # 046454287 is one digit off from the valid test SIN → fails Luhn
        assert validate_sin("046454287") is False

    def test_invalid_sin_starts_with_zero(self):
        # 046454286 passes Luhn but starts with 0 — must be rejected by validate_sin
        assert validate_sin("046454286") is False

    def test_invalid_sin_all_zeros(self):
        assert validate_sin("000000000") is False

    def test_invalid_sin_empty_string(self):
        assert validate_sin("") is False

    def test_sin_starts_with_zero_invalid_regardless_of_luhn(self):
        # 046454286 is Luhn-valid but starts with 0 — the spec says SINs starting with 0
        # are invalid. Note: the function returns True for this because 0 is stripped
        # by the leading-zero check only when the first digit IS 0.
        # Per spec: "SINs starting with 0 are invalid (no Canadian SIN starts with 0)"
        result = validate_sin("046454286")
        # 046454286[0] == '0' → should be False per spec
        assert result is False


# ===========================================================================
# normalize_sin
# ===========================================================================


class TestNormalizeSin:
    """Tests for normalize_sin()."""

    def test_normalize_sin_with_spaces(self):
        # Use a SIN that does NOT start with 0 and is Luhn-valid
        # 130 692 544 is a commonly cited Luhn-valid test SIN not starting with 0
        result = normalize_sin("130 692 544")
        assert result == "130692544"

    def test_normalize_sin_with_dashes(self):
        result = normalize_sin("130-692-544")
        assert result == "130692544"

    def test_normalize_sin_plain(self):
        result = normalize_sin("130692544")
        assert result == "130692544"

    def test_normalize_sin_wrong_length_raises(self):
        with pytest.raises(ValueError, match="Invalid SIN"):
            normalize_sin("12345678")

    def test_normalize_sin_non_numeric_raises(self):
        with pytest.raises(ValueError, match="Invalid SIN"):
            normalize_sin("12345678A")

    def test_normalize_sin_starts_with_zero_raises(self):
        with pytest.raises(ValueError, match="Invalid SIN"):
            normalize_sin("046454286")

    def test_normalize_sin_failed_luhn_raises(self):
        with pytest.raises(ValueError, match="Invalid SIN"):
            normalize_sin("130692545")  # off by one → Luhn fails

    def test_normalize_sin_returns_nine_digit_string(self):
        result = normalize_sin("130-692-544")
        assert len(result) == 9
        assert result.isdigit()


# ===========================================================================
# mask_sin
# ===========================================================================


class TestMaskSin:
    """Tests for mask_sin()."""

    def test_mask_sin_basic(self):
        assert mask_sin("123456789") == "XXX-XXX-789"

    def test_mask_sin_last_three_preserved(self):
        assert mask_sin("987654321") == "XXX-XXX-321"

    def test_mask_sin_format(self):
        result = mask_sin("130692544")
        assert result == "XXX-XXX-544"
        assert result.startswith("XXX-XXX-")

    def test_mask_sin_does_not_validate(self):
        # mask_sin works on any 9-digit string; no validation expected
        result = mask_sin("000000000")
        assert result == "XXX-XXX-000"


# ===========================================================================
# validate_bn
# ===========================================================================


class TestValidateBn:
    """Tests for validate_bn()."""

    # --- Valid BNs ---
    def test_valid_9_digit_bn(self):
        assert validate_bn("123456789") is True

    def test_valid_15_char_bn_rt(self):
        assert validate_bn("123456789RT0001") is True

    def test_valid_15_char_bn_rp(self):
        assert validate_bn("123456789RP0001") is True

    def test_valid_15_char_bn_rc(self):
        assert validate_bn("123456789RC0001") is True

    def test_valid_15_char_bn_rz(self):
        assert validate_bn("123456789RZ0001") is True

    def test_valid_bn_with_leading_trailing_spaces(self):
        # strip() is applied in validate_bn
        assert validate_bn("  123456789  ") is True

    # --- Invalid BNs ---
    def test_invalid_bn_wrong_length_8_digits(self):
        assert validate_bn("12345678") is False

    def test_invalid_bn_wrong_length_10_digits(self):
        assert validate_bn("1234567890") is False

    def test_invalid_bn_14_chars(self):
        assert validate_bn("123456789RT001") is False

    def test_invalid_bn_16_chars(self):
        assert validate_bn("123456789RT00011") is False

    def test_invalid_bn_non_numeric_base(self):
        assert validate_bn("12345678A") is False

    def test_invalid_bn_invalid_program_code_aa(self):
        # "AA" is not a recognised program code
        assert validate_bn("123456789AA0001") is False

    def test_invalid_bn_invalid_program_code_numeric(self):
        assert validate_bn("123456789120001") is False

    def test_invalid_bn_account_non_numeric(self):
        assert validate_bn("123456789RTABCD") is False

    def test_invalid_bn_empty_string(self):
        assert validate_bn("") is False


# ===========================================================================
# normalize_bn
# ===========================================================================


class TestNormalizeBn:
    """Tests for normalize_bn()."""

    def test_normalize_bn_uppercases_program_code(self):
        result = normalize_bn("123456789rt0001")
        assert result == "123456789RT0001"

    def test_normalize_bn_strips_spaces(self):
        result = normalize_bn("  123456789  ")
        assert result == "123456789"

    def test_normalize_bn_valid_15_char(self):
        result = normalize_bn("123456789RT0001")
        assert result == "123456789RT0001"

    def test_normalize_bn_invalid_raises(self):
        with pytest.raises(ValueError, match="Invalid BN"):
            normalize_bn("12345678")

    def test_normalize_bn_invalid_program_code_raises(self):
        with pytest.raises(ValueError, match="Invalid BN"):
            normalize_bn("123456789XX0001")


# ===========================================================================
# validate_postal_code
# ===========================================================================


class TestValidatePostalCode:
    """Tests for validate_postal_code()."""

    # --- Valid postal codes ---
    def test_valid_full_with_space(self):
        assert validate_postal_code("K1A 0A9") is True

    def test_valid_full_without_space(self):
        assert validate_postal_code("K1A0A9") is True

    def test_valid_full_lowercase(self):
        assert validate_postal_code("k1a0a9") is True

    def test_valid_full_mixed_case(self):
        assert validate_postal_code("k1A 0a9") is True

    def test_valid_full_vancouver(self):
        assert validate_postal_code("V6B 1S5") is True

    def test_valid_full_toronto(self):
        assert validate_postal_code("M5V3L9") is True

    def test_valid_fsa_only(self):
        assert validate_postal_code("V6B") is True

    def test_valid_fsa_lowercase(self):
        assert validate_postal_code("v6b") is True

    def test_valid_fsa_k1a(self):
        assert validate_postal_code("K1A") is True

    # --- Invalid: first letter not allowed ---
    def test_invalid_first_letter_d(self):
        assert validate_postal_code("D1A 0A9") is False

    def test_invalid_first_letter_f(self):
        assert validate_postal_code("F1A 0A9") is False

    def test_invalid_first_letter_i(self):
        assert validate_postal_code("I1A 0A9") is False

    def test_invalid_first_letter_o(self):
        assert validate_postal_code("O1A 0A9") is False

    def test_invalid_first_letter_q(self):
        assert validate_postal_code("Q1A 0A9") is False

    def test_invalid_first_letter_u(self):
        assert validate_postal_code("U1A 0A9") is False

    def test_invalid_fsa_first_letter_d(self):
        assert validate_postal_code("D1A") is False

    # --- Invalid: wrong format ---
    def test_invalid_format_all_digits(self):
        assert validate_postal_code("123456") is False

    def test_invalid_format_too_short(self):
        assert validate_postal_code("K1") is False

    def test_invalid_format_too_long(self):
        assert validate_postal_code("K1A 0A99") is False

    def test_invalid_empty_string(self):
        assert validate_postal_code("") is False

    def test_invalid_us_zip_code(self):
        assert validate_postal_code("10001") is False


# ===========================================================================
# validate_claim_fields
# ===========================================================================


def _valid_claim() -> dict:
    """Return a minimal fully-valid claim data dict."""
    return {
        "sin": "130692544",
        "employer_bn": "123456789RT0001",
        "insurable_hours": 800,
        "insurable_earnings": 25000.00,
        "separation_date": str(date.today() - timedelta(days=30)),
        "separation_reason": "Shortage of work",
        "postal_code": "K1A 0A9",
    }


class TestValidateClaimFields:
    """Tests for validate_claim_fields()."""

    # --- All-valid dict returns empty list ---
    def test_all_valid_returns_empty_list(self):
        assert validate_claim_fields(_valid_claim()) == []

    # --- SIN errors ---
    def test_missing_sin_returns_error(self):
        data = _valid_claim()
        del data["sin"]
        errors = validate_claim_fields(data)
        fields = [e["field"] for e in errors]
        assert "sin" in fields
        codes = [e["code"] for e in errors if e["field"] == "sin"]
        assert "MISSING" in codes

    def test_empty_sin_returns_missing_error(self):
        data = _valid_claim()
        data["sin"] = ""
        errors = validate_claim_fields(data)
        codes = [e["code"] for e in errors if e["field"] == "sin"]
        assert "MISSING" in codes

    def test_invalid_sin_luhn_returns_error(self):
        data = _valid_claim()
        data["sin"] = "130692545"  # off by one → fails Luhn
        errors = validate_claim_fields(data)
        codes = [e["code"] for e in errors if e["field"] == "sin"]
        assert "INVALID" in codes

    def test_sin_starts_with_zero_returns_error(self):
        data = _valid_claim()
        data["sin"] = "046454286"  # starts with 0
        errors = validate_claim_fields(data)
        codes = [e["code"] for e in errors if e["field"] == "sin"]
        assert "INVALID" in codes

    def test_sin_wrong_length_returns_error(self):
        data = _valid_claim()
        data["sin"] = "12345"
        errors = validate_claim_fields(data)
        codes = [e["code"] for e in errors if e["field"] == "sin"]
        assert "INVALID" in codes

    # --- Employer BN errors ---
    def test_missing_employer_bn_returns_error(self):
        data = _valid_claim()
        del data["employer_bn"]
        errors = validate_claim_fields(data)
        codes = [e["code"] for e in errors if e["field"] == "employer_bn"]
        assert "MISSING" in codes

    def test_invalid_employer_bn_returns_error(self):
        data = _valid_claim()
        data["employer_bn"] = "NOTABN"
        errors = validate_claim_fields(data)
        codes = [e["code"] for e in errors if e["field"] == "employer_bn"]
        assert "INVALID" in codes

    def test_empty_employer_bn_returns_missing_error(self):
        data = _valid_claim()
        data["employer_bn"] = "  "
        errors = validate_claim_fields(data)
        codes = [e["code"] for e in errors if e["field"] == "employer_bn"]
        assert "MISSING" in codes

    # --- Insurable hours errors ---
    def test_missing_insurable_hours_returns_error(self):
        data = _valid_claim()
        del data["insurable_hours"]
        errors = validate_claim_fields(data)
        codes = [e["code"] for e in errors if e["field"] == "insurable_hours"]
        assert "MISSING" in codes

    def test_insurable_hours_zero_returns_error(self):
        data = _valid_claim()
        data["insurable_hours"] = 0
        errors = validate_claim_fields(data)
        codes = [e["code"] for e in errors if e["field"] == "insurable_hours"]
        assert "INVALID" in codes

    def test_insurable_hours_negative_returns_error(self):
        data = _valid_claim()
        data["insurable_hours"] = -1
        errors = validate_claim_fields(data)
        codes = [e["code"] for e in errors if e["field"] == "insurable_hours"]
        assert "INVALID" in codes

    def test_insurable_hours_exceeds_max_returns_error(self):
        data = _valid_claim()
        data["insurable_hours"] = 3121
        errors = validate_claim_fields(data)
        codes = [e["code"] for e in errors if e["field"] == "insurable_hours"]
        assert "INVALID" in codes

    def test_insurable_hours_at_max_boundary_is_valid(self):
        data = _valid_claim()
        data["insurable_hours"] = 3120
        errors = validate_claim_fields(data)
        assert not any(e["field"] == "insurable_hours" for e in errors)

    def test_insurable_hours_non_numeric_returns_error(self):
        data = _valid_claim()
        data["insurable_hours"] = "many"
        errors = validate_claim_fields(data)
        codes = [e["code"] for e in errors if e["field"] == "insurable_hours"]
        assert "INVALID" in codes

    # --- Insurable earnings errors ---
    def test_missing_insurable_earnings_returns_error(self):
        data = _valid_claim()
        del data["insurable_earnings"]
        errors = validate_claim_fields(data)
        codes = [e["code"] for e in errors if e["field"] == "insurable_earnings"]
        assert "MISSING" in codes

    def test_insurable_earnings_zero_returns_error(self):
        data = _valid_claim()
        data["insurable_earnings"] = 0
        errors = validate_claim_fields(data)
        codes = [e["code"] for e in errors if e["field"] == "insurable_earnings"]
        assert "INVALID" in codes

    def test_insurable_earnings_negative_returns_error(self):
        data = _valid_claim()
        data["insurable_earnings"] = -500
        errors = validate_claim_fields(data)
        codes = [e["code"] for e in errors if e["field"] == "insurable_earnings"]
        assert "INVALID" in codes

    def test_insurable_earnings_non_numeric_returns_error(self):
        data = _valid_claim()
        data["insurable_earnings"] = "lots"
        errors = validate_claim_fields(data)
        codes = [e["code"] for e in errors if e["field"] == "insurable_earnings"]
        assert "INVALID" in codes

    # --- Separation date errors ---
    def test_missing_separation_date_returns_error(self):
        data = _valid_claim()
        del data["separation_date"]
        errors = validate_claim_fields(data)
        codes = [e["code"] for e in errors if e["field"] == "separation_date"]
        assert "MISSING" in codes

    def test_separation_date_in_future_returns_error(self):
        data = _valid_claim()
        data["separation_date"] = str(date.today() + timedelta(days=1))
        errors = validate_claim_fields(data)
        codes = [e["code"] for e in errors if e["field"] == "separation_date"]
        assert "INVALID" in codes

    def test_separation_date_today_is_valid(self):
        data = _valid_claim()
        data["separation_date"] = str(date.today())
        errors = validate_claim_fields(data)
        assert not any(e["field"] == "separation_date" for e in errors)

    def test_separation_date_invalid_format_returns_error(self):
        data = _valid_claim()
        data["separation_date"] = "not-a-date"
        errors = validate_claim_fields(data)
        codes = [e["code"] for e in errors if e["field"] == "separation_date"]
        assert "INVALID" in codes

    def test_separation_date_as_date_object_is_valid(self):
        data = _valid_claim()
        data["separation_date"] = date.today() - timedelta(days=10)
        assert validate_claim_fields(data) == []

    def test_separation_date_as_date_object_future_returns_error(self):
        data = _valid_claim()
        data["separation_date"] = date.today() + timedelta(days=1)
        errors = validate_claim_fields(data)
        codes = [e["code"] for e in errors if e["field"] == "separation_date"]
        assert "INVALID" in codes

    # --- Separation reason errors ---
    def test_missing_separation_reason_returns_error(self):
        data = _valid_claim()
        del data["separation_reason"]
        errors = validate_claim_fields(data)
        codes = [e["code"] for e in errors if e["field"] == "separation_reason"]
        assert "MISSING" in codes

    def test_empty_separation_reason_returns_error(self):
        data = _valid_claim()
        data["separation_reason"] = "   "
        errors = validate_claim_fields(data)
        codes = [e["code"] for e in errors if e["field"] == "separation_reason"]
        assert "MISSING" in codes

    # --- Postal code errors ---
    def test_missing_postal_code_is_accepted(self):
        # postal_code is OPTIONAL per Req 1.2 (the schema column is nullable):
        # an absent postal_code must NOT produce a validation error.
        data = _valid_claim()
        del data["postal_code"]
        errors = validate_claim_fields(data)
        postal_errors = [e for e in errors if e["field"] == "postal_code"]
        assert postal_errors == []

    def test_invalid_postal_code_returns_error(self):
        data = _valid_claim()
        data["postal_code"] = "D1A 0A9"  # D is invalid first letter
        errors = validate_claim_fields(data)
        codes = [e["code"] for e in errors if e["field"] == "postal_code"]
        assert "INVALID" in codes

    def test_malformed_postal_code_returns_error(self):
        data = _valid_claim()
        data["postal_code"] = "99999"
        errors = validate_claim_fields(data)
        codes = [e["code"] for e in errors if e["field"] == "postal_code"]
        assert "INVALID" in codes

    # --- Multiple simultaneous errors ---
    def test_multiple_missing_fields_return_multiple_errors(self):
        data = {}
        errors = validate_claim_fields(data)
        error_fields = {e["field"] for e in errors}
        expected_fields = {
            "sin",
            "employer_bn",
            "insurable_hours",
            "insurable_earnings",
            "separation_date",
            "separation_reason",
        }  # postal_code is OPTIONAL per Req 1.2 — not reported when absent
        assert expected_fields == error_fields

    def test_each_error_has_required_keys(self):
        data = {}
        errors = validate_claim_fields(data)
        for error in errors:
            assert "field" in error
            assert "code" in error
            assert "message" in error

    # --- Edge cases ---
    def test_insurable_hours_as_string_numeric_is_valid(self):
        data = _valid_claim()
        data["insurable_hours"] = "800"
        assert validate_claim_fields(data) == []

    def test_insurable_earnings_as_string_numeric_is_valid(self):
        data = _valid_claim()
        data["insurable_earnings"] = "25000.00"
        assert validate_claim_fields(data) == []

    def test_valid_claim_9_digit_bn_is_accepted(self):
        data = _valid_claim()
        data["employer_bn"] = "123456789"
        assert validate_claim_fields(data) == []

    def test_valid_claim_fsa_postal_code_is_accepted(self):
        data = _valid_claim()
        data["postal_code"] = "V6B"
        assert validate_claim_fields(data) == []
