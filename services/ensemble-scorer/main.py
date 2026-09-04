"""
services/ensemble-scorer/main.py
================================
FastAPI application for the Ensemble Scorer service.

Endpoints implemented in this task (7.1):
  - ``POST /score`` — accept a list of Indicators (as JSON, matching
    ``Indicator.to_dict()``) and return ``{risk_score, risk_decision}``.
  - ``GET /healthz`` — report model-loading status and liveness.

SHAP attribution and persistence to PostgreSQL / ClickHouse are wired in a
later task (7.2); this module focuses on scoring and the decision mapping.

Requirements: 9.1, 9.2
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, status

from shared.auth import Role, require_role
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

try:
    from shared.models import Indicator
except ImportError:  # pragma: no cover
    from models import Indicator  # type: ignore

try:  # intra-service sibling modules imported by bare name in the service dir
    from scorer import EnsembleScorer  # type: ignore
    from repository import RiskAssessmentRepository  # type: ignore
except ImportError:  # pragma: no cover - when imported as a package
    from .scorer import EnsembleScorer  # type: ignore
    from .repository import RiskAssessmentRepository  # type: ignore


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class ScoreRequest(BaseModel):
    """Inbound scoring request.

    ``indicators`` is a list of Indicator dicts using the canonical
    ``Indicator.to_dict()`` shape. An empty list is valid and yields a
    zero-score AUTO_APPROVE result.
    """

    indicators: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Indicator objects (Indicator.to_dict() shape).",
    )


class ScoreResponse(BaseModel):
    """Scoring result returned by ``POST /score``."""

    assessment_id: str
    claim_id: str
    risk_score: int = Field(..., ge=0, le=100)
    risk_decision: str
    shap_base_value: float
    shap_attributions: list[dict[str, Any]]
    model_version: str


# ---------------------------------------------------------------------------
# Lifespan — load models once at startup
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Instantiate the EnsembleScorer and repository at startup.

    The scorer loads its models (or heuristic fallback). The repository builds
    asyncpg / clickhouse-connect clients from environment settings; when those
    are unavailable it degrades to a no-op persistence path so the service still
    starts in development environments.
    """
    app.state.scorer = EnsembleScorer()
    app.state.repository = await RiskAssessmentRepository.create()
    try:
        yield
    finally:
        app.state.scorer = None
        app.state.repository = None


app = FastAPI(
    title="EI-ADS Ensemble Scorer Service",
    version="0.1.0",
    lifespan=lifespan,
)

# ── OpenTelemetry distributed tracing (shared.telemetry, task 32.1 / Req 21.5) ──
# Single, self-contained hook: all tracer setup and span helpers live in
# shared/telemetry.py. No-op when the OpenTelemetry packages are absent.
from shared.telemetry import instrument_tracing  # noqa: E402

instrument_tracing(app, service_name="ensemble-scorer")
# ── end OpenTelemetry tracing ──

# --- Prometheus metrics + /metrics endpoint (task 31.1) ---
from shared.metrics import instrument_app  # noqa: E402

instrument_app(app, kind="engine")
# --- end metrics wiring ---


def _get_scorer(app_: FastAPI) -> EnsembleScorer:
    scorer = getattr(app_.state, "scorer", None)
    if scorer is None:
        scorer = EnsembleScorer()
        app_.state.scorer = scorer
    return scorer


@app.post("/score", response_model=ScoreResponse)
async def score(
    request: ScoreRequest,
    _user: dict[str, Any] = Depends(require_role(Role.INVESTIGATOR)),
) -> ScoreResponse:
    """Score a claim: compute Risk_Score, SHAP attribution, and persist.

    Builds a full RiskAssessment (with additivity-checked SHAP attributions),
    persists it to PostgreSQL + ClickHouse via the repository, and returns the
    score, decision, and attribution summary. The bilingual explanation fields
    are populated later by the LLM service.
    """
    # Each indicator dict must match the canonical Indicator.to_dict() shape.
    # A malformed entry (missing key, bad UUID/enum value) is a client error, so
    # surface a structured 422 rather than letting from_dict() raise a 500.
    try:
        indicators = [Indicator.from_dict(d) for d in request.indicators]
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Malformed indicator payload: {exc}",
        ) from exc
    scorer = _get_scorer(app)
    assessment = scorer.assess(indicators)

    repository = getattr(app.state, "repository", None)
    if repository is not None:
        await repository.persist(assessment)

    return ScoreResponse(
        assessment_id=str(assessment.assessment_id),
        claim_id=str(assessment.claim_id),
        risk_score=assessment.risk_score,
        risk_decision=assessment.risk_decision.value,
        shap_base_value=assessment.shap_base_value,
        shap_attributions=[a.to_dict() for a in assessment.shap_attributions],
        model_version=assessment.model_version,
    )


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Report liveness and which scoring mode is active."""
    scorer = _get_scorer(app)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "ok",
            "model_version": scorer.model_version,
            "using_fallback": scorer.using_fallback,
        },
    )


__all__ = ["app", "ScoreRequest", "ScoreResponse", "lifespan"]
