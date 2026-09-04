"""
tests/property/test_p4_zero_earnings_discrepancy.py
====================================================
Property 4: Zero Earnings Discrepancy Invariant.

**Validates: Requirements 6.10**

Statement
---------
*For any* pair of (ROE_earnings, T4_earnings) where both values are numerically
identical, the History_Inconsistency_Engine SHALL compute a discrepancy value of
exactly ``Decimal("0")`` and SHALL NOT generate a discrepancy Indicator.

The task-14.1 implementation splits this behaviour across two functions in
``services/history-inconsistency-engine/discrepancy.py``:

  * :func:`compute_discrepancy(roe_earnings, t4_earnings)` returns the exact
    ``Decimal`` absolute difference; when the inputs are equal the result is
    exactly ``Decimal("0")`` (no floating-point drift, because the arithmetic is
    performed with :class:`decimal.Decimal`).
  * :func:`check_earnings_discrepancy(roe_earnings, t4_earnings, ...)` returns
    the Indicator list; when the amounts are equal the discrepancy is ``0`` and
    the list is empty, so no CRITICAL discrepancy Indicator is produced.

This property drives Hypothesis over equal ROE and T4 values across a wide,
finite, non-NaN ``Decimal`` range and asserts both halves of the invariant.

Import isolation
----------------
The ``history-inconsistency-engine`` service directory contains a hyphen, so it
is not importable as a normal package. We use the shared
:func:`tests.conftest.load_service_module` helper (rather than ad-hoc
``importlib`` / ``sys.path`` juggling) so the module loads under a unique,
collision-free namespaced key and the whole property/unit suite stays
collision-free when run together.
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from tests.conftest import load_service_module

# --- Load the discrepancy module in isolation --------------------------------
# ``discrepancy`` imports ``shared.models`` (resolved normally via sys.path) and
# has no bare-name intra-service siblings, so the default siblings=[module_name]
# is sufficient.
_discrepancy = load_service_module(
    "history-inconsistency-engine", "discrepancy"
)
compute_discrepancy = _discrepancy.compute_discrepancy
check_earnings_discrepancy = _discrepancy.check_earnings_discrepancy


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
# Strategy: finite, non-NaN CAD earnings amounts with 2 decimal places within a
# bounded range (mirrors the design's P4 generator). Equal ROE and T4 values are
# obtained by feeding the same drawn ``earnings`` to both parameters.
# ---------------------------------------------------------------------------
_earnings_strategy = st.decimals(
    min_value=Decimal("0.00"),
    max_value=Decimal("999999.99"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)


# ---------------------------------------------------------------------------
# Property 4 — Hypothesis-driven invariant
# ---------------------------------------------------------------------------


@given(earnings=_earnings_strategy)
def test_P4_zero_discrepancy_when_roe_equals_t4(earnings):
    """Property 4. Validates: Requirements 6.10

    For equal ROE and T4 earnings, ``compute_discrepancy`` returns exactly
    ``Decimal("0")`` and ``check_earnings_discrepancy`` produces no Indicator
    (hence no CRITICAL discrepancy Indicator).
    """
    discrepancy = compute_discrepancy(roe_earnings=earnings, t4_earnings=earnings)
    assert discrepancy == Decimal("0")

    indicators = check_earnings_discrepancy(
        roe_earnings=earnings, t4_earnings=earnings
    )
    # No discrepancy Indicator at all when the amounts are equal ...
    assert indicators == []
    # ... and in particular no CRITICAL discrepancy Indicator.
    assert not any(
        ind.severity.value == "CRITICAL" and "discrepancy" in ind.algorithm
        for ind in indicators
    )


# ---------------------------------------------------------------------------
# Direct (deterministic) edge-case assertions complementing the property.
# ---------------------------------------------------------------------------


def test_P4_zero_discrepancy_direct_examples():
    """Property 4. Validates: Requirements 6.10

    Concrete equal-value examples (zero, whole dollars, cents, and a large
    amount) all yield an exact ``Decimal("0")`` discrepancy and no Indicator.
    """
    for value in (
        Decimal("0.00"),
        Decimal("1234.00"),
        Decimal("999.99"),
        Decimal("500000.55"),
    ):
        assert compute_discrepancy(value, value) == Decimal("0")
        assert check_earnings_discrepancy(value, value) == []
