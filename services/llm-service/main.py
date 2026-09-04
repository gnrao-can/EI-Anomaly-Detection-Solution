"""
services/llm-service/main.py
============================
FastAPI application for the EI-ADS LLM Service.

Endpoints
---------
* ``GET  /healthz``        — liveness/readiness probe (never blocks on model load)
* ``POST /llm/similarity`` — cosine similarity of two narratives in ``[0, 1]``

The narrative-similarity model and the LangChain/Llama chain are wired **lazily**
(constructed on first use, cached thereafter) so that process startup — and the
``/healthz`` probe Kubernetes hits immediately — never blocks on loading the
multi-gigabyte Llama 3.1 GGUF weights or the sentence-transformers model.

Endpoints
---------
* ``GET  /healthz``        — liveness/readiness probe (never blocks on model load)
* ``POST /llm/similarity`` — cosine similarity of two narratives in ``[0, 1]``
* ``POST /llm/explain``    — bilingual (EN/FR) risk explanation for a scored claim

Requirements: 3.5, 6.6, 9.4, 9.5, 9.9, 20.2, 20.3
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Optional

from config import get_settings
from similarity import NarrativeSimilarity

try:
    from fastapi import Depends, FastAPI, HTTPException, status
    from pydantic import BaseModel, Field

    from shared.auth import Role, require_role
    from shared.models import RiskDecision

    _HAS_FASTAPI = True
except ImportError:  # pragma: no cover - only in a bare sandbox without FastAPI
    _HAS_FASTAPI = False


# ---------------------------------------------------------------------------
# Lazy singletons — built on first request, not at import/startup time
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_similarity_scorer() -> NarrativeSimilarity:
    """Return a process-wide ``NarrativeSimilarity`` instance (lazy)."""
    settings = get_settings()
    return NarrativeSimilarity(model_name=settings.embedding_model_name)


@lru_cache(maxsize=1)
def get_chain():  # -> LLMChain (import kept local to stay lazy)
    """Build and cache the LangChain/Llama chain on first use."""
    from llm_chain import build_llm_chain

    return build_llm_chain(get_settings())


@lru_cache(maxsize=1)
def get_explanation_generator():  # -> ExplanationGenerator (lazy)
    """Build and cache the bilingual ExplanationGenerator on first use."""
    from explanation import ExplanationGenerator

    settings = get_settings()
    return ExplanationGenerator(
        chain=get_chain(), timeout_s=settings.explanation_timeout_s
    )


if _HAS_FASTAPI:

    class SimilarityRequest(BaseModel):
        """Request body for ``POST /llm/similarity``."""

        narrative_a: str = Field(..., description="First narrative to compare.")
        narrative_b: str = Field(..., description="Second narrative to compare.")

    class SimilarityResponse(BaseModel):
        """Response body: cosine similarity in the closed interval ``[0, 1]``."""

        similarity: float = Field(..., ge=0.0, le=1.0)

    class ExplainRequest(BaseModel):
        """Request body for ``POST /llm/explain``.

        ``indicators`` accepts the ``Indicator.to_dict()`` shape produced by the
        Ensemble Scorer; only the fields needed for ranking and templating are
        required, so extra keys are ignored.
        """

        indicators: list[dict[str, Any]] = Field(default_factory=list)
        risk_score: int = Field(..., ge=0, le=100)
        risk_decision: str = Field(...)

    class ExplainResponse(BaseModel):
        """Response body: bilingual explanation plus per-language word counts."""

        en: str
        fr: str
        word_count_en: int
        word_count_fr: int
        flag: Optional[str] = None

    app = FastAPI(
        title="EI-ADS LLM Service",
        description="Narrative similarity and bilingual explanation generation.",
        version="0.1.0",
    )

    # --- Prometheus metrics + /metrics endpoint (task 31.1) ---
    from shared.metrics import instrument_app  # noqa: E402

    instrument_app(app, kind="service")
    # --- end metrics wiring ---

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Liveness probe. Returns 200 without touching heavy model backends."""
        return {"status": "ok", "service": get_settings().service_name}

    @app.post("/llm/similarity", response_model=SimilarityResponse)
    def similarity(
        request: SimilarityRequest,
        _user: dict[str, Any] = Depends(require_role(Role.INVESTIGATOR)),
    ) -> SimilarityResponse:
        """Embed two narratives and return their cosine similarity in ``[0, 1]``."""
        scorer = get_similarity_scorer()
        score = scorer.similarity(request.narrative_a, request.narrative_b)
        return SimilarityResponse(similarity=score)

    @app.post("/llm/explain", response_model=ExplainResponse)
    async def explain(
        request: ExplainRequest,
        _user: dict[str, Any] = Depends(require_role(Role.INVESTIGATOR)),
    ) -> ExplainResponse:
        """Generate a bilingual (EN/FR) explanation for a scored claim.

        Ranks the supplied indicators by ``|SHAP contribution|``, cites the top
        three in a chain-of-thought prompt, enforces a per-language 500-character
        limit and a 5-second LLM timeout, and falls back to deterministic
        templates on timeout or a malformed LLM response.
        """
        from explanation import build_indicators_from_payload

        # ``risk_decision`` and the indicator payloads come from the client;
        # invalid enum values or malformed indicator dicts are client errors, so
        # surface a structured 422 instead of letting the enum/parse raise a 500.
        try:
            indicators = build_indicators_from_payload(request.indicators)
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Malformed indicator payload: {exc}",
            ) from exc

        valid_decisions = {d.value for d in RiskDecision}
        if request.risk_decision not in valid_decisions:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Invalid risk_decision {request.risk_decision!r}; "
                    f"expected one of {sorted(valid_decisions)}."
                ),
            )

        generator = get_explanation_generator()
        result = await generator.generate(
            indicators=indicators,
            risk_score=request.risk_score,
            risk_decision=request.risk_decision,
        )
        return ExplainResponse(**result)

else:  # pragma: no cover - executed only when FastAPI is not installed

    app = None


# ── OpenTelemetry distributed tracing (shared.telemetry, task 32.1 / Req 21.5) ──
# Single, self-contained hook: all tracer setup and span helpers live in
# shared/telemetry.py. ``app`` may be None here (bare sandbox); instrument_tracing
# tolerates that. No-op when the OpenTelemetry packages are absent.
from shared.telemetry import instrument_tracing  # noqa: E402

instrument_tracing(app, service_name="llm-service")
# ── end OpenTelemetry tracing ──
