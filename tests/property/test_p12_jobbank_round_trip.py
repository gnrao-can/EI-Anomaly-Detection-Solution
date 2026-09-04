"""
tests/property/test_p12_jobbank_round_trip.py
=============================================
Property 12: Job Bank adapter round-trip. Validates: Requirements 16.5

Statement
---------
*For any* :class:`JobBankData` instance ``d``, parsing its serialised form
reproduces the same posting data exactly:

    parse_job_bank_data(serialise_job_bank_data(d))

preserves

  * ``active_posting_count`` EXACTLY (integer equality);
  * ``median_hourly_wage`` EXACTLY (``Decimal`` equality) — the wage crosses the
    serialisation boundary as a *string*, so there is no float precision loss,
    and trailing zeros / high precision are preserved verbatim;
  * ``noc_code`` and ``region_code`` unchanged; and
  * idempotence at the wire level:
    ``serialise(parse(serialise(d))) == serialise(d)``.

The Decimal median wage is the interesting part: because it is emitted as a
string (never a JSON float), even values with many fractional digits or trailing
zeros survive the round-trip bit-for-bit. This test drives Hypothesis at those
edge cases (trailing zeros such as ``"24.1000"``, zero postings, very high
precision) plus a broad generated space.

Module loading
--------------
The ``services/job-bank-adapter`` directory is hyphenated (not an importable
package) and its modules import each other by bare name. We load the
``serialization`` module through :func:`tests.conftest.load_service_module`,
which registers it under a collision-free namespaced key so running the whole
suite in one process stays clean.

Property 12: Job Bank adapter round-trip. Validates: Requirements 16.5
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.conftest import load_service_module

# ---------------------------------------------------------------------------
# Load the hyphenated job-bank-adapter serialization module (collision-free).
# ---------------------------------------------------------------------------
_serialization = load_service_module(
    "job-bank-adapter", "serialization", siblings=["serialization"]
)
JobBankData = _serialization.JobBankData
serialise_job_bank_data = _serialization.serialise_job_bank_data
parse_job_bank_data = _serialization.parse_job_bank_data


# ---------------------------------------------------------------------------
# Hypothesis profile: CI runs 200 examples (design.md — Testing Strategy;
# tasks.md Notes: settings.register_profile("ci", max_examples=200, ...)).
# Registration is guarded so a duplicate registration from a sibling
# property-test module already loaded in the same session does not raise; we
# then always (re)load the "ci" profile so this file runs 200 examples.
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

# 5-char NOC-like codes: digit strings, plus a small fixed set of realistic
# NOC 2021 codes to keep some examples concrete.
_noc_digits = st.text(alphabet="0123456789", min_size=5, max_size=5)
_noc_known = st.sampled_from(["21234", "63201", "00000", "75110", "42202"])
noc_code_strategy = st.one_of(_noc_digits, _noc_known)

# 2-char ESDC region code.
region_code_strategy = st.text(alphabet="0123456789", min_size=2, max_size=2)

posting_count_strategy = st.integers(min_value=0, max_value=100_000)

# Decimals in [0, 1000] with up to 4 fractional places, no NaN / infinity.
# Covering up to 4 places exercises trailing-zero and high-precision cases;
# the string serialisation must preserve exactly what Hypothesis generates.
wage_strategy = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("1000"),
    places=4,
    allow_nan=False,
    allow_infinity=False,
)

# Optional reference month (first-of-month dates) and stale flag.
reference_month_strategy = st.one_of(
    st.none(),
    st.dates(min_value=date(2015, 1, 1), max_value=date(2030, 12, 1)).map(
        lambda d: d.replace(day=1)
    ),
)


@st.composite
def job_bank_data_strategy(draw) -> JobBankData:
    """Generate a valid :class:`JobBankData` instance across the input space."""
    return JobBankData(
        noc_code=draw(noc_code_strategy),
        region_code=draw(region_code_strategy),
        active_posting_count=draw(posting_count_strategy),
        median_hourly_wage=draw(wage_strategy),
        reference_month=draw(reference_month_strategy),
        data_stale=draw(st.booleans()),
    )


# ---------------------------------------------------------------------------
# The core P12 assertions — reused across every property below.
# ---------------------------------------------------------------------------


def _assert_round_trip_preserves(data: JobBankData) -> None:
    """Assert parse(serialise(data)) preserves counts, wage, and codes exactly."""
    restored = parse_job_bank_data(serialise_job_bank_data(data))

    # Posting count preserved EXACTLY (int equality).
    assert restored.active_posting_count == data.active_posting_count
    assert isinstance(restored.active_posting_count, int)

    # Median wage preserved EXACTLY (Decimal equality) — no float loss because
    # the value crosses the boundary as a string.
    assert restored.median_hourly_wage == data.median_hourly_wage
    # The exact digit sequence (including trailing zeros) is preserved too.
    assert str(restored.median_hourly_wage) == str(data.median_hourly_wage)

    # NOC and region codes preserved.
    assert restored.noc_code == data.noc_code
    assert restored.region_code == data.region_code


def _assert_wire_idempotent(data: JobBankData) -> None:
    """Assert serialise(parse(serialise(data))) == serialise(data)."""
    once = serialise_job_bank_data(data)
    twice = serialise_job_bank_data(parse_job_bank_data(once))
    assert twice == once


# ===========================================================================
# Property 12 — generated across the full input space.
# ===========================================================================


@given(data=job_bank_data_strategy())
def test_P12_round_trip_preserves_count_and_wage(data):
    """Property 12: Job Bank adapter round-trip. Validates: Requirements 16.5

    parse(serialise(d)) preserves posting count exactly and median wage exactly
    (Decimal equality) for any generated JobBankData.
    """
    _assert_round_trip_preserves(data)


@given(data=job_bank_data_strategy())
def test_P12_serialise_is_idempotent(data):
    """Property 12: Job Bank adapter round-trip. Validates: Requirements 16.5

    Serialising the parsed form of a serialised value reproduces the exact same
    wire dict: serialise(parse(serialise(d))) == serialise(d).
    """
    _assert_wire_idempotent(data)


@given(
    noc=noc_code_strategy,
    region=region_code_strategy,
    count=posting_count_strategy,
    wage=wage_strategy,
)
def test_P12_wage_crosses_boundary_as_string(noc, region, count, wage):
    """Property 12: Job Bank adapter round-trip. Validates: Requirements 16.5

    The median wage is serialised as a string (never a JSON float), guaranteeing
    exact reconstruction of the Decimal.
    """
    data = JobBankData(noc, region, count, wage)
    payload = serialise_job_bank_data(data)
    assert isinstance(payload["median_hourly_wage"], str)
    assert isinstance(payload["active_posting_count"], int)
    assert parse_job_bank_data(payload).median_hourly_wage == wage


# ===========================================================================
# Edge cases — enumerated explicitly (not property-generated).
# ===========================================================================


def test_P12_trailing_zeros_preserved():
    """Property 12: Job Bank adapter round-trip. Validates: Requirements 16.5

    A wage with trailing zeros ("24.1000") survives as the identical string.
    """
    data = JobBankData("63201", "59", 245, Decimal("24.1000"))
    restored = parse_job_bank_data(serialise_job_bank_data(data))
    assert restored.median_hourly_wage == Decimal("24.1000")
    assert str(restored.median_hourly_wage) == "24.1000"


def test_P12_zero_postings_and_zero_wage():
    """Property 12: Job Bank adapter round-trip. Validates: Requirements 16.5

    Zero postings and a zero wage round-trip exactly.
    """
    data = JobBankData("21234", "35", 0, Decimal("0"))
    restored = parse_job_bank_data(serialise_job_bank_data(data))
    assert restored.active_posting_count == 0
    assert restored.median_hourly_wage == Decimal("0")


def test_P12_high_precision_wage_preserved():
    """Property 12: Job Bank adapter round-trip. Validates: Requirements 16.5

    A high-precision Decimal wage is not corrupted by any float conversion.
    """
    wage = Decimal("33.123456789")
    data = JobBankData("00000", "10", 1, wage)
    restored = parse_job_bank_data(serialise_job_bank_data(data))
    assert restored.median_hourly_wage == wage
    assert str(restored.median_hourly_wage) == "33.123456789"


def test_P12_max_postings_boundary():
    """Property 12: Job Bank adapter round-trip. Validates: Requirements 16.5

    A large posting count round-trips exactly as an int.
    """
    data = JobBankData("75110", "24", 100_000, Decimal("999.9999"))
    restored = parse_job_bank_data(serialise_job_bank_data(data))
    assert restored.active_posting_count == 100_000
    assert restored.median_hourly_wage == Decimal("999.9999")
