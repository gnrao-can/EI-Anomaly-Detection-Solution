"""
tests/unit/test_explanation.py
==============================
Unit tests for the LLM Service bilingual explanation generation (task 8.2).

These tests run entirely on the deterministic ``StubLLM`` / template fallback
path, so they need neither the multi-gigabyte Llama weights nor network access.
They verify the behaviours the design and task require:

    - Top-3 indicator selection ranked by |SHAP contribution| descending
    - 500-character enforcement per language (word-boundary truncation)
    - LLM timeout -> EXPLANATION_UNAVAILABLE flag (slow mock LLM)
    - Malformed / empty LLM response -> template fallback (EN and FR populated)
    - Correct word counts in the result

The ``POST /llm/explain`` endpoint is exercised via FastAPI's ``TestClient``
when FastAPI + httpx are available; otherwise that class skips cleanly.

Requirements: 9.4, 9.5, 9.9, 20.2, 20.3
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

import pytest

# The LLM Service modules import each other by bare module name
# (``from config import ...``); put the service dir on sys.path so they resolve.
_SERVICE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "services", "llm-service")
)
if _SERVICE_DIR not in sys.path:
    sys.path.insert(0, _SERVICE_DIR)

from shared.models import EngineType, Indicator, RiskDecision, Severity  # noqa: E402

import explanation as explanation_mod  # noqa: E402
from explanation import (  # noqa: E402
    EXPLANATION_UNAVAILABLE,
    MAX_CHARS_PER_LANGUAGE,
    ExplanationGenerator,
    build_indicators_from_payload,
    count_words,
    rank_top_indicators,
    truncate_at_word_boundary,
)
from llm_chain import LLMChain  # noqa: E402


# ===========================================================================
# Helpers / test doubles
# ===========================================================================


def make_indicator(
    engine: EngineType = EngineType.REPETITIVE_PATTERN,
    severity: Severity = Severity.HIGH,
    title: str = "Test indicator",
    shap: float | None = 0.0,
) -> Indicator:
    return Indicator(
        indicator_id=uuid.uuid4(),
        claim_id=uuid.uuid4(),
        engine=engine,
        severity=severity,
        algorithm="unit_test",
        title=title,
        explanation_en="",
        explanation_fr="",
        payload={},
        produced_at=datetime.now(timezone.utc),
        shap_contribution=shap,
    )


class _FakeLLM:
    """LLM stand-in with a controllable response and optional delay."""

    def __init__(self, response: str = "", delay_s: float = 0.0) -> None:
        self.response = response
        self.delay_s = delay_s
        self.last_prompt: str | None = None

    def invoke(self, prompt: str) -> str:
        self.last_prompt = prompt
        if self.delay_s:
            import time

            time.sleep(self.delay_s)
        return self.response


def make_chain(response: str = "", delay_s: float = 0.0) -> LLMChain:
    return LLMChain(llm=_FakeLLM(response=response, delay_s=delay_s), is_stub=True)


def run(coro):
    return asyncio.run(coro)


# ===========================================================================
# Top-3 selection by |SHAP|
# ===========================================================================


class TestRanking:
    def test_selects_top_three_by_absolute_shap(self):
        inds = [
            make_indicator(title="a", shap=0.1),
            make_indicator(title="b", shap=-0.9),  # largest magnitude
            make_indicator(title="c", shap=0.5),
            make_indicator(title="d", shap=-0.3),
            make_indicator(title="e", shap=0.0),
        ]
        top = rank_top_indicators(inds)
        assert [i.title for i in top] == ["b", "c", "d"]

    def test_negative_contributions_ranked_by_magnitude(self):
        inds = [
            make_indicator(title="small", shap=-0.05),
            make_indicator(title="big", shap=-0.95),
        ]
        top = rank_top_indicators(inds)
        assert top[0].title == "big"

    def test_none_contribution_treated_as_zero(self):
        inds = [
            make_indicator(title="none", shap=None),
            make_indicator(title="pos", shap=0.4),
        ]
        top = rank_top_indicators(inds)
        assert top[0].title == "pos"

    def test_ties_broken_by_arrival_order(self):
        inds = [
            make_indicator(title="first", shap=0.5),
            make_indicator(title="second", shap=0.5),
            make_indicator(title="third", shap=0.5),
        ]
        top = rank_top_indicators(inds)
        assert [i.title for i in top] == ["first", "second", "third"]

    def test_fewer_than_three_returns_all(self):
        inds = [make_indicator(title="only", shap=0.2)]
        assert len(rank_top_indicators(inds)) == 1


# ===========================================================================
# 500-character enforcement / truncation
# ===========================================================================


class TestTruncation:
    def test_short_text_unchanged(self):
        assert truncate_at_word_boundary("hello world") == "hello world"

    def test_truncates_at_word_boundary(self):
        text = "word " * 200  # 1000 chars
        out = truncate_at_word_boundary(text)
        assert len(out) <= MAX_CHARS_PER_LANGUAGE
        # No trailing partial word / no dangling space.
        assert not out.endswith(" ")
        assert " word" in out or out.startswith("word")

    def test_single_long_token_hard_cut(self):
        text = "x" * 800
        out = truncate_at_word_boundary(text)
        assert len(out) == MAX_CHARS_PER_LANGUAGE

    def test_generate_enforces_limit_per_language(self):
        # A chain returning JSON with over-long EN/FR strings.
        long_en = "alpha " * 300
        long_fr = "bravo " * 300
        import json

        chain = make_chain(response=json.dumps({"en": long_en, "fr": long_fr}))
        gen = ExplanationGenerator(chain=chain)
        result = run(gen.generate([make_indicator()], 55, RiskDecision.FLAG_FOR_REVIEW))
        assert len(result["en"]) <= MAX_CHARS_PER_LANGUAGE
        assert len(result["fr"]) <= MAX_CHARS_PER_LANGUAGE


# ===========================================================================
# Timeout -> EXPLANATION_UNAVAILABLE
# ===========================================================================


class TestTimeout:
    def test_slow_llm_triggers_unavailable_flag(self):
        # Chain sleeps 1s; generator timeout is 0.05s.
        chain = make_chain(response='{"en":"x","fr":"y"}', delay_s=1.0)
        gen = ExplanationGenerator(chain=chain, timeout_s=0.05)
        result = run(
            gen.generate([make_indicator(title="Repeat claims")], 80, RiskDecision.INVESTIGATE)
        )
        assert result.get("flag") == EXPLANATION_UNAVAILABLE
        # Even on timeout, both languages are populated via the template fallback.
        assert result["en"].strip()
        assert result["fr"].strip()
        assert result["word_count_en"] > 0
        assert result["word_count_fr"] > 0

    def test_fast_llm_has_no_flag(self):
        import json

        chain = make_chain(response=json.dumps({"en": "ok en", "fr": "ok fr"}))
        gen = ExplanationGenerator(chain=chain, timeout_s=5.0)
        result = run(gen.generate([make_indicator()], 10, RiskDecision.AUTO_APPROVE))
        assert "flag" not in result
        assert result["en"] == "ok en"
        assert result["fr"] == "ok fr"


# ===========================================================================
# Malformed / empty response -> template fallback
# ===========================================================================


class TestTemplateFallback:
    def test_empty_response_falls_back_to_template(self):
        chain = make_chain(response="")
        gen = ExplanationGenerator(chain=chain)
        ind = make_indicator(
            engine=EngineType.GEOGRAPHIC_ANOMALY, title="500km commute"
        )
        result = run(gen.generate([ind], 72, RiskDecision.INVESTIGATE))
        assert result["en"].strip()
        assert result["fr"].strip()
        # Template mentions the indicator title in both languages.
        assert "500km commute" in result["en"]
        assert "500km commute" in result["fr"]

    def test_non_json_prose_falls_back(self):
        chain = make_chain(response="I am unable to produce JSON right now, sorry.")
        gen = ExplanationGenerator(chain=chain)
        result = run(gen.generate([make_indicator()], 45, RiskDecision.FLAG_FOR_REVIEW))
        assert result["en"].strip()
        assert result["fr"].strip()

    def test_json_missing_fr_falls_back(self):
        chain = make_chain(response='{"en": "only english"}')
        gen = ExplanationGenerator(chain=chain)
        result = run(gen.generate([make_indicator()], 45, RiskDecision.FLAG_FOR_REVIEW))
        # fr missing -> whole thing is malformed -> template (both populated).
        assert result["fr"].strip()
        assert result["en"].strip()

    def test_stub_llm_default_chain_falls_back(self):
        # The default StubLLM never returns valid JSON -> template path.
        from llm_chain import StubLLM

        chain = LLMChain(llm=StubLLM(), is_stub=True)
        gen = ExplanationGenerator(chain=chain)
        result = run(gen.generate([make_indicator(title="pattern")], 60, RiskDecision.FLAG_FOR_REVIEW))
        assert "pattern" in result["en"]
        assert "pattern" in result["fr"]

    def test_no_indicators_template(self):
        chain = make_chain(response="")
        gen = ExplanationGenerator(chain=chain)
        result = run(gen.generate([], 0, RiskDecision.AUTO_APPROVE))
        assert result["en"].strip()
        assert result["fr"].strip()


# ===========================================================================
# Word counts
# ===========================================================================


class TestWordCounts:
    def test_word_counts_match_text(self):
        import json

        chain = make_chain(
            response=json.dumps({"en": "one two three", "fr": "un deux trois quatre"})
        )
        gen = ExplanationGenerator(chain=chain)
        result = run(gen.generate([make_indicator()], 50, RiskDecision.FLAG_FOR_REVIEW))
        assert result["word_count_en"] == 3
        assert result["word_count_fr"] == 4
        assert result["word_count_en"] == count_words(result["en"])
        assert result["word_count_fr"] == count_words(result["fr"])


# ===========================================================================
# Payload -> Indicator coercion
# ===========================================================================


class TestPayloadCoercion:
    def test_builds_from_full_payload(self):
        ind = make_indicator(shap=0.7)
        built = build_indicators_from_payload([ind.to_dict()])
        assert len(built) == 1
        assert built[0].shap_contribution == pytest.approx(0.7)
        assert built[0].engine == ind.engine

    def test_builds_from_partial_payload(self):
        built = build_indicators_from_payload(
            [{"engine": "FRAUD_RING", "severity": "CRITICAL", "title": "ring", "shap_contribution": -0.9}]
        )
        assert built[0].engine == EngineType.FRAUD_RING
        assert built[0].severity == Severity.CRITICAL
        assert built[0].title == "ring"

    def test_unknown_engine_defaults_gracefully(self):
        built = build_indicators_from_payload([{"engine": "NOPE", "title": "x"}])
        assert len(built) == 1  # does not raise

    def test_empty_payload_returns_empty(self):
        assert build_indicators_from_payload([]) == []


# ===========================================================================
# Endpoint (TestClient) — skipped if FastAPI / httpx TestClient unavailable
# ===========================================================================

try:
    from fastapi.testclient import TestClient as _TestClient

    _TESTCLIENT_AVAILABLE = True
    _TESTCLIENT_SKIP_REASON = ""
except Exception as _exc:  # noqa: BLE001
    _TESTCLIENT_AVAILABLE = False
    _TESTCLIENT_SKIP_REASON = f"FastAPI TestClient unavailable: {_exc}"


@pytest.mark.skipif(not _TESTCLIENT_AVAILABLE, reason=_TESTCLIENT_SKIP_REASON)
class TestExplainEndpoint:
    def _main(self):
        # Load the llm-service ``main`` under a collision-free namespaced key so
        # a bare ``import main`` from another service's test file cannot shadow
        # it when the whole unit suite runs together (see tests/conftest.py).
        from tests.conftest import load_service_module  # noqa: PLC0415

        return load_service_module(
            "llm-service", "main", siblings=["config", "similarity"]
        )

    def _client(self):
        main = self._main()

        # Task 29.1: /llm/explain now requires the INVESTIGATOR role; inject an
        # authenticated principal so this test exercises the handler.
        from shared.auth import get_current_user  # noqa: PLC0415

        main.app.dependency_overrides[get_current_user] = lambda: {
            "roles": ["INVESTIGATOR"]
        }
        return _TestClient(main.app)

    def teardown_method(self):
        self._main().app.dependency_overrides.clear()

    def test_explain_returns_bilingual_payload(self):
        client = self._client()
        payload = {
            "indicators": [
                {
                    "engine": "REGIONAL_ANALYSIS",
                    "severity": "MEDIUM",
                    "title": "low regional unemployment",
                    "shap_contribution": 0.4,
                }
            ],
            "risk_score": 55,
            "risk_decision": "FLAG_FOR_REVIEW",
        }
        resp = client.post("/llm/explain", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["en"].strip()
        assert body["fr"].strip()
        assert body["word_count_en"] >= 0
        assert body["word_count_fr"] >= 0
        assert len(body["en"]) <= MAX_CHARS_PER_LANGUAGE
        assert len(body["fr"]) <= MAX_CHARS_PER_LANGUAGE

    def test_explain_no_indicators(self):
        client = self._client()
        payload = {"indicators": [], "risk_score": 0, "risk_decision": "AUTO_APPROVE"}
        resp = client.post("/llm/explain", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["en"].strip()
        assert body["fr"].strip()
