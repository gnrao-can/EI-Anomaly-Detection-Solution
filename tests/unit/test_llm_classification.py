"""
tests/unit/test_llm_classification.py
======================================
Unit tests for the LLM Service narrative CLASSIFICATION endpoints + logic:

  * POST /llm/narrative-contradiction (Req 6.6) — {contradicted, evidence, source}
  * POST /llm/style-analysis          (Req 7.8) — {inconsistent, score, source}

The deterministic (StubLLM) path is exercised here — no llama/torch. The pure
``classification`` functions are tested directly (always run), and the FastAPI
endpoints are tested via TestClient (skipped when FastAPI/httpx are absent).

Requirements: 6.6, 7.8, 19.7
"""

from __future__ import annotations

import pytest

from tests.conftest import load_service_module

# Load the llm-service modules collision-free. The endpoints lazily import
# ``classification`` and ``llm_chain``, so include them as siblings.
_SIBLINGS = [
    "config",
    "similarity",
    "classification",
    "llm_chain",
    "templates",
    "explanation",
    "main",
]

_classification = load_service_module(
    "llm-service", "classification", siblings=_SIBLINGS
)
_similarity = load_service_module("llm-service", "similarity", siblings=_SIBLINGS)

classify_contradiction = _classification.classify_contradiction
classify_style_inconsistency = _classification.classify_style_inconsistency
NarrativeSimilarity = _similarity.NarrativeSimilarity


def _sim():
    # Force the deterministic hashing backend (no sentence-transformers needed).
    return NarrativeSimilarity(use_fallback=True)


# ===========================================================================
# Contradiction classifier (Req 6.6) — pure function, deterministic path
# ===========================================================================


class TestClassifyContradiction:
    def test_separation_narrative_vs_continued_employment_is_contradicted(self):
        result = classify_contradiction(
            "I was laid off due to shortage of work on my last day.",
            "CRA records show continued employment on active payroll through the period.",
            chain=None,
            similarity=_sim(),
        )
        assert result.contradicted is True
        assert result.source == "deterministic"
        assert result.evidence

    def test_consistent_narrative_not_contradicted(self):
        result = classify_contradiction(
            "I was laid off due to shortage of work.",
            "CRA records confirm the employment ended on the separation date.",
            chain=None,
            similarity=_sim(),
        )
        assert result.contradicted is False

    def test_empty_cra_evidence_not_contradicted(self):
        result = classify_contradiction(
            "I was terminated.", "", chain=None, similarity=_sim()
        )
        assert result.contradicted is False


# ===========================================================================
# Style-inconsistency classifier (Req 7.8) — pure function, deterministic path
# ===========================================================================


class TestClassifyStyleInconsistency:
    def test_narrative_unlike_corpus_is_inconsistent(self):
        # A narrative sharing no tokens with any prior ROE → max similarity ~0 →
        # inconsistent, with a high inconsistency score.
        result = classify_style_inconsistency(
            "zzz qqq xxx unheard vocabulary",
            [
                "shortage of work layoff",
                "end of seasonal contract",
                "position eliminated restructuring",
            ],
            similarity=_sim(),
            chain=None,
        )
        assert result.inconsistent is True
        assert result.score > 0.5
        assert result.source == "deterministic"

    def test_narrative_matching_corpus_is_consistent(self):
        # Identical to a prior ROE → max similarity 1.0 → not inconsistent.
        result = classify_style_inconsistency(
            "shortage of work layoff",
            ["shortage of work layoff", "end of seasonal contract"],
            similarity=_sim(),
            chain=None,
        )
        assert result.inconsistent is False
        assert result.score == pytest.approx(0.0, abs=1e-6)

    def test_empty_corpus_scores_zero_not_inconsistent(self):
        result = classify_style_inconsistency(
            "any narrative", [], similarity=_sim(), chain=None
        )
        assert result.inconsistent is False
        assert result.score == 0.0


# ===========================================================================
# Endpoints (TestClient) — skipped if FastAPI / httpx TestClient unavailable
# ===========================================================================

try:
    from fastapi.testclient import TestClient as _TestClient

    _TESTCLIENT_AVAILABLE = True
    _SKIP_REASON = ""
except Exception as _exc:  # noqa: BLE001
    _TESTCLIENT_AVAILABLE = False
    _SKIP_REASON = f"FastAPI TestClient unavailable: {_exc}"


@pytest.mark.skipif(not _TESTCLIENT_AVAILABLE, reason=_SKIP_REASON)
class TestClassificationEndpoints:
    def _main(self):
        return load_service_module("llm-service", "main", siblings=_SIBLINGS)

    def _client(self):
        main = self._main()
        from shared.auth import get_current_user

        main.app.dependency_overrides[get_current_user] = lambda: {
            "roles": ["INVESTIGATOR"]
        }
        return _TestClient(main.app)

    def teardown_method(self):
        self._main().app.dependency_overrides.clear()

    def test_narrative_contradiction_endpoint(self):
        client = self._client()
        resp = client.post(
            "/llm/narrative-contradiction",
            json={
                "narrative": "I was laid off due to shortage of work.",
                "cra_evidence": "CRA shows continued employment on active payroll.",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["contradicted"] is True
        assert body["source"] == "deterministic"
        assert body["evidence"]

    def test_style_analysis_endpoint(self):
        client = self._client()
        resp = client.post(
            "/llm/style-analysis",
            json={
                "narrative": "zzz qqq xxx unheard vocabulary",
                "corpus": [
                    "shortage of work layoff",
                    "end of seasonal contract",
                    "position eliminated",
                ],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["inconsistent"] is True
        assert 0.0 <= body["score"] <= 1.0

    def test_endpoints_require_auth(self):
        """Without an INVESTIGATOR principal the endpoints reject (Req 19.7)."""
        main = self._main()
        main.app.dependency_overrides.clear()  # no auth override
        client = _TestClient(main.app)
        r1 = client.post(
            "/llm/narrative-contradiction",
            json={"narrative": "x", "cra_evidence": "y"},
        )
        r2 = client.post("/llm/style-analysis", json={"narrative": "x", "corpus": []})
        assert r1.status_code in (401, 403)
        assert r2.status_code in (401, 403)
