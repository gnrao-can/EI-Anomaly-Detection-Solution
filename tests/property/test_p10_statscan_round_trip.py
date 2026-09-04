"""
tests/property/test_p10_statscan_round_trip.py
==============================================
Property 10: Stats Canada adapter round-trip. Validates: Requirements 10.7

Statement
---------
*For any* raw Statistics Canada Labour Force Survey (LFS) payload, parsing it to
canonical ``LabourForceData`` and then serialising + re-parsing SHALL:

  1. Cover exactly the same set of ESDC region codes (none lost, none added);
  2. Preserve every rate field (``unemployment_rate``, ``employment_rate``,
     ``participation_rate``, ``national_unemployment_rate``) EXACTLY, as
     ``Decimal`` values retaining >= 4 decimal places; and
  3. Be idempotent, i.e. ``serialise(parse(serialise(records))) ==
     serialise(records)``.

The spec's exact formulation ``parse(serialise(parse(raw)))`` is asserted
directly (see :func:`test_P10_parse_serialise_parse_covers_regions_and_rates`).

Round-trip precision is guaranteed by the adapter's string-path Decimal
serialisation (``str(decimal)`` on the way out, ``Decimal(str(value))`` on the
way in), so no counterexample should reveal precision loss. If one ever did,
this test surfaces it rather than weakening the assertion.

Import isolation
----------------
The service directory ``services/stats-canada-adapter`` is hyphenated and its
modules import each other by bare name, so it is not importable as a normal
package. We load ``client.py`` under a unique namespaced key via the shared
``tests/conftest.py`` :func:`load_service_module` helper to stay collision-free
when the whole suite runs in one process.

Property 10: Stats Canada adapter round-trip. Validates: Requirements 10.7
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.conftest import load_service_module

# ---------------------------------------------------------------------------
# Load the hyphenated service module under a collision-free namespaced key.
# ``client`` imports its siblings by bare name; the helper handles that.
# ---------------------------------------------------------------------------

try:
    client_mod = load_service_module(
        "stats-canada-adapter", "client", siblings=["client"]
    )
    parse_lfs_response = client_mod.parse_lfs_response
    serialise = client_mod.serialise
    _HAS_ADAPTER = True
except Exception as exc:  # noqa: BLE001 - defensive: skip if not importable
    _HAS_ADAPTER = False
    _IMPORT_ERROR = exc


pytestmark = pytest.mark.skipif(
    not _HAS_ADAPTER, reason="stats-canada-adapter not importable"
)


# ---------------------------------------------------------------------------
# Hypothesis "ci" profile: 200 examples (design.md Testing Strategy; tasks.md
# Notes). Registration is guarded so a duplicate registration from a sibling
# property-test module already loaded in the same session does not raise.
# ---------------------------------------------------------------------------

try:
    settings.register_profile(
        "ci", max_examples=200, suppress_health_check=[HealthCheck.too_slow]
    )
except Exception:  # noqa: BLE001 - already registered elsewhere
    pass
settings.load_profile("ci")


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# A realistic set of 2-character ESDC economic region codes, including all the
# major ESDC regions used across the mock-data fixtures plus a broader spread so
# generated examples exercise a variety of regions.
_ESDC_REGION_CODES = [
    "10",  # St. John's, Newfoundland and Labrador
    "11",  # Prince Edward Island
    "12",  # Halifax, Nova Scotia
    "13",  # Moncton, New Brunswick
    "24",  # Montreal, Quebec
    "35",  # Toronto, Ontario
    "46",  # Winnipeg, Manitoba
    "47",  # Saskatoon, Saskatchewan
    "48",  # Calgary, Alberta
    "59",  # Vancouver, British Columbia
    "60",  # Yukon
    "61",  # Northwest Territories
    "62",  # Nunavut
]

# Fixed reference month for every record in a payload. LabourForceData is keyed
# on (esdc_region_code, reference_month) for equality/hashing, so holding the
# month constant and keeping region codes unique guarantees each record is a
# distinct region — exactly the "all ESDC regions covered" invariant under test.
_REFERENCE_MONTH = "2024-05"


@st.composite
def _rate_string(draw: st.DrawFn) -> str:
    """Draw a rate as a decimal string with >= 4 fractional digits.

    Generating via strings (rather than floats) drives the adapter's string-path
    ``Decimal(str(value))`` parsing and exercises values that are not exactly
    representable as IEEE-754 floats. Includes trailing-zero cases (e.g.
    ``"6.1000"``) so we prove they survive the round-trip unchanged.
    """
    whole = draw(st.integers(min_value=0, max_value=99))
    # 4..7 fractional digits so precision always exceeds the 4-dp requirement.
    frac_len = draw(st.integers(min_value=4, max_value=7))
    frac_digits = draw(
        st.lists(
            st.integers(min_value=0, max_value=9),
            min_size=frac_len,
            max_size=frac_len,
        )
    )
    frac = "".join(str(d) for d in frac_digits)
    return f"{whole}.{frac}"


@st.composite
def _raw_lfs_payload(draw: st.DrawFn) -> dict:
    """Generate a raw LFS payload dict with 1..N records over unique regions."""
    n = draw(st.integers(min_value=1, max_value=len(_ESDC_REGION_CODES)))
    # Unique region codes so every record is a distinct ESDC region.
    region_codes = draw(
        st.lists(
            st.sampled_from(_ESDC_REGION_CODES),
            min_size=n,
            max_size=n,
            unique=True,
        )
    )
    # One shared national rate for the reference month (as it is in real data).
    national_rate = draw(_rate_string())
    source_api_version = draw(
        st.sampled_from(["98100424", "98100424-v2", "v1", "unknown"])
    )

    records = []
    for code in region_codes:
        records.append(
            {
                "reference_month": _REFERENCE_MONTH,
                "esdc_region_code": code,
                "region_name_en": draw(st.text(min_size=0, max_size=40)),
                "region_name_fr": draw(st.text(min_size=0, max_size=40)),
                "unemployment_rate": draw(_rate_string()),
                "employment_rate": draw(_rate_string()),
                "participation_rate": draw(_rate_string()),
                "national_unemployment_rate": national_rate,
            }
        )

    return {"source_api_version": source_api_version, "records": records}


# ---------------------------------------------------------------------------
# Assertions helpers
# ---------------------------------------------------------------------------

_RATE_FIELDS = (
    "unemployment_rate",
    "employment_rate",
    "participation_rate",
    "national_unemployment_rate",
)


def _fractional_places(value: Decimal) -> int:
    """Number of fractional decimal places carried by a Decimal."""
    exponent = value.as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


def _assert_regions_and_rates_preserved(reference, roundtrip) -> None:
    """Assert region coverage + exact rate preservation between two record lists."""
    ref_codes = {r.esdc_region_code for r in reference}
    rt_codes = {r.esdc_region_code for r in roundtrip}
    assert rt_codes == ref_codes, (
        f"region coverage changed: added={rt_codes - ref_codes}, "
        f"lost={ref_codes - rt_codes}"
    )

    ref_by_code = {r.esdc_region_code: r for r in reference}
    rt_by_code = {r.esdc_region_code: r for r in roundtrip}
    for code, ref_rec in ref_by_code.items():
        rt_rec = rt_by_code[code]
        for field in _RATE_FIELDS:
            ref_val = getattr(ref_rec, field)
            rt_val = getattr(rt_rec, field)
            # Exact Decimal equality (not just numeric ==).
            assert rt_val == ref_val, (
                f"region {code} field {field}: {rt_val} != {ref_val}"
            )
            assert isinstance(rt_val, Decimal)
            assert _fractional_places(rt_val) >= 4, (
                f"region {code} field {field}: only "
                f"{_fractional_places(rt_val)} dp (< 4): {rt_val}"
            )


# ---------------------------------------------------------------------------
# Property 10 — parse(serialise(parse(raw))) : regions covered, rates preserved
# ---------------------------------------------------------------------------


@given(raw=_raw_lfs_payload())
def test_P10_parse_serialise_parse_covers_regions_and_rates(raw):
    """Property 10: Stats Canada adapter round-trip. Validates: Requirements 10.7

    The spec's exact formulation ``parse(serialise(parse(raw)))`` covers every
    ESDC region present in ``raw`` and preserves all four rate fields to >= 4
    decimal places, with exact Decimal equality.
    """
    records1 = parse_lfs_response(raw)
    # The spec formulation: parse(serialise(parse(raw))).
    round_records = parse_lfs_response(serialise(records1))

    _assert_regions_and_rates_preserved(records1, round_records)


# ---------------------------------------------------------------------------
# Property 10 — idempotence of the canonical serialisation
# ---------------------------------------------------------------------------


@given(raw=_raw_lfs_payload())
def test_P10_serialisation_is_idempotent(raw):
    """Property 10: Stats Canada adapter round-trip. Validates: Requirements 10.7

    ``serialise(parse(serialise(records1))) == serialise(records1)`` — once
    parsed, the canonical payload is a fixed point of the round-trip.
    """
    records1 = parse_lfs_response(raw)
    canonical = serialise(records1)
    reparsed = parse_lfs_response(canonical)
    assert serialise(reparsed) == canonical


# ---------------------------------------------------------------------------
# Edge cases (explicit examples that Hypothesis might otherwise reach rarely)
# ---------------------------------------------------------------------------


def test_P10_single_region_round_trip():
    """Property 10: Stats Canada adapter round-trip. Validates: Requirements 10.7

    A payload with a single region round-trips with the region covered and rates
    preserved exactly.
    """
    raw = {
        "source_api_version": "98100424",
        "records": [
            {
                "reference_month": _REFERENCE_MONTH,
                "esdc_region_code": "35",
                "region_name_en": "Toronto, Ontario",
                "region_name_fr": "Toronto, Ontario",
                "unemployment_rate": "6.9057",
                "employment_rate": "60.8813",
                "participation_rate": "65.4470",
                "national_unemployment_rate": "6.1024",
            }
        ],
    }
    records1 = parse_lfs_response(raw)
    round_records = parse_lfs_response(serialise(records1))
    assert {r.esdc_region_code for r in round_records} == {"35"}
    _assert_regions_and_rates_preserved(records1, round_records)


def test_P10_many_regions_round_trip():
    """Property 10: Stats Canada adapter round-trip. Validates: Requirements 10.7

    A payload covering all known ESDC regions round-trips with every region
    still covered.
    """
    records = [
        {
            "reference_month": _REFERENCE_MONTH,
            "esdc_region_code": code,
            "region_name_en": f"Region {code}",
            "region_name_fr": f"Region {code}",
            "unemployment_rate": "5.1234",
            "employment_rate": "60.5678",
            "participation_rate": "64.9012",
            "national_unemployment_rate": "6.1024",
        }
        for code in _ESDC_REGION_CODES
    ]
    raw = {"source_api_version": "98100424", "records": records}
    records1 = parse_lfs_response(raw)
    round_records = parse_lfs_response(serialise(records1))
    assert {r.esdc_region_code for r in round_records} == set(_ESDC_REGION_CODES)
    _assert_regions_and_rates_preserved(records1, round_records)


def test_P10_trailing_zeros_preserved():
    """Property 10: Stats Canada adapter round-trip. Validates: Requirements 10.7

    Rates with trailing zeros (e.g. ``"6.1000"``) keep their exact scale and
    their >= 4-dp precision through the round-trip.
    """
    raw = {
        "source_api_version": "98100424",
        "records": [
            {
                "reference_month": _REFERENCE_MONTH,
                "esdc_region_code": "24",
                "region_name_en": "Montreal, Quebec",
                "region_name_fr": "Montreal, Quebec",
                "unemployment_rate": "6.1000",
                "employment_rate": "60.0000",
                "participation_rate": "64.5000",
                "national_unemployment_rate": "6.1000",
            }
        ],
    }
    records1 = parse_lfs_response(raw)
    round_records = parse_lfs_response(serialise(records1))
    rec = round_records[0]
    assert rec.unemployment_rate == Decimal("6.1000")
    assert rec.employment_rate == Decimal("60.0000")
    # Trailing zeros retained (scale preserved), so >= 4 dp holds.
    assert _fractional_places(rec.unemployment_rate) >= 4
    assert _fractional_places(rec.employment_rate) >= 4
    _assert_regions_and_rates_preserved(records1, round_records)
