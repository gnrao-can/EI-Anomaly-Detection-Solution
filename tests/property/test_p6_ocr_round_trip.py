"""
tests/property/test_p6_ocr_round_trip.py
========================================
Property 6: OCR round-trip fidelity. Validates: Requirements 7.13

Statement
---------
*For any* :class:`ROEFields` value ``fields`` produced from an ROE document,

    restored = parse_from_canonical(render_to_canonical(fields))

reproduces the structured field set such that

  * every NUMERIC field (total_insurable_hours, total_insurable_earnings,
    hourly_rate) matches EXACTLY (``Decimal`` equality at 2 dp canonical form);
  * every DATE field (first_day_worked, last_day_paid) matches EXACTLY; and
  * every FREE-TEXT field (employer_name, employer_bn, employee_name, sin,
    occupation, reason_for_separation) of >= 20 characters is within a
    Levenshtein distance of <= 1 of the original.

This mirrors the design's OCR round-trip fidelity invariant (design "5.
Document Forgery Engine" -> "OCR round-trip fidelity (P6)") and Requirement
7.13.

Generators
----------
The free-text generator constrains to the valid canonical input space: single
line values (no embedded newlines, which are the record separator) whose
whitespace is already normalized (no leading/trailing spaces, no internal
whitespace runs). Rendering only ever *normalizes* free-text, so
already-normalized text round-trips with distance 0 — comfortably inside the
<= 1 tolerance the property allows for >= 20-character fields. Numerics are
generated with up to 2 fractional places (the canonical precision) and dates
across a wide realistic ROE range.

Module loading
--------------
The ``services/document-forgery-engine`` directory is hyphenated (not an
importable package) and its modules import each other by bare name
(``roe_canonical`` imports ``from ocr import MANDATORY_ROE_FIELDS``). We load the
``roe_canonical`` module through :func:`tests.conftest.load_service_module`,
which registers it (and its ``ocr`` sibling) under collision-free namespaced
keys so running the whole suite in one process stays clean.

Property 6: OCR round-trip fidelity. Validates: Requirements 7.13
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.conftest import load_service_module

# ---------------------------------------------------------------------------
# Load the hyphenated document-forgery-engine round-trip module (collision-free).
# ``roe_canonical`` imports its ``ocr`` sibling by bare name, so both are
# registered as siblings during load.
# ---------------------------------------------------------------------------
_roe_canonical = load_service_module(
    "document-forgery-engine", "roe_canonical", siblings=["ocr", "roe_canonical"]
)
ROEFields = _roe_canonical.ROEFields
render_to_canonical = _roe_canonical.render_to_canonical
parse_from_canonical = _roe_canonical.parse_from_canonical
levenshtein = _roe_canonical.levenshtein
NUMERIC_FIELDS = _roe_canonical.NUMERIC_FIELDS
DATE_FIELDS = _roe_canonical.DATE_FIELDS
FREE_TEXT_FIELDS = _roe_canonical.FREE_TEXT_FIELDS


# ---------------------------------------------------------------------------
# Hypothesis profile: CI runs 200 examples (design.md — Testing Strategy;
# tasks.md Notes). Registration is guarded so a duplicate registration from a
# sibling property-test module already loaded in the same session does not
# raise; we then always (re)load the "ci" profile so this file runs 200
# examples.
# ---------------------------------------------------------------------------
try:
    settings.register_profile(
        "ci", max_examples=200, suppress_health_check=[HealthCheck.too_slow]
    )
except Exception:  # noqa: BLE001 - already registered elsewhere
    pass
settings.load_profile("ci")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Free-text values constrained to the canonical (already-normalized) space:
#   * no newlines (newline is the canonical record separator),
#   * no leading/trailing whitespace and no internal whitespace runs,
#   * no ASCII control characters.
# Text is drawn both short (< 20 chars) and long (>= 20 chars) so both branches
# of the property (untested-below-20 / tolerance-at-or-above-20) are exercised.
_visible_char = st.characters(
    min_codepoint=0x21,  # exclude space and control chars; we add single spaces below
    max_codepoint=0x2FFF,
    blacklist_categories=("Cs", "Cc", "Zl", "Zp", "Zs"),
)


@st.composite
def free_text_strategy(draw) -> str:
    """Generate normalized single-line free text (words joined by single spaces)."""
    words = draw(
        st.lists(
            st.text(alphabet=_visible_char, min_size=1, max_size=12),
            min_size=1,
            max_size=8,
        )
    )
    value = " ".join(words)
    # Words are non-empty visible-char runs joined by single spaces, so the
    # value is already normalized (no leading/trailing/double whitespace).
    return value


@st.composite
def long_free_text_strategy(draw) -> str:
    """Generate normalized free text guaranteed to be >= 20 characters."""
    value = draw(free_text_strategy())
    while len(value) < 20:
        value = value + " " + draw(st.text(alphabet=_visible_char, min_size=1, max_size=12))
    return value


# Numerics with up to 2 fractional places (the canonical precision), non-negative
# and bounded to a realistic ROE range so no overflow/precision surprises.
numeric_strategy = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("1000000"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)

date_strategy = st.dates(min_value=date(1990, 1, 1), max_value=date(2035, 12, 31))


@st.composite
def roe_fields_strategy(draw) -> ROEFields:
    """Generate a valid :class:`ROEFields` across the canonical input space."""
    return ROEFields(
        employer_name=draw(free_text_strategy()),
        employer_bn=draw(free_text_strategy()),
        employee_name=draw(free_text_strategy()),
        sin=draw(free_text_strategy()),
        occupation=draw(free_text_strategy()),
        first_day_worked=draw(date_strategy),
        last_day_paid=draw(date_strategy),
        total_insurable_hours=draw(numeric_strategy),
        total_insurable_earnings=draw(numeric_strategy),
        hourly_rate=draw(numeric_strategy),
        reason_for_separation=draw(free_text_strategy()),
    )


# ---------------------------------------------------------------------------
# Core P6 assertion — reused across the property tests below.
# ---------------------------------------------------------------------------


def _assert_round_trip_fidelity(fields: ROEFields) -> None:
    """Assert numerics/dates match exactly and free-text >= 20 chars is <= 1 edit."""
    restored = parse_from_canonical(render_to_canonical(fields))

    # Numerics: exact value match (Decimal equality at canonical 2 dp).
    for name in NUMERIC_FIELDS:
        original = getattr(fields, name).quantize(Decimal("0.01"))
        assert getattr(restored, name) == original, (
            f"numeric field {name!r} changed: {original} -> {getattr(restored, name)}"
        )

    # Dates: exact match.
    for name in DATE_FIELDS:
        assert getattr(restored, name) == getattr(fields, name), (
            f"date field {name!r} changed"
        )

    # Free-text: fields of >= 20 chars must be within Levenshtein distance 1.
    for name in FREE_TEXT_FIELDS:
        original = getattr(fields, name)
        got = getattr(restored, name)
        if len(original) >= 20:
            distance = levenshtein(original, got)
            assert distance <= 1, (
                f"free-text field {name!r} (len {len(original)}) drifted by "
                f"{distance} > 1: {original!r} -> {got!r}"
            )


# ===========================================================================
# Property 6 — generated across the full canonical input space.
# ===========================================================================


@given(fields=roe_fields_strategy())
def test_P6_round_trip_preserves_numerics_dates_and_free_text(fields):
    """Property 6: OCR round-trip fidelity. Validates: Requirements 7.13

    parse_from_canonical(render_to_canonical(fields)) matches numerics and dates
    exactly and stays within 1 Levenshtein edit on free-text of >= 20 chars.
    """
    _assert_round_trip_fidelity(fields)


@given(
    hours=numeric_strategy,
    earnings=numeric_strategy,
    rate=numeric_strategy,
)
def test_P6_numeric_fields_match_exactly(hours, earnings, rate):
    """Property 6: OCR round-trip fidelity. Validates: Requirements 7.13

    Every numeric ROE field round-trips to the exact canonical (2 dp) Decimal.
    """
    fields = ROEFields(
        employer_name="Acme",
        employer_bn="123456789RP0001",
        employee_name="Jane Doe",
        sin="046454286",
        occupation="Welder",
        first_day_worked=date(2023, 1, 3),
        last_day_paid=date(2023, 6, 30),
        total_insurable_hours=hours,
        total_insurable_earnings=earnings,
        hourly_rate=rate,
        reason_for_separation="Shortage of work",
    )
    restored = parse_from_canonical(render_to_canonical(fields))
    assert restored.total_insurable_hours == hours.quantize(Decimal("0.01"))
    assert restored.total_insurable_earnings == earnings.quantize(Decimal("0.01"))
    assert restored.hourly_rate == rate.quantize(Decimal("0.01"))


@given(first=date_strategy, last=date_strategy)
def test_P6_date_fields_match_exactly(first, last):
    """Property 6: OCR round-trip fidelity. Validates: Requirements 7.13

    Every date ROE field round-trips exactly through the YYYY-MM-DD canonical
    form.
    """
    fields = ROEFields(
        employer_name="Acme",
        employer_bn="123456789RP0001",
        employee_name="Jane Doe",
        sin="046454286",
        occupation="Welder",
        first_day_worked=first,
        last_day_paid=last,
        total_insurable_hours=Decimal("1200.00"),
        total_insurable_earnings=Decimal("36000.00"),
        hourly_rate=Decimal("30.00"),
        reason_for_separation="Shortage of work",
    )
    restored = parse_from_canonical(render_to_canonical(fields))
    assert restored.first_day_worked == first
    assert restored.last_day_paid == last


@given(text=long_free_text_strategy())
def test_P6_long_free_text_within_one_edit(text):
    """Property 6: OCR round-trip fidelity. Validates: Requirements 7.13

    A normalized free-text field of >= 20 characters round-trips within a
    Levenshtein distance of 1 (in fact exactly, since canonical text is stable).
    """
    fields = ROEFields(
        employer_name="Acme",
        employer_bn="123456789RP0001",
        employee_name="Jane Doe",
        sin="046454286",
        occupation="Welder",
        first_day_worked=date(2023, 1, 3),
        last_day_paid=date(2023, 6, 30),
        total_insurable_hours=Decimal("1200.00"),
        total_insurable_earnings=Decimal("36000.00"),
        hourly_rate=Decimal("30.00"),
        reason_for_separation=text,
    )
    restored = parse_from_canonical(render_to_canonical(fields))
    assert len(text) >= 20
    assert levenshtein(text, restored.reason_for_separation) <= 1


# ===========================================================================
# Edge cases — enumerated explicitly (not property-generated).
# ===========================================================================


def _base_fields(**overrides) -> ROEFields:
    """Build a valid ROEFields, overriding specific attributes for a case."""
    defaults = dict(
        employer_name="Acme Manufacturing Inc",
        employer_bn="123456789RP0001",
        employee_name="Jane Elizabeth Doe",
        sin="046454286",
        occupation="Industrial Welder",
        first_day_worked=date(2023, 1, 3),
        last_day_paid=date(2023, 6, 30),
        total_insurable_hours=Decimal("1200.00"),
        total_insurable_earnings=Decimal("36000.00"),
        hourly_rate=Decimal("30.00"),
        reason_for_separation="Shortage of work at the plant",
    )
    defaults.update(overrides)
    return ROEFields(**defaults)


def test_P6_zero_and_high_value_numerics():
    """Property 6: OCR round-trip fidelity. Validates: Requirements 7.13

    Zero and large numerics survive the 2 dp canonical round-trip exactly.
    """
    fields = _base_fields(
        total_insurable_hours=Decimal("0.00"),
        total_insurable_earnings=Decimal("999999.99"),
        hourly_rate=Decimal("0.01"),
    )
    restored = parse_from_canonical(render_to_canonical(fields))
    assert restored.total_insurable_hours == Decimal("0.00")
    assert restored.total_insurable_earnings == Decimal("999999.99")
    assert restored.hourly_rate == Decimal("0.01")


def test_P6_leap_day_date():
    """Property 6: OCR round-trip fidelity. Validates: Requirements 7.13

    A leap-day date round-trips exactly.
    """
    fields = _base_fields(first_day_worked=date(2024, 2, 29))
    restored = parse_from_canonical(render_to_canonical(fields))
    assert restored.first_day_worked == date(2024, 2, 29)


def test_P6_exactly_twenty_char_free_text():
    """Property 6: OCR round-trip fidelity. Validates: Requirements 7.13

    A free-text field of exactly 20 characters round-trips within 1 edit.
    """
    text = "Shortage of work here"[:20]  # 20 chars
    assert len(text) == 20
    fields = _base_fields(reason_for_separation=text)
    restored = parse_from_canonical(render_to_canonical(fields))
    assert levenshtein(text, restored.reason_for_separation) <= 1


def test_P6_free_text_containing_colon_and_label_like_content():
    """Property 6: OCR round-trip fidelity. Validates: Requirements 7.13

    Free text containing a colon (label-like content) still round-trips within
    tolerance because parsing is anchored to line-start labels.
    """
    text = "Reason: laid off due to seasonal downturn"
    fields = _base_fields(reason_for_separation=text)
    restored = parse_from_canonical(render_to_canonical(fields))
    assert levenshtein(text, restored.reason_for_separation) <= 1
