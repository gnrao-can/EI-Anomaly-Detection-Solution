"""
tests/unit/test_repetitive_pattern_engine_11_3.py
=================================================
Property-based test for the Repetitive Pattern Engine (task 11.3).

Property 1 — Sequential Pattern Detection Completeness (Requirement 3.8):

    FOR ALL claim sequences of length N >= 3 drawn from the same SIN, the
    Repetitive_Pattern_Engine SHALL detect at least one pattern Indicator when
    the inter-claim intervals are within +/-14 calendar days of each other
    across all consecutive pairs.

The task-11.1 detector ``sequential_pattern_mining`` implements a PrefixSpan-style
cadence detection: it scans the consecutive inter-claim gaps for the longest run
whose gaps all stay within ``2 * tolerance`` (= 28) days of one another. When
every consecutive interval is within +/-14 days of a shared cadence, all gaps are
mutually within 28 days, the whole sequence forms one qualifying run, and a HIGH
pattern Indicator is produced — satisfying P1.

Generation strategy (mirrors the design.md P1 skeleton): anchor each consecutive
inter-claim interval at ~365 days with a jitter ``delta`` in [-14, 14], so every
gap sits within +/-14 days of the 365-day cadence and any two consecutive gaps
differ by at most 28 days. We build N >= 3 claim dates from those intervals and
assert at least one Indicator is produced.

The service directory is hyphenated (``services/repetitive-pattern-engine``) and
its modules import each other by bare name, so the modules are loaded through the
shared ``load_service_module`` conftest helper, which namespaces ``sys.modules``
keys and keeps the full unit suite collision-free.

Hypothesis is configured with ``max_examples=200`` for CI, matching the other
property tests in this suite.

Requirements: 3.8
"""

from __future__ import annotations

from datetime import date, timedelta

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from shared.models import EngineType, Severity
from tests.conftest import load_service_module

# ---------------------------------------------------------------------------
# Load the hyphenated repetitive-pattern-engine service modules (collision-free).
# ---------------------------------------------------------------------------

_SVC = "repetitive-pattern-engine"
_SIBLINGS = [
    "config",
    "repository",
    "patterns",
    "hours_clustering",
    "llm_client",
    "kafka_producer",
    "main",
]

_patterns = load_service_module(_SVC, "patterns", siblings=_SIBLINGS)
sequential_pattern_mining = _patterns.sequential_pattern_mining
DEFAULT_INTERVAL_TOLERANCE_DAYS = _patterns.DEFAULT_INTERVAL_TOLERANCE_DAYS


# ===========================================================================
# Property 1: Sequential Pattern Detection Completeness (Req 3.8)
# ===========================================================================


class TestP1SequentialPatternCompleteness:
    """Feature: ei-anomaly-detection-solution, Property 1."""

    @settings(max_examples=200)
    @given(
        n=st.integers(min_value=3, max_value=10),
        base_date=st.dates(min_value=date(2019, 1, 1), max_value=date(2024, 1, 1)),
        intervals=st.lists(
            st.integers(min_value=-DEFAULT_INTERVAL_TOLERANCE_DAYS,
                        max_value=DEFAULT_INTERVAL_TOLERANCE_DAYS),
            min_size=2,
            max_size=9,
        ),
    )
    def test_P1_sequential_pattern_detection(self, n, base_date, intervals):
        """Feature: ei-anomaly-detection-solution, Property 1: sequential pattern detection completeness

        For any N >= 3 claims whose consecutive inter-claim intervals are all
        within +/-14 days of a shared cadence, at least one pattern Indicator is
        produced.

        **Validates: Requirements 3.8**
        """
        assume(len(intervals) >= n - 1)

        # Build N claim dates: each consecutive interval is ~365 days with a
        # jitter in [-14, 14], so every gap lies within +/-14 days of the 365-day
        # cadence (and any two consecutive gaps differ by at most 28 days).
        claim_dates = [base_date]
        for delta in intervals[: n - 1]:
            claim_dates.append(claim_dates[-1] + timedelta(days=365 + delta))

        assert len(claim_dates) == n

        indicators = sequential_pattern_mining(claim_dates)

        # Completeness: at least one pattern Indicator must be produced.
        assert len(indicators) >= 1, (
            f"expected >= 1 pattern Indicator for {n} claims with intervals "
            f"{[365 + d for d in intervals[: n - 1]]} days"
        )

        ind = indicators[0]
        assert ind.engine == EngineType.REPETITIVE_PATTERN
        assert ind.severity == Severity.HIGH
        assert ind.algorithm == "sequential_pattern_mining"
        # The detected pattern must span all N claims for this uniform cadence.
        assert ind.payload["pattern_length"] == n
