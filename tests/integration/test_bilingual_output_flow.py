"""
tests/integration/test_bilingual_output_flow.py
===============================================
Integration tests for bilingual (EN/FR) explanation output and its persistence
to the ClickHouse analytics store (task 33.3).

Requirements exercised
----------------------
* **20.2** — THE LLM_Service SHALL generate all natural-language Indicator
  explanations and Risk_Decision summaries as *parallel English and French*
  text. (Cross-refs Req 9.4: no more than 500 characters per language.)
* **20.4** — THE EI-ADS SHALL store *both* the English and French versions of
  all LLM_Service-generated explanations in the ClickHouse analytics store
  (``risk_assessments_analytics``) alongside each claim record.

What is verified
----------------
For a claim that triggers an LLM explanation, both ``explanation_en`` and
``explanation_fr`` are:

  1. populated (non-empty), and
  2. at most 500 characters each (Req 9.4 / 20.3 hard limit), and
  3. persisted to the ClickHouse ``risk_assessments_analytics`` table and
     readable back with both language columns intact (Req 20.4).

Two execution paths (mirrors ``test_orchestrator_flow.py``)
-----------------------------------------------------------
1. **testcontainers ClickHouse** — the preferred, spec-faithful path. It starts
   a real ClickHouse server via ``testcontainers``, creates the
   ``risk_assessments_analytics`` table from the design DDL, drives the *real*
   ``llm-service`` ``ExplanationGenerator`` to produce a bilingual explanation,
   persists it through the *real* ``ensemble-scorer`` ``RiskAssessmentRepository``
   ClickHouse writer, then queries the row back and asserts both EN and FR are
   present, each <= 500 characters. Requires the ``testcontainers`` and
   ``clickhouse-connect`` packages **and** a reachable container daemon
   (Docker/Podman). All three are absent in the CI sandbox, so this class is
   skipped with a clear reason rather than failing.

   To run it locally: ``poetry install`` (installs the dev ``testcontainers``
   group and ``clickhouse-connect``), start Docker/Podman, then
   ``pytest tests/integration/test_bilingual_output_flow.py -m integration``.

2. **in-process** — always runs, no external services. It drives the same real
   ``ExplanationGenerator`` (backed by the deterministic ``StubLLM`` in the
   sandbox) and persists through the real ``RiskAssessmentRepository`` wired to
   an in-memory ClickHouse fake that captures exactly what the real
   clickhouse-connect ``insert`` call would receive. It asserts both EN and FR
   are populated, each <= 500 chars, and that both columns land in the
   ``risk_assessments_analytics`` insert. This gives equivalent coverage of the
   task's assertions without a container runtime.

This file deliberately uses a DISTINCT module name and adds NO project-level
fixtures / conftest entries, so it composes cleanly with sibling tasks
(33.1 / 33.2) that may create their own integration tests or a shared
``conftest.py``. It only reuses the existing ``tests.conftest.load_service_module``
helper for collision-free loading of the hyphenated service packages.

Requirements: 20.2, 20.4
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from uuid import uuid4

import pytest

# ---------------------------------------------------------------------------
# Load hyphenated service modules collision-free via the shared helper.
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from tests.conftest import load_service_module  # noqa: E402

from shared.models import (  # noqa: E402 - after sys.path setup
    EngineType,
    RiskAssessment,
    RiskDecision,
    Severity,
    ShapAttribution,
    Indicator,
)

# llm-service ``explanation`` imports its siblings (templates, llm_chain) by
# bare name; register them together so the load resolves cleanly.
_llm_explanation = load_service_module(
    "llm-service",
    "explanation",
    siblings=["config", "templates", "llm_chain", "explanation"],
)
ExplanationGenerator = _llm_explanation.ExplanationGenerator
MAX_CHARS_PER_LANGUAGE = _llm_explanation.MAX_CHARS_PER_LANGUAGE

# ensemble-scorer ``repository`` provides the real ClickHouse writer and the
# in-memory fake used by the always-on in-process path.
_scorer_repo = load_service_module(
    "ensemble-scorer", "repository", siblings=["repository"]
)
RiskAssessmentRepository = _scorer_repo.RiskAssessmentRepository

# The analytics table under test (design.md — ClickHouse Schema Overview).
ANALYTICS_TABLE = "risk_assessments_analytics"


# ---------------------------------------------------------------------------
# Shared helpers (module-local; no project-level fixtures added)
# ---------------------------------------------------------------------------


def _make_scored_indicators() -> list[Indicator]:
    """Build a small set of indicators that will drive an LLM explanation.

    SHAP contributions differ so the generator has a clear top-3 ranking, the
    same shape the Ensemble Scorer emits after attribution.
    """
    claim_id = uuid4()
    specs = [
        (EngineType.GEOGRAPHIC_ANOMALY, Severity.CRITICAL,
         "Economically implausible commute distance", 31.4),
        (EngineType.DOCUMENT_FORGERY, Severity.HIGH,
         "ROE signature does not match employer registry", 18.9),
        (EngineType.FRAUD_RING, Severity.HIGH,
         "Employer linked to a coordinated claimant cluster", 12.2),
        (EngineType.REPETITIVE_PATTERN, Severity.MEDIUM,
         "Repeated claim cadence across benefit periods", 4.1),
    ]
    indicators: list[Indicator] = []
    for engine, severity, title, shap in specs:
        indicators.append(
            Indicator(
                indicator_id=uuid4(),
                claim_id=claim_id,
                engine=engine,
                severity=severity,
                algorithm="integration_test",
                title=title,
                explanation_en=f"{title} (source signal).",
                explanation_fr=f"{title} (signal source).",
                payload={"engine": engine.value},
                produced_at=datetime.now(timezone.utc),
                shap_contribution=shap,
            )
        )
    return indicators


async def _generate_bilingual_explanation() -> tuple[str, str, dict]:
    """Run the real ExplanationGenerator and return ``(en, fr, full_result)``.

    In the sandbox the generator is backed by the deterministic ``StubLLM``; the
    JSON parse of that stub text fails, so the generator falls back to its
    deterministic bilingual template — which is exactly the parallel EN/FR text
    Req 20.2 requires. Either way we get populated, length-bounded EN and FR.
    """
    indicators = _make_scored_indicators()
    generator = ExplanationGenerator()
    result = await generator.generate(
        indicators=indicators,
        risk_score=82,
        risk_decision=RiskDecision.INVESTIGATE,
    )
    return result["en"], result["fr"], result


def _build_assessment(explanation_en: str, explanation_fr: str) -> RiskAssessment:
    """Assemble a RiskAssessment carrying the bilingual explanation to persist."""
    return RiskAssessment(
        assessment_id=uuid4(),
        claim_id=uuid4(),
        risk_score=82,
        risk_decision=RiskDecision.INVESTIGATE,
        shap_base_value=5.0,
        shap_attributions=[
            ShapAttribution(
                indicator_id=uuid4(),
                feature_name="geographic_anomaly_critical_count",
                contribution=31.4,
                base_value=5.0,
            )
        ],
        explanation_en=explanation_en,
        explanation_fr=explanation_fr,
        engines_completed=[
            EngineType.GEOGRAPHIC_ANOMALY,
            EngineType.DOCUMENT_FORGERY,
            EngineType.FRAUD_RING,
            EngineType.REPETITIVE_PATTERN,
        ],
        engines_partial=[],
        scored_at=datetime.now(timezone.utc),
        model_version="integration_v1",
    )


def _assert_bilingual_bounds(en: str, fr: str) -> None:
    """Both explanations must be populated and <= 500 characters (Req 20.2/9.4)."""
    assert en and en.strip(), "English explanation must be populated (Req 20.2)"
    assert fr and fr.strip(), "French explanation must be populated (Req 20.2)"
    assert len(en) <= MAX_CHARS_PER_LANGUAGE == 500, (
        f"EN explanation exceeds 500 chars: {len(en)}"
    )
    assert len(fr) <= MAX_CHARS_PER_LANGUAGE == 500, (
        f"FR explanation exceeds 500 chars: {len(fr)}"
    )


# ---------------------------------------------------------------------------
# In-process ClickHouse fake — captures exactly what clickhouse-connect's
# ``insert(table, rows, column_names=...)`` call would receive.
# ---------------------------------------------------------------------------


class InMemoryClickHouseClient:
    """Minimal stand-in for a ``clickhouse-connect`` client.

    Records every ``insert`` and lets tests read back rows as column->value
    dicts, so we can assert both explanation columns are persisted to
    ``risk_assessments_analytics`` exactly as the real writer would send them.
    """

    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[list], list[str]]] = []

    def insert(self, table: str, rows, column_names=None):  # noqa: ANN001
        self.inserts.append((table, [list(r) for r in rows], list(column_names or [])))

    def rows_for(self, table: str) -> list[dict]:
        out: list[dict] = []
        for (tbl, rows, cols) in self.inserts:
            if tbl != table:
                continue
            for row in rows:
                out.append(dict(zip(cols, row)))
        return out


# ===========================================================================
# In-process integration path (always runs — no external services)
# ===========================================================================


@pytest.mark.asyncio
async def test_bilingual_explanation_generated_parallel_en_fr():
    """LLM_Service produces parallel EN + FR text, each <= 500 chars.

    Validates: Requirements 20.2
    """
    en, fr, result = await _generate_bilingual_explanation()

    _assert_bilingual_bounds(en, fr)
    # Parallel text: both language keys present and the reconciled P15 balance
    # metric (character counts) is reported for each language.
    assert result["char_count_en"] == len(en)
    assert result["char_count_fr"] == len(fr)
    # EN and FR are genuinely different renderings, not a copy of one language.
    assert en != fr


@pytest.mark.asyncio
async def test_both_versions_persisted_to_clickhouse_analytics_in_process():
    """Both EN and FR land in risk_assessments_analytics via the real writer.

    Drives the real ``RiskAssessmentRepository`` ClickHouse path against an
    in-memory client that captures the exact insert the production
    clickhouse-connect client would receive.

    Validates: Requirements 20.2, 20.4
    """
    en, fr, _ = await _generate_bilingual_explanation()
    _assert_bilingual_bounds(en, fr)

    assessment = _build_assessment(en, fr)
    ch = InMemoryClickHouseClient()
    # pg_pool=None → PostgreSQL write skipped; only the ClickHouse path exercised.
    repo = RiskAssessmentRepository(pg_pool=None, clickhouse_client=ch)

    await repo.persist(assessment)

    analytics_rows = ch.rows_for(ANALYTICS_TABLE)
    assert len(analytics_rows) == 1, "exactly one analytics row expected"
    row = analytics_rows[0]

    # Both language columns present and correct in the analytics store (Req 20.4).
    assert "explanation_en" in row and "explanation_fr" in row
    assert row["explanation_en"] == en
    assert row["explanation_fr"] == fr
    # And they still satisfy the 500-char bound after persistence (Req 20.2).
    assert len(row["explanation_en"]) <= 500
    assert len(row["explanation_fr"]) <= 500
    # The row is keyed to the same claim record it explains.
    assert row["claim_id"] == str(assessment.claim_id)


# ===========================================================================
# testcontainers ClickHouse path (preferred when available; skipped in sandbox)
# ===========================================================================


def _testcontainers_clickhouse_available() -> tuple[bool, str]:
    """Report whether a real testcontainers ClickHouse path can run here.

    Requires the ``testcontainers`` package, the ``clickhouse-connect`` driver,
    AND a reachable container daemon (Docker/Podman). None of these are present
    in the CI sandbox, so this guard returns a clear skip reason instead of
    failing at collection or run time.
    """
    try:
        import testcontainers  # noqa: F401
        from testcontainers.clickhouse import ClickHouseContainer  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - ImportError or missing submodule
        return False, f"testcontainers[clickhouse] not installed ({exc})"

    try:
        import clickhouse_connect  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return False, f"clickhouse-connect not installed ({exc})"

    docker_host = os.environ.get("DOCKER_HOST")
    default_sock = "/var/run/docker.sock"
    if not docker_host and not os.path.exists(default_sock):
        return False, "no container daemon (Docker/Podman) available"
    return True, ""


_CH_AVAILABLE, _CH_SKIP_REASON = _testcontainers_clickhouse_available()


# DDL for the analytics table under test. Mirrors the design's MergeTree table
# (design.md — ClickHouse Schema Overview) with the explicit columns the writer
# in ensemble-scorer/repository.py inserts.
_CREATE_ANALYTICS_TABLE = """
CREATE TABLE IF NOT EXISTS risk_assessments_analytics (
    assessment_id   String,
    claim_id        String,
    risk_score      Int32,
    risk_decision   String,
    shap_base_value Float64,
    explanation_en  String,
    explanation_fr  String,
    model_version   String,
    scored_at       DateTime
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(scored_at)
ORDER BY (scored_at, claim_id)
"""

_CREATE_SHAP_TABLE = """
CREATE TABLE IF NOT EXISTS shap_attributions (
    assessment_id String,
    claim_id      String,
    indicator_id  String,
    feature_name  String,
    contribution  Float64,
    base_value    Float64,
    scored_at     DateTime
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(scored_at)
ORDER BY (scored_at, claim_id, indicator_id)
"""


@pytest.mark.integration
@pytest.mark.skipif(
    not _CH_AVAILABLE, reason=_CH_SKIP_REASON or "testcontainers ClickHouse unavailable"
)
class TestBilingualOutputWithClickHouseContainer:
    """End-to-end bilingual persistence over a real ClickHouse via testcontainers.

    Preferred when a container daemon is available. In the sandbox this whole
    class is skipped with a clear reason; the in-process tests above provide
    equivalent coverage without external services.
    """

    @pytest.mark.asyncio
    async def test_bilingual_explanation_stored_and_readable_in_clickhouse(self):
        """Submit a claim that triggers an LLM explanation, persist both EN + FR
        to ClickHouse ``risk_assessments_analytics``, and read them back.

        Validates: Requirements 20.2, 20.4
        """
        import clickhouse_connect  # local import (guarded)
        from testcontainers.clickhouse import ClickHouseContainer  # guarded

        with ClickHouseContainer() as clickhouse:
            client = clickhouse_connect.get_client(
                host=clickhouse.get_container_host_ip(),
                port=int(clickhouse.get_exposed_port(8123)),
                username=clickhouse.username,
                password=clickhouse.password,
                database=clickhouse.dbname,
            )
            client.command(_CREATE_ANALYTICS_TABLE)
            client.command(_CREATE_SHAP_TABLE)

            # 1) LLM_Service produces the bilingual explanation (Req 20.2).
            en, fr, _ = await _generate_bilingual_explanation()
            _assert_bilingual_bounds(en, fr)

            # 2) Persist through the REAL repository ClickHouse writer (Req 20.4).
            assessment = _build_assessment(en, fr)
            repo = RiskAssessmentRepository(
                pg_pool=None, clickhouse_client=client
            )
            await repo.persist(assessment)

            # 3) Read both language columns back from the analytics store.
            result = client.query(
                "SELECT explanation_en, explanation_fr "
                "FROM risk_assessments_analytics WHERE claim_id = {cid:String}",
                parameters={"cid": str(assessment.claim_id)},
            )
            rows = result.result_rows
            assert len(rows) == 1, "exactly one persisted analytics row expected"
            stored_en, stored_fr = rows[0]

            assert stored_en == en
            assert stored_fr == fr
            assert stored_en and stored_en.strip()
            assert stored_fr and stored_fr.strip()
            assert len(stored_en) <= 500
            assert len(stored_fr) <= 500
