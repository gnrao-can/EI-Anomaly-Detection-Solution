"""
services/history-inconsistency-engine/analysis.py
==================================================
Task-14.2 analysis orchestration for the Employment History Inconsistency
Engine.

This module composes the three task-14.2 checks on top of the task-14.1
earnings-discrepancy result:

  1. **NOC validation** (Requirement 6.5) — compares the declared NOC code
     against the CRA employer industry category; a major-group mismatch yields a
     HIGH Indicator (see :mod:`noc`).
  2. **Concurrent employment** (Requirement 6.8) — detects a second employer
     BN active during any portion of the claim period; each overlap yields a
     CRITICAL Indicator (see :mod:`concurrent`).
  3. **LLM narrative contradiction** (Requirements 6.6, 6.7) — an async check
     with a 10-second timeout, run *outside* the wall-clock budget (see
     :mod:`narrative`).

Wall-clock budget (Requirement 6.9)
-----------------------------------
The engine must publish all Indicators within **500 ms excluding LLM wait
time**. :func:`run_core_analysis` runs the NOC + concurrent checks (the CRA
queries and the pure computation) and measures elapsed wall-clock against the
500 ms budget. The LLM narrative check is awaited separately by the caller and
is explicitly excluded from that measurement.

Everything here is dependency-injected (the CRA client and the LLM checker) so
the whole module is unit-testable with fakes — no live network is used.

Requirements: 6.5, 6.6, 6.7, 6.8, 6.9
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from shared.models import Indicator

try:  # sibling modules imported by bare name in the service directory
    from noc import check_noc_mismatch  # type: ignore
    from concurrent_employment import (  # type: ignore
        EmploymentSpan,
        detect_concurrent_employment,
    )
    from narrative import NarrativeContradictionChecker  # type: ignore
except ImportError:  # pragma: no cover - when imported as a package
    from .noc import check_noc_mismatch  # type: ignore
    from .concurrent_employment import (  # type: ignore
        EmploymentSpan,
        detect_concurrent_employment,
    )
    from .narrative import NarrativeContradictionChecker  # type: ignore

logger = logging.getLogger("history-inconsistency-engine.analysis")

#: Wall-clock budget (ms) for the core analysis, excluding LLM wait (Req 6.9).
CORE_BUDGET_MS: int = 500


@dataclass
class CoreAnalysisResult:
    """Result of the non-LLM (budgeted) portion of task-14.2 analysis.

    Attributes
    ----------
    indicators:
        NOC-mismatch and concurrent-employment Indicators produced.
    elapsed_ms:
        Wall-clock time spent on the budgeted analysis, in milliseconds.
    within_budget:
        ``True`` when the budgeted analysis stayed within the 500 ms budget
        (Requirement 6.9). LLM wait time is excluded by construction.
    """

    indicators: list[Indicator] = field(default_factory=list)
    elapsed_ms: float = 0.0
    within_budget: bool = True


async def run_core_analysis(
    cra_client: Any,
    *,
    sin_hash: str,
    tax_year: int,
    employer_bn: str,
    declared_noc: Optional[str],
    claim_start: date,
    claim_end: date,
    claim_id: Optional[str] = None,
    budget_ms: int = CORE_BUDGET_MS,
) -> CoreAnalysisResult:
    """Run the NOC-mismatch and concurrent-employment checks within budget.

    Both checks query the CRA client (the NOC category comes from the T4 record;
    concurrent spans come from the employment endpoint). The elapsed wall-clock
    time is measured against the 500 ms budget (Requirement 6.9) — the LLM
    narrative check is NOT part of this measurement and is run separately by the
    caller.
    """
    start = time.monotonic()
    indicators: list[Indicator] = []

    # ── NOC validation (Requirement 6.5) ──
    cra_industry_category = await cra_client.fetch_employer_noc_category(
        sin_hash, tax_year, employer_bn
    )
    indicators.extend(
        check_noc_mismatch(
            declared_noc, cra_industry_category, claim_id=claim_id
        )
    )

    # ── Concurrent employment (Requirement 6.8) ──
    raw_spans = await cra_client.fetch_concurrent_employment(
        sin_hash, claim_start.isoformat(), claim_end.isoformat()
    )
    spans = [EmploymentSpan.from_dict(s) for s in raw_spans]
    indicators.extend(
        detect_concurrent_employment(
            spans,
            claim_start,
            claim_end,
            claim_employer_bn=employer_bn,
            claim_id=claim_id,
        )
    )

    elapsed_ms = (time.monotonic() - start) * 1000.0
    within_budget = elapsed_ms <= budget_ms
    if not within_budget:
        logger.warning(
            "core analysis for claim %s took %.1f ms, exceeding the %d ms budget",
            claim_id,
            elapsed_ms,
            budget_ms,
        )
    return CoreAnalysisResult(
        indicators=indicators,
        elapsed_ms=elapsed_ms,
        within_budget=within_budget,
    )


async def run_narrative_check(
    narrative_checker: Optional[NarrativeContradictionChecker],
    *,
    narrative: Optional[str],
    cra_evidence: Optional[str],
    claim_id: Optional[str] = None,
) -> list[Indicator]:
    """Run the async LLM contradiction check (Requirements 6.6, 6.7).

    Returns an empty list when no checker is configured, when there is no
    narrative to analyse, or when there is no CRA continued-employment evidence
    to contradict (the LLM is only consulted when CRA shows continued employment
    during the claimed separation period, per the design). Otherwise delegates
    to the checker, which applies the 10-second timeout and emits either a
    CRITICAL contradiction Indicator or a NARRATIVE_ANALYSIS_TIMEOUT-flagged
    Indicator.
    """
    if narrative_checker is None:
        return []
    if not narrative or not cra_evidence:
        return []
    return await narrative_checker.check(
        narrative, cra_evidence, claim_id=claim_id
    )


__all__ = [
    "CORE_BUDGET_MS",
    "CoreAnalysisResult",
    "run_core_analysis",
    "run_narrative_check",
]
