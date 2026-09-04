"""
services/document-forgery-engine/roe_canonical.py
=================================================
Canonical ROE render / parse round-trip helpers (design "5. Document Forgery
Engine" -> "OCR round-trip fidelity (P6)").

The design specifies an OCR round-trip fidelity invariant (Requirement 7.13):

    parse_from_canonical(render_to_canonical(parse(D)))

must reproduce a structured field set with

  * EXACT value matches for every numeric field and every date field, and
  * at most 1 character (Levenshtein) difference per free-text field of
    20 or more characters.

This module provides the three design artefacts that the invariant is stated
over — the :class:`ROEFields` structured type, :func:`render_to_canonical`
(deterministic template rendering) and :func:`parse_from_canonical`
(regex-based extraction) — for the exact 11 mandatory ROE fields enumerated by
``ocr.MANDATORY_ROE_FIELDS``.

Field classification (design step 1 field list):

  Numerics (formatted to 2 dp, matched exactly)
      total_insurable_hours, total_insurable_earnings, hourly_rate
  Dates (formatted YYYY-MM-DD, matched exactly)
      first_day_worked, last_day_paid
  Free-text (stripped + whitespace-normalized, matched within tolerance)
      employer_name, employer_bn, employee_name, sin, occupation,
      reason_for_separation

The canonical text is a line-per-field ``Label: value`` template. Rendering is
deterministic and parsing is the exact inverse for numerics/dates; for free-text
the only lossy step is whitespace normalisation (collapsing internal runs of
whitespace to single spaces and stripping the ends), which the round-trip
invariant tolerates for fields of >= 20 characters via a <= 1 Levenshtein bound.

Requirements: 7.13
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from ocr import MANDATORY_ROE_FIELDS

# ---------------------------------------------------------------------------
# Field classification (over the canonical 11 mandatory ROE fields).
# ---------------------------------------------------------------------------

NUMERIC_FIELDS: tuple[str, ...] = (
    "total_insurable_hours",
    "total_insurable_earnings",
    "hourly_rate",
)

DATE_FIELDS: tuple[str, ...] = (
    "first_day_worked",
    "last_day_paid",
)

FREE_TEXT_FIELDS: tuple[str, ...] = (
    "employer_name",
    "employer_bn",
    "employee_name",
    "sin",
    "occupation",
    "reason_for_separation",
)

# Sanity check: the classification must cover exactly the 11 mandatory fields.
assert set(NUMERIC_FIELDS) | set(DATE_FIELDS) | set(FREE_TEXT_FIELDS) == set(
    MANDATORY_ROE_FIELDS
), "ROE field classification must partition the 11 mandatory ROE fields"

#: Human-readable label used for each field in the canonical text. Ordering
#: mirrors ``MANDATORY_ROE_FIELDS`` so the rendered document is stable.
FIELD_LABELS: dict[str, str] = {
    "employer_name": "Employer Name",
    "employer_bn": "Employer BN",
    "employee_name": "Employee Name",
    "sin": "SIN",
    "occupation": "Occupation",
    "first_day_worked": "First Day Worked",
    "last_day_paid": "Last Day Paid",
    "total_insurable_hours": "Total Insurable Hours",
    "total_insurable_earnings": "Total Insurable Earnings",
    "hourly_rate": "Hourly Rate",
    "reason_for_separation": "Reason For Separation",
}


@dataclass
class ROEFields:
    """Structured, typed representation of the 11 mandatory ROE fields.

    Numerics are exact :class:`~decimal.Decimal` values, dates are
    :class:`datetime.date`, and free-text fields are strings. Instances are the
    unit over which the P6 round-trip invariant is stated.
    """

    employer_name: str
    employer_bn: str
    employee_name: str
    sin: str
    occupation: str
    first_day_worked: date
    last_day_paid: date
    total_insurable_hours: Decimal
    total_insurable_earnings: Decimal
    hourly_rate: Decimal
    reason_for_separation: str


# ---------------------------------------------------------------------------
# Formatting helpers (render side)
# ---------------------------------------------------------------------------

_WHITESPACE_RUN = re.compile(r"\s+")


def _normalize_text(value: str) -> str:
    """Collapse internal whitespace runs to single spaces and strip the ends.

    This is the ONLY lossy transformation applied to free-text fields; the
    round-trip invariant tolerates it for fields of >= 20 characters.
    """
    return _WHITESPACE_RUN.sub(" ", value).strip()


def _format_numeric(value: Decimal) -> str:
    """Format a numeric field to a fixed 2-decimal-place canonical string."""
    return f"{value.quantize(Decimal('0.01'))}"


def _format_date(value: date) -> str:
    """Format a date field as ISO ``YYYY-MM-DD``."""
    return value.isoformat()


# ---------------------------------------------------------------------------
# render_to_canonical / parse_from_canonical (design P6 artefacts)
# ---------------------------------------------------------------------------


def render_to_canonical(fields: ROEFields) -> str:
    """Render an :class:`ROEFields` to the deterministic canonical ROE text.

    One ``Label: value`` line per field, in the canonical
    ``MANDATORY_ROE_FIELDS`` order. Numerics are formatted to 2 dp, dates to
    ``YYYY-MM-DD``, and free-text is whitespace-normalized.
    """
    lines: list[str] = []
    for name in MANDATORY_ROE_FIELDS:
        label = FIELD_LABELS[name]
        raw = getattr(fields, name)
        if name in NUMERIC_FIELDS:
            rendered = _format_numeric(raw)
        elif name in DATE_FIELDS:
            rendered = _format_date(raw)
        else:
            rendered = _normalize_text(str(raw))
        lines.append(f"{label}: {rendered}")
    return "\n".join(lines)


def parse_from_canonical(text: str) -> ROEFields:
    """Parse canonical ROE text back into a typed :class:`ROEFields`.

    The inverse of :func:`render_to_canonical`: numerics parse to exact
    ``Decimal`` and dates to ``date`` (so those fields round-trip exactly);
    free-text is taken verbatim after the label (already whitespace-normalized
    by the render step).
    """
    extracted: dict[str, str] = {}
    for name in MANDATORY_ROE_FIELDS:
        label = re.escape(FIELD_LABELS[name])
        # Capture everything after "Label:" up to end-of-line.
        match = re.search(rf"^{label}:[ ]?(.*)$", text, re.MULTILINE)
        extracted[name] = match.group(1) if match else ""

    return ROEFields(
        employer_name=extracted["employer_name"],
        employer_bn=extracted["employer_bn"],
        employee_name=extracted["employee_name"],
        sin=extracted["sin"],
        occupation=extracted["occupation"],
        first_day_worked=date.fromisoformat(extracted["first_day_worked"]),
        last_day_paid=date.fromisoformat(extracted["last_day_paid"]),
        total_insurable_hours=Decimal(extracted["total_insurable_hours"]),
        total_insurable_earnings=Decimal(extracted["total_insurable_earnings"]),
        hourly_rate=Decimal(extracted["hourly_rate"]),
        reason_for_separation=extracted["reason_for_separation"],
    )


def levenshtein(a: str, b: str) -> int:
    """Return the Levenshtein (edit) distance between two strings.

    Standard dynamic-programming implementation over insert/delete/substitute
    operations; used by the P6 property to bound free-text drift.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            insert_cost = current[j - 1] + 1
            delete_cost = previous[j] + 1
            substitute_cost = previous[j - 1] + (ca != cb)
            current.append(min(insert_cost, delete_cost, substitute_cost))
        previous = current
    return previous[-1]


__all__ = [
    "ROEFields",
    "NUMERIC_FIELDS",
    "DATE_FIELDS",
    "FREE_TEXT_FIELDS",
    "FIELD_LABELS",
    "render_to_canonical",
    "parse_from_canonical",
    "levenshtein",
]
