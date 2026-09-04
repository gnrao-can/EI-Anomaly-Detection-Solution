"""
shared/validation.py
====================
Input validation and normalisation utilities for the EI Anomaly Detection Solution.

Provides:
  - SIN (Social Insurance Number) Luhn checksum validation and normalisation
  - BN (CRA Business Number) format validation and normalisation
  - Canadian postal code format validation
  - Claim field validation returning structured error dicts

Requirements: 1.2, 1.3
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Letters not used as the first character of any Canadian postal code FSA
_INVALID_POSTAL_FIRST_LETTERS: frozenset[str] = frozenset("DFIOQUW")

# Valid BN program identifier codes (two uppercase letters)
_VALID_BN_PROGRAM_CODES: frozenset[str] = frozenset(
    {"RT", "RP", "RC", "RZ", "RU", "RM"}
)

# Pattern for a full Canadian postal code (with or without space): A1A1A1 or A1A 1A1
_POSTAL_CODE_FULL_RE = re.compile(
    r"^([A-Z]\d[A-Z])\s?(\d[A-Z]\d)$",
    re.IGNORECASE,
)

# Pattern for a Forward Sortation Area (FSA) only: A1A
_POSTAL_CODE_FSA_RE = re.compile(r"^[A-Z]\d[A-Z]$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# SIN utilities
# ---------------------------------------------------------------------------


def luhn_checksum(number: str) -> bool:
    """Apply the Luhn algorithm to *number* (digits only).

    Returns ``True`` if the number passes the Luhn check, ``False`` otherwise.
    The input must be a string of digit characters; no spaces or dashes.
    """
    if not number.isdigit():
        return False

    total = 0
    reverse_digits = number[::-1]
    for i, ch in enumerate(reverse_digits):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n

    return total % 10 == 0


def validate_sin(sin: str) -> bool:
    """Validate a Canadian Social Insurance Number.

    Strips spaces and dashes, then checks:
    - Exactly 9 digits
    - Does not start with ``0`` (no Canadian SIN starts with 0)
    - Passes the Luhn checksum

    Returns ``True`` if valid, ``False`` otherwise.
    """
    cleaned = sin.replace(" ", "").replace("-", "")
    if len(cleaned) != 9:
        return False
    if not cleaned.isdigit():
        return False
    if cleaned[0] == "0":
        return False
    return luhn_checksum(cleaned)


def normalize_sin(sin: str) -> str:
    """Normalise a SIN by stripping spaces and dashes.

    Returns a 9-digit string.

    Raises:
        ValueError: with message ``"Invalid SIN: <reason>"`` if the SIN is invalid.
    """
    cleaned = sin.replace(" ", "").replace("-", "")
    if len(cleaned) != 9:
        raise ValueError(
            f"Invalid SIN: expected 9 digits after stripping separators, got {len(cleaned)}"
        )
    if not cleaned.isdigit():
        raise ValueError("Invalid SIN: contains non-numeric characters")
    if cleaned[0] == "0":
        raise ValueError("Invalid SIN: SINs cannot start with 0")
    if not luhn_checksum(cleaned):
        raise ValueError("Invalid SIN: failed Luhn checksum")
    return cleaned


def mask_sin(sin: str) -> str:
    """Return a masked SIN string suitable for logging or display.

    Expects an already-normalised 9-digit string.  Does not validate.
    Returns ``"XXX-XXX-NNN"`` where NNN is the last 3 digits.

    Example::

        >>> mask_sin("123456789")
        'XXX-XXX-789'
    """
    return f"XXX-XXX-{sin[-3:]}"


# ---------------------------------------------------------------------------
# Business Number utilities
# ---------------------------------------------------------------------------


def validate_bn(bn: str) -> bool:
    """Validate a CRA Business Number.

    A valid BN is one of:
    - A 9-digit numeric string (base BN), or
    - A 15-character string: 9 digits + 2 uppercase letters (program identifier)
      + 4 digits (account number).

    The program identifier must be a recognised CRA program code
    (``RT``, ``RP``, ``RC``, ``RZ``, ``RU``, ``RM``).

    Returns ``True`` if valid, ``False`` otherwise.
    """
    stripped = bn.strip()
    if len(stripped) == 9:
        return stripped.isdigit()
    if len(stripped) == 15:
        base = stripped[:9]
        program = stripped[9:11].upper()
        account = stripped[11:]
        return (
            base.isdigit()
            and program in _VALID_BN_PROGRAM_CODES
            and account.isdigit()
        )
    return False


def normalize_bn(bn: str) -> str:
    """Normalise a BN by stripping spaces and uppercasing letters.

    Raises:
        ValueError: if the BN is not valid after normalisation.
    """
    normalised = bn.strip().upper()
    if not validate_bn(normalised):
        raise ValueError(
            f"Invalid BN: '{bn}' — must be a 9-digit base BN or a 15-character "
            "BN (9 digits + 2-letter program code + 4-digit account number)"
        )
    return normalised


# ---------------------------------------------------------------------------
# Postal code utilities
# ---------------------------------------------------------------------------


def validate_postal_code(postal_code: str) -> bool:
    """Validate a Canadian postal code.

    Accepts:
    - FSA format: ``A1A`` (letter-digit-letter)
    - Full postal code: ``A1A 1A1`` or ``A1A1A1`` (with or without space)

    Case-insensitive.  The first letter must not be one of:
    ``D``, ``F``, ``I``, ``O``, ``Q``, ``U``  (not used in Canadian postal codes).

    Returns ``True`` if valid, ``False`` otherwise.
    """
    cleaned = postal_code.strip().upper()

    # Try full postal code first
    full_match = _POSTAL_CODE_FULL_RE.match(cleaned)
    if full_match:
        fsa = full_match.group(1)
        return fsa[0] not in _INVALID_POSTAL_FIRST_LETTERS

    # Try FSA-only
    fsa_match = _POSTAL_CODE_FSA_RE.match(cleaned)
    if fsa_match:
        return cleaned[0] not in _INVALID_POSTAL_FIRST_LETTERS

    return False


# ---------------------------------------------------------------------------
# Claim field validation
# ---------------------------------------------------------------------------


def validate_claim_fields(claim_data: dict[str, Any]) -> list[dict[str, str]]:
    """Validate mandatory fields of a raw claim data dictionary.

    Checks all required claim fields and returns a list of error dictionaries.
    An empty list means all fields are valid.

    Each error dict has the keys:
    - ``field``   — the field name that failed
    - ``code``    — a machine-readable error code (``MISSING``, ``INVALID``, etc.)
    - ``message`` — a human-readable description of the problem
    - ``hint``    — the same human-readable text under the ``hint`` key that the
      SPA reads (mirrors ``message`` so both the ``{field,code,hint}`` design
      contract and the legacy ``message`` consumers are satisfied)

    Mandatory fields (Requirement 1.2) are EXACTLY: ``sin``, ``employer_bn``,
    ``insurable_hours``, ``insurable_earnings``, ``separation_date``,
    ``separation_reason``. ``postal_code`` is **optional** — it is validated for
    format only when a non-empty value is supplied (the ``claims`` schema column
    ``residential_postal_code`` is nullable), and its absence is NOT an error.

    Fields validated:
    - ``sin``                — present, valid format (Luhn), not starting with 0
    - ``employer_bn``        — present, valid BN format
    - ``insurable_hours``    — present, numeric, > 0, ≤ 3120
    - ``insurable_earnings`` — present, numeric, > 0
    - ``separation_date``    — present, valid ISO date (YYYY-MM-DD), not in future
    - ``separation_reason``  — present, non-empty string
    - ``postal_code``        — OPTIONAL; when present, a valid Canadian postal code
    """
    errors: list[dict[str, str]] = []

    # --- sin ---
    sin_value = claim_data.get("sin")
    if sin_value is None or (isinstance(sin_value, str) and sin_value.strip() == ""):
        errors.append(
            {
                "field": "sin",
                "code": "MISSING",
                "message": "SIN is required.",
            }
        )
    else:
        if not validate_sin(str(sin_value)):
            errors.append(
                {
                    "field": "sin",
                    "code": "INVALID",
                    "message": (
                        "SIN must be a 9-digit number that passes the Luhn checksum "
                        "and must not start with 0."
                    ),
                }
            )

    # --- employer_bn ---
    bn_value = claim_data.get("employer_bn")
    if bn_value is None or (isinstance(bn_value, str) and bn_value.strip() == ""):
        errors.append(
            {
                "field": "employer_bn",
                "code": "MISSING",
                "message": "Employer Business Number (BN) is required.",
            }
        )
    else:
        if not validate_bn(str(bn_value)):
            errors.append(
                {
                    "field": "employer_bn",
                    "code": "INVALID",
                    "message": (
                        "Employer BN must be a 9-digit number or a 15-character "
                        "BN (9 digits + program code + 4-digit account number)."
                    ),
                }
            )

    # --- insurable_hours ---
    hours_value = claim_data.get("insurable_hours")
    if hours_value is None:
        errors.append(
            {
                "field": "insurable_hours",
                "code": "MISSING",
                "message": "Insurable hours is required.",
            }
        )
    else:
        try:
            hours_float = float(hours_value)
            if hours_float <= 0:
                errors.append(
                    {
                        "field": "insurable_hours",
                        "code": "INVALID",
                        "message": "Insurable hours must be greater than 0.",
                    }
                )
            elif hours_float > 3120:
                errors.append(
                    {
                        "field": "insurable_hours",
                        "code": "INVALID",
                        "message": (
                            "Insurable hours cannot exceed 3120 "
                            "(maximum 60 hours/week × 52 weeks)."
                        ),
                    }
                )
        except (TypeError, ValueError):
            errors.append(
                {
                    "field": "insurable_hours",
                    "code": "INVALID",
                    "message": "Insurable hours must be a numeric value.",
                }
            )

    # --- insurable_earnings ---
    earnings_value = claim_data.get("insurable_earnings")
    if earnings_value is None:
        errors.append(
            {
                "field": "insurable_earnings",
                "code": "MISSING",
                "message": "Insurable earnings is required.",
            }
        )
    else:
        try:
            earnings_float = float(earnings_value)
            if earnings_float <= 0:
                errors.append(
                    {
                        "field": "insurable_earnings",
                        "code": "INVALID",
                        "message": "Insurable earnings must be greater than 0.",
                    }
                )
        except (TypeError, ValueError):
            errors.append(
                {
                    "field": "insurable_earnings",
                    "code": "INVALID",
                    "message": "Insurable earnings must be a numeric value.",
                }
            )

    # --- separation_date ---
    sep_date_value = claim_data.get("separation_date")
    if sep_date_value is None or (
        isinstance(sep_date_value, str) and sep_date_value.strip() == ""
    ):
        errors.append(
            {
                "field": "separation_date",
                "code": "MISSING",
                "message": "Separation date is required.",
            }
        )
    else:
        parsed_date: date | None = None
        if isinstance(sep_date_value, date):
            parsed_date = sep_date_value
        else:
            try:
                parsed_date = date.fromisoformat(str(sep_date_value).strip())
            except (ValueError, TypeError):
                errors.append(
                    {
                        "field": "separation_date",
                        "code": "INVALID",
                        "message": (
                            "Separation date must be a valid ISO date in YYYY-MM-DD format."
                        ),
                    }
                )

        if parsed_date is not None and parsed_date > date.today():
            errors.append(
                {
                    "field": "separation_date",
                    "code": "INVALID",
                    "message": "Separation date cannot be in the future.",
                }
            )

    # --- separation_reason ---
    reason_value = claim_data.get("separation_reason")
    if reason_value is None or (
        isinstance(reason_value, str) and reason_value.strip() == ""
    ):
        errors.append(
            {
                "field": "separation_reason",
                "code": "MISSING",
                "message": "Separation reason is required and must be a non-empty string.",
            }
        )

    # --- postal_code (OPTIONAL per Req 1.2) ---
    # postal_code is NOT a mandatory field: an absent or blank value is accepted
    # (the schema column residential_postal_code is nullable). Only validate the
    # format when a non-empty value is actually supplied.
    postal_value = claim_data.get("postal_code")
    if postal_value is not None and not (
        isinstance(postal_value, str) and postal_value.strip() == ""
    ):
        if not validate_postal_code(str(postal_value)):
            errors.append(
                {
                    "field": "postal_code",
                    "code": "INVALID",
                    "message": (
                        "Postal code must be a valid Canadian postal code "
                        "(e.g. 'K1A 0A9' or 'V6B')."
                    ),
                }
            )

    # Every error carries a ``hint`` key (the shape the SPA reads) alongside the
    # legacy ``message`` key so both {field,code,hint} consumers and existing
    # {field,code,message} tests are satisfied. hint mirrors message.
    for error in errors:
        if "hint" not in error and "message" in error:
            error["hint"] = error["message"]

    return errors


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "luhn_checksum",
    "validate_sin",
    "normalize_sin",
    "mask_sin",
    "validate_bn",
    "normalize_bn",
    "validate_postal_code",
    "validate_claim_fields",
]
