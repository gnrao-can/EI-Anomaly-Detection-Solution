"""
tests/unit/test_similarity.py
=============================
Unit tests for the LLM Service narrative-similarity component.

These tests exercise the deterministic hashing fallback path (``use_fallback=
True``) so they run in any environment without ``sentence-transformers`` or the
multi-gigabyte embedding weights. They verify the contract properties every
caller depends on:

    - identical non-empty narratives  → similarity ~= 1.0
    - token-disjoint narratives        → low similarity
    - symmetric: sim(a, b) == sim(b, a)
    - bounded in the closed interval [0.0, 1.0]

The ``POST /llm/similarity`` endpoint is covered end-to-end via FastAPI's
``TestClient`` when FastAPI is installed; otherwise that class is skipped.

Requirements: 3.5, 6.6
"""

from __future__ import annotations

import os
import sys

import pytest

# The LLM Service modules import each other by bare module name
# (``from config import ...``), matching how they run inside the container.
# Put the service directory on sys.path so those imports resolve here.
_SERVICE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "services", "llm-service")
)
if _SERVICE_DIR not in sys.path:
    sys.path.insert(0, _SERVICE_DIR)

from similarity import NarrativeSimilarity  # noqa: E402


@pytest.fixture()
def scorer() -> NarrativeSimilarity:
    """A similarity scorer forced onto the deterministic fallback backend."""
    return NarrativeSimilarity(use_fallback=True)


# ===========================================================================
# Backend selection
# ===========================================================================


class TestBackendSelection:
    def test_forced_fallback_reports_fallback_backend(self, scorer: NarrativeSimilarity):
        assert scorer.using_fallback is True
        assert scorer.backend == "hashing-fallback"


# ===========================================================================
# Identical narratives → ~1.0
# ===========================================================================


class TestIdentical:
    def test_identical_strings_score_one(self, scorer: NarrativeSimilarity):
        text = "Shortage of work at the sawmill led to a temporary layoff."
        assert scorer.similarity(text, text) == pytest.approx(1.0)

    def test_identical_ignoring_case_and_whitespace(self, scorer: NarrativeSimilarity):
        a = "Laid off due to lack of work"
        b = "  laid OFF due to LACK of work  "
        # Same token multiset after case-folding/whitespace-stripping → 1.0.
        assert scorer.similarity(a, b) == pytest.approx(1.0)

    def test_both_empty_returns_one(self, scorer: NarrativeSimilarity):
        assert scorer.similarity("", "") == pytest.approx(1.0)
        assert scorer.similarity("   ", "\n\t ") == pytest.approx(1.0)


# ===========================================================================
# Disjoint narratives → low
# ===========================================================================


class TestDisjoint:
    def test_token_disjoint_strings_score_zero(self, scorer: NarrativeSimilarity):
        a = "quit voluntarily seeking better opportunity"
        b = "dismissed misconduct workplace policy breach"
        assert scorer.similarity(a, b) == pytest.approx(0.0)

    def test_disjoint_is_much_lower_than_identical(self, scorer: NarrativeSimilarity):
        a = "seasonal fishing employment ended for the winter season"
        b = "corporate restructuring eliminated the entire finance department"
        disjoint = scorer.similarity(a, b)
        identical = scorer.similarity(a, a)
        assert disjoint < identical
        assert disjoint < 0.25

    def test_empty_vs_nonempty_returns_zero(self, scorer: NarrativeSimilarity):
        assert scorer.similarity("", "some text here") == pytest.approx(0.0)
        assert scorer.similarity("some text here", "   ") == pytest.approx(0.0)


# ===========================================================================
# Symmetry
# ===========================================================================


class TestSymmetry:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("plant closure permanent layoff", "temporary shortage of work"),
            ("resigned for personal reasons", "resigned for personal reasons soon"),
            ("A", "B"),
            ("shared words between the two", "the two shared several words indeed"),
        ],
    )
    def test_similarity_is_symmetric(self, scorer: NarrativeSimilarity, a: str, b: str):
        assert scorer.similarity(a, b) == scorer.similarity(b, a)


# ===========================================================================
# Boundedness in [0, 1]
# ===========================================================================


class TestBounded:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("hello world", "hello world"),
            ("hello world", "goodbye moon"),
            ("hello world", "hello there world people"),
            ("", ""),
            ("", "nonempty"),
            ("a b c d e f g", "a b c"),
            ("repeat repeat repeat", "repeat"),
        ],
    )
    def test_similarity_within_unit_interval(
        self, scorer: NarrativeSimilarity, a: str, b: str
    ):
        score = scorer.similarity(a, b)
        assert 0.0 <= score <= 1.0

    def test_partial_overlap_is_between_zero_and_one(self, scorer: NarrativeSimilarity):
        a = "layoff due to seasonal work shortage"
        b = "seasonal work resumed after the shortage"
        score = scorer.similarity(a, b)
        assert 0.0 < score < 1.0


# ===========================================================================
# Endpoint (TestClient) — skipped if FastAPI / httpx TestClient unavailable
# ===========================================================================

# FastAPI's TestClient additionally needs httpx; guard against both being
# absent so the endpoint suite skips cleanly in a bare or partial sandbox.
try:
    from fastapi.testclient import TestClient as _TestClient

    _TESTCLIENT_AVAILABLE = True
    _TESTCLIENT_SKIP_REASON = ""
except Exception as _exc:  # noqa: BLE001 - RuntimeError when httpx is missing, etc.
    _TESTCLIENT_AVAILABLE = False
    _TESTCLIENT_SKIP_REASON = f"FastAPI TestClient unavailable: {_exc}"


@pytest.mark.skipif(not _TESTCLIENT_AVAILABLE, reason=_TESTCLIENT_SKIP_REASON)
class TestSimilarityEndpoint:
    def _main(self):
        # Load the llm-service ``main`` under a collision-free namespaced key so
        # a bare ``import main`` from another service's test file can't shadow it
        # when the whole unit suite runs together (see tests/conftest.py).
        from tests.conftest import load_service_module  # noqa: PLC0415

        return load_service_module(
            "llm-service", "main", siblings=["config", "similarity"]
        )

    def _client(self):
        main = self._main()

        # Task 29.1: /llm/similarity now requires the INVESTIGATOR role; inject
        # an authenticated principal so this test exercises the handler.
        from shared.auth import get_current_user  # noqa: PLC0415

        main.app.dependency_overrides[get_current_user] = lambda: {
            "roles": ["INVESTIGATOR"]
        }
        return _TestClient(main.app)

    def teardown_method(self):
        self._main().app.dependency_overrides.clear()

    def test_healthz_returns_ok(self):
        client = self._client()
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_similarity_identical_is_one(self):
        client = self._client()
        payload = {"narrative_a": "same text", "narrative_b": "same text"}
        resp = client.post("/llm/similarity", json=payload)
        assert resp.status_code == 200
        assert resp.json()["similarity"] == pytest.approx(1.0)

    def test_similarity_disjoint_is_low(self):
        client = self._client()
        payload = {"narrative_a": "alpha beta gamma", "narrative_b": "delta epsilon"}
        resp = client.post("/llm/similarity", json=payload)
        assert resp.status_code == 200
        assert resp.json()["similarity"] < 0.25

    def test_similarity_response_bounded(self):
        client = self._client()
        payload = {"narrative_a": "shared words here", "narrative_b": "shared words too"}
        resp = client.post("/llm/similarity", json=payload)
        assert resp.status_code == 200
        score = resp.json()["similarity"]
        assert 0.0 <= score <= 1.0
