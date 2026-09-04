"""
tests/unit/test_regional_analysis_engine_16_5.py
================================================
Focused boundary unit tests for the Regional Analysis Engine (task 16.5).

This file exercises the four boundary pairs called out explicitly by task 16.5,
using EXACT ``Decimal`` arithmetic so each threshold is tested at the precise
tipping point (the value that must NOT flag) and one ULP-equivalent step beyond
it (the value that MUST flag):

  * Rate deviation (Requirement 8.3): deviation of exactly -1.5 pp => NO
    Indicator; -1.5001 pp => a MEDIUM Indicator naming both rates and the
    deviation.
  * Active job postings (Requirement 8.4): exactly 200 postings => NO Indicator;
    201 postings => a HIGH Indicator stating the count.
  * Median advertised wage (Requirement 8.6): median wage exactly +15% above the
    claimant's insurable hourly earnings => NO Indicator; +15.01% above => a HIGH
    Indicator.
  * Recurring seasonal claim (Requirement 8.8): the same calendar quarter in only
    1 of the 3 prior years => NO Indicator; 2 of 3 => a MEDIUM Indicator naming
    the matching years and quarter.

These are DISTINCT from the broader coverage in
``test_regional_analysis_engine.py`` (task 16.1) and
``test_regional_analysis_engine_16_2.py`` (task 16.2): here every test is a
crisp on/just-past-the-line boundary assertion for the four scenarios named in
task 16.5, exercising the same already-implemented pure functions
(``check_rate_deviation``, ``check_job_postings``, ``check_recurring_claims``)
through mocked/dependency-injected inputs — no live network or database.

Isolation: the service directory is hyphenated
(``services/regional-analysis-engine``) and its modules import each other by
BARE name, so we load them via the shared ``load_service_module`` helper in
``tests/conftest.py`` with the FULL sibling set. That helper namespaces
``sys.modules`` keys and restores bare-name modules afterwards, keeping the whole
unit suite collision-free when run in one process.

Requirements: 8.3, 8.4, 8.6, 8.8
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from tests.conftest import load_service_module

# ---------------------------------------------------------------------------
# Load the hyphenated service modules under collision-free namespaced keys.
# The full sibling set MUST be supplied so that a bare name (e.g.
# ``kafka_producer``) never resolves to a different service's module when the
# whole unit suite runs in one process.
# ---------------------------------------------------------------------------

_SIBLINGS = [
    "config",
    "analysis",
    "client",
    "forecast",
    "seasonality",
    "providers",
    "kafka_producer",
    "main",
]

_analysis = load_service_module(
    "regional-analysis-engine", "analysis", siblings=_SIBLINGS
)
_seasonality = load_service_module(
    "regional-analysis-engine", "seasonality", siblings=_SIBLINGS
)

check_rate_deviation = _analysis.check_rate_deviation
rate_deviation = _analysis.rate_deviation
check_job_postings = _analysis.check_job_postings
check_recurring_claims = _seasonality.check_recurring_claims


# ===========================================================================
# Boundary 1 — Rate deviation exactly -1.5 pp vs -1.5001 pp (Requirement 8.3)
# ===========================================================================


class TestRateDeviationBoundary16_5:
    """Deviation strictly below -1.5 pp flags; exactly -1.5 pp does not."""

    def test_deviation_exactly_minus_1_5_pp_no_flag(self):
        # regional 5.0, national 6.5 => deviation is EXACTLY Decimal("-1.5").
        # -1.5 is not strictly below the -1.5 threshold => NO Indicator.
        assert rate_deviation(Decimal("5.0"), Decimal("6.5")) == Decimal("-1.5")
        assert check_rate_deviation(Decimal("5.0"), Decimal("6.5")) == []

    def test_deviation_minus_1_5001_pp_flags_medium(self):
        # regional 4.9999, national 6.5 => deviation EXACTLY Decimal("-1.5001"),
        # one ten-thousandth below the boundary => MEDIUM Indicator.
        deviation = rate_deviation(Decimal("4.9999"), Decimal("6.5"))
        assert deviation == Decimal("-1.5001")

        indicators = check_rate_deviation(Decimal("4.9999"), Decimal("6.5"))
        assert len(indicators) == 1
        ind = indicators[0]
        assert ind.severity.value == "MEDIUM"
        assert ind.algorithm == "regional_rate_deviation"
        # Evidence names both rates and the exact deviation (Requirement 8.3).
        assert ind.payload["regional_rate"] == "4.9999"
        assert ind.payload["national_rate"] == "6.5"
        assert Decimal(ind.payload["deviation"]) == Decimal("-1.5001")

    def test_deviation_just_inside_boundary_still_no_flag(self):
        # -1.4999 pp is above the threshold (less negative) => NO Indicator.
        assert rate_deviation(Decimal("5.0001"), Decimal("6.5")) == Decimal("-1.4999")
        assert check_rate_deviation(Decimal("5.0001"), Decimal("6.5")) == []


# ===========================================================================
# Boundary 2 — Active postings exactly 200 vs 201 (Requirement 8.4)
# ===========================================================================


class TestActivePostingsBoundary16_5:
    """More than 200 active postings flags HIGH; exactly 200 does not."""

    def test_exactly_200_postings_no_flag(self):
        # Neutral wage (median == hourly) isolates the postings boundary so a
        # stray wage Indicator can't be mistaken for the postings one.
        indicators = check_job_postings(
            active_count=200,
            median_wage=Decimal("20.00"),
            hourly_earnings=Decimal("20.00"),
        )
        assert not any(
            i.algorithm == "job_bank_active_postings" for i in indicators
        )

    def test_201_postings_flags_high(self):
        indicators = check_job_postings(
            active_count=201,
            median_wage=Decimal("20.00"),
            hourly_earnings=Decimal("20.00"),
        )
        postings = [
            i for i in indicators if i.algorithm == "job_bank_active_postings"
        ]
        assert len(postings) == 1
        ind = postings[0]
        assert ind.severity.value == "HIGH"
        assert ind.engine.value == "REGIONAL_ANALYSIS"
        # The Indicator states the number of active postings (Requirement 8.4).
        assert ind.payload["active_posting_count"] == 201
        assert ind.payload["threshold"] == 200


# ===========================================================================
# Boundary 3 — Median wage exactly +15% vs +15.01% (Requirement 8.6)
# ===========================================================================


class TestMedianWageBoundary16_5:
    """Median wage strictly more than +15% flags HIGH; exactly +15% does not."""

    def test_median_wage_exactly_15_percent_above_no_flag(self):
        # hourly 20.00 * 1.15 == EXACTLY Decimal("23.0000"); median 23.00 is not
        # strictly greater => NO wage Indicator. Low posting count keeps the
        # postings check silent so the wage boundary is isolated.
        hourly = Decimal("20.00")
        assert hourly * Decimal("1.15") == Decimal("23.0000")
        indicators = check_job_postings(
            active_count=10,
            median_wage=Decimal("23.00"),
            hourly_earnings=hourly,
        )
        assert not any(i.algorithm == "job_bank_median_wage" for i in indicators)

    def test_median_wage_15_01_percent_above_flags_high(self):
        # 20.00 * 1.15 == 23.00; median 23.01 is strictly greater (≈ +15.05%,
        # i.e. past the +15% line) => HIGH Indicator.
        indicators = check_job_postings(
            active_count=10,
            median_wage=Decimal("23.01"),
            hourly_earnings=Decimal("20.00"),
        )
        wage = [i for i in indicators if i.algorithm == "job_bank_median_wage"]
        assert len(wage) == 1
        ind = wage[0]
        assert ind.severity.value == "HIGH"
        assert ind.engine.value == "REGIONAL_ANALYSIS"
        assert ind.payload["median_wage"] == "23.01"
        assert ind.payload["hourly_earnings"] == "20.00"

    def test_median_wage_one_cent_below_boundary_no_flag(self):
        # 22.99 < 23.00 boundary => NO wage Indicator (guards the exact line).
        indicators = check_job_postings(
            active_count=10,
            median_wage=Decimal("22.99"),
            hourly_earnings=Decimal("20.00"),
        )
        assert not any(i.algorithm == "job_bank_median_wage" for i in indicators)


# ===========================================================================
# Boundary 4 — Recurring claim in 1 of 3 vs 2 of 3 prior years (Requirement 8.8)
# ===========================================================================


class TestRecurringClaimBoundary16_5:
    """Same quarter in >= 2 of 3 prior years flags MEDIUM; 1 of 3 does not."""

    def test_recurring_claim_one_of_three_prior_years_no_flag(self):
        # Reference 2024 Q1 (March). Only 2023 Q1 matches; 2022 and 2021 fall in
        # other quarters => 1 of 3 => NO Indicator.
        indicators = check_recurring_claims(
            date(2024, 3, 10),
            [date(2023, 2, 15), date(2022, 7, 1), date(2021, 11, 20)],
        )
        assert indicators == []

    def test_recurring_claim_two_of_three_prior_years_flags_medium(self):
        # Reference 2024 Q1 (March). 2023 Q1 and 2022 Q1 match => 2 of 3 =>
        # MEDIUM Indicator naming the matching years and the quarter.
        indicators = check_recurring_claims(
            date(2024, 3, 10),
            [date(2023, 1, 20), date(2022, 2, 5), date(2021, 8, 1)],
        )
        assert len(indicators) == 1
        ind = indicators[0]
        assert ind.severity.value == "MEDIUM"
        assert ind.algorithm == "recurring_seasonal_claim"
        assert ind.engine.value == "REGIONAL_ANALYSIS"
        # The Indicator identifies the matching years and the quarter (Req 8.8).
        assert ind.payload["quarter"] == 1
        assert ind.payload["matching_years"] == [2022, 2023]
        assert ind.payload["match_count"] == 2
