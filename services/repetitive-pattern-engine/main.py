"""
services/repetitive-pattern-engine/main.py
===========================================
FastAPI application for the Repetitive Pattern Engine (task 11.1).

Responsibilities:
  * ``POST /engine/analyze`` — accept a claim payload (``sin_hash``,
    ``employer_bn`` and either explicit ``claim_dates`` or a ``claim_id`` to
    look up), retrieve the claimant's 60-month claim history from PostgreSQL by
    ``sin_hash``, then run:
      - employer-repetition detection (>= 3 claims from the same employer BN in
        any 36-month window -> HIGH Indicator; Req 3.2), and
      - sequential pattern mining over the claim-date sequence (repeating
        inter-claim cadence within +/-14 days -> HIGH Indicator; Req 3.3, 3.8).
    The Indicator list is returned in the response.
      - On DB unavailable, a SINGLE DATA_UNAVAILABLE Indicator is produced and
        the endpoint returns WITHOUT running any further analysis (Req 3.1
        fallback).
  * ``GET /healthz`` — liveness/readiness.

The 60-month / 36-month windows and the +/-14-day tolerance come from
``config.py`` (see ``.env.example``). In dev/test the caller injects an
in-memory fake repository on ``app.state`` and no live database is required.

Note: hours-clustering and the async LLM narrative similarity check are task
11.2 and are intentionally NOT implemented here.

Requirements: 3.1, 3.2, 3.3, 3.8
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Any, Optional

from fastapi import Depends, FastAPI, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from shared.auth import Role, require_role
from shared.models import EngineType, Indicator, Severity

try:  # sibling modules imported by bare name in the service directory
    from config import get_settings  # type: ignore
    from hours_clustering import check_hours_clustering  # type: ignore
    from kafka_producer import IndicatorPublishError  # type: ignore
    from llm_client import (  # type: ignore
        LLMServiceClient,
        build_narrative_pairs,
    )
    from patterns import (  # type: ignore
        check_employer_repetition,
        sequential_pattern_mining,
    )
    from repository import (  # type: ignore
        ClaimHistoryRecord,
        ClaimHistoryRepository,
        ClaimHistoryUnavailable,
    )
except ImportError:  # pragma: no cover - when imported as a package
    from .config import get_settings  # type: ignore
    from .hours_clustering import check_hours_clustering  # type: ignore
    from .kafka_producer import IndicatorPublishError  # type: ignore
    from .llm_client import (  # type: ignore
        LLMServiceClient,
        build_narrative_pairs,
    )
    from .patterns import (  # type: ignore
        check_employer_repetition,
        sequential_pattern_mining,
    )
    from .repository import (  # type: ignore
        ClaimHistoryRecord,
        ClaimHistoryRepository,
        ClaimHistoryUnavailable,
    )


logger = logging.getLogger("repetitive-pattern-engine")


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class AnalyzeRequest(BaseModel):
    """Payload for ``POST /engine/analyze``.

    ``sin_hash`` keys the 60-month history lookup. ``employer_bn`` is the
    employer of the claim under analysis (included in employer-repetition
    grouping). ``claim_dates`` may carry explicit dates for the current claim
    when they are not yet persisted; when omitted the retrieved history drives
    the sequential analysis. ``as_of`` bounds the rolling window (defaults to
    today) and is primarily used by tests for determinism.
    """

    sin_hash: str
    claim_id: Optional[str] = None
    employer_bn: Optional[str] = None
    claim_dates: list[date] = Field(default_factory=list)
    as_of: Optional[date] = None
    # Current-claim fields used by the hours-clustering detector (Req 3.4).
    insurable_hours: Optional[float] = None
    economic_region_code: Optional[str] = None
    # Reason-for-separation narrative for the current claim; compared against
    # prior-claim narratives via the LLM Service (Req 3.5, 3.6).
    separation_narrative: Optional[str] = None
    # Optional explicit prior-claim narratives as ``(claim_id, text)`` pairs,
    # supplied when narratives are not carried on the history records.
    prior_narratives: list[dict[str, Any]] = Field(default_factory=list)


class AnalyzeResponse(BaseModel):
    """Response body: the produced Indicator list plus light metadata."""

    sin_hash: str
    claim_id: Optional[str] = None
    status: str  # "OK" | "DATA_UNAVAILABLE"
    history_count: int
    indicators: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Lifespan — wire up the repository (asyncpg pool in prod; fake in dev/test)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings

    # The repository is set on app.state. In dev/test the caller injects a fake
    # (FakeClaimHistoryRepository). In production a lifespan hook (or deployment
    # wiring) would create an asyncpg pool and a ClaimHistoryRepository; that is
    # left to the deployment layer and kept out of the request path here.
    if not hasattr(app.state, "repository"):
        app.state.repository = None
    if not hasattr(app.state, "pool"):
        app.state.pool = None

    # If a pool has been provided but no repository, build one.
    if app.state.repository is None and app.state.pool is not None:
        app.state.repository = ClaimHistoryRepository(
            app.state.pool, window_months=settings.history_window_months
        )

    # LLM Service client for narrative similarity. In dev/test the caller may
    # inject a fake client (or a fake transport) on app.state.llm_client; when
    # absent we build a default httpx-backed client from settings.
    if not hasattr(app.state, "llm_client"):
        app.state.llm_client = None
    if app.state.llm_client is None:
        app.state.llm_client = LLMServiceClient(
            settings.llm_service_url,
            timeout_seconds=settings.llm_timeout_seconds,
            similarity_threshold=settings.narrative_similarity_threshold,
        )

    # Kafka Indicator producer. In dev/test the caller injects a
    # FakeIndicatorProducer; in production the deployment layer wires an
    # aiokafka-backed IndicatorProducer. When absent, publishing is skipped.
    if not hasattr(app.state, "indicator_producer"):
        app.state.indicator_producer = None

    try:
        yield
    finally:
        pool = getattr(app.state, "pool", None)
        if pool is not None:
            close = getattr(pool, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result


app = FastAPI(
    title="EI-ADS Repetitive Pattern Engine",
    version="0.1.0",
    lifespan=lifespan,
)

# ── OpenTelemetry distributed tracing (shared.telemetry, task 32.1 / Req 21.5) ──
# Single, self-contained hook: all tracer setup and span helpers live in
# shared/telemetry.py. No-op when the OpenTelemetry packages are absent.
from shared.telemetry import instrument_tracing  # noqa: E402

instrument_tracing(app, service_name="repetitive-pattern-engine")
# ── end OpenTelemetry tracing ──

# --- Prometheus metrics + /metrics endpoint (task 31.1) ---
from shared.metrics import instrument_app  # noqa: E402

instrument_app(app, kind="engine")
# --- end metrics wiring ---


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _data_unavailable_indicator(claim_id: Optional[str], sin_hash: str) -> Indicator:
    """Build the single DATA_UNAVAILABLE Indicator emitted when the DB is down.

    Matches the design's Indicator generation rule: "Claim history DB
    unavailable -> DATA_UNAVAILABLE". Severity is UNRESOLVED (the analysis could
    not be completed).
    """
    return Indicator(
        indicator_id=uuid.uuid4(),
        claim_id=_coerce_uuid(claim_id),
        engine=EngineType.REPETITIVE_PATTERN,
        severity=Severity.UNRESOLVED,
        algorithm="claim_history_retrieval",
        title="Claim history data unavailable",
        explanation_en=(
            "The 60-month claim history store was unavailable, so repetitive "
            "pattern analysis could not be completed for this claim."
        ),
        explanation_fr=(
            "Le magasin d'historique des demandes sur 60 mois etait "
            "indisponible; l'analyse des schemas repetitifs n'a pas pu etre "
            "effectuee pour cette demande."
        ),
        payload={"reason": "DATA_UNAVAILABLE"},
        produced_at=datetime.now(timezone.utc),
    )


def _coerce_uuid(claim_id: Optional[str]) -> uuid.UUID:
    if isinstance(claim_id, uuid.UUID):
        return claim_id
    if claim_id:
        try:
            return uuid.UUID(str(claim_id))
        except (ValueError, AttributeError, TypeError):
            return uuid.uuid5(uuid.NAMESPACE_OID, str(claim_id))
    return uuid.uuid4()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.post(
    "/engine/analyze",
    responses={200: {"model": AnalyzeResponse}},
)
async def analyze(
    req: AnalyzeRequest,
    _user: dict[str, Any] = Depends(require_role(Role.INVESTIGATOR)),
) -> Any:
    """Run repetitive-pattern analysis for a claim.

    Retrieves the 60-month claim history by ``sin_hash``. On DB unavailability a
    single DATA_UNAVAILABLE Indicator is returned and NO further analysis runs.
    Otherwise, employer-repetition and sequential-pattern detectors run over the
    history (augmented with any explicit ``claim_dates`` / ``employer_bn`` from
    the current claim) and their Indicators are returned.
    """
    settings = getattr(app.state, "settings", None) or get_settings()
    repository = getattr(app.state, "repository", None)

    # ── Retrieve claim history; DB unavailable => single DATA_UNAVAILABLE ──
    if repository is None:
        logger.info("analyze_data_unavailable", extra={"reason": "no_repository"})
        indicator = _data_unavailable_indicator(req.claim_id, req.sin_hash)
        return AnalyzeResponse(
            sin_hash=req.sin_hash,
            claim_id=req.claim_id,
            status="DATA_UNAVAILABLE",
            history_count=0,
            indicators=[indicator.to_dict()],
        )

    try:
        history = await repository.fetch_history(req.sin_hash, as_of=req.as_of)
    except ClaimHistoryUnavailable:
        logger.info("analyze_data_unavailable", extra={"reason": "fetch_failed"})
        indicator = _data_unavailable_indicator(req.claim_id, req.sin_hash)
        return AnalyzeResponse(
            sin_hash=req.sin_hash,
            claim_id=req.claim_id,
            status="DATA_UNAVAILABLE",
            history_count=0,
            indicators=[indicator.to_dict()],
        )

    # ── Fold the current claim into the analysed set (if dates supplied) ──
    prior_records: list[ClaimHistoryRecord] = list(history)
    records: list[ClaimHistoryRecord] = list(history)
    current_bn = req.employer_bn
    for d in req.claim_dates:
        records.append(
            ClaimHistoryRecord(
                claim_id=req.claim_id or "current",
                employer_bn=current_bn or (records[0].employer_bn if records else ""),
                separation_date=d,
            )
        )

    # ── Run the non-LLM detectors under a 400 ms wall-clock budget (Req 3.7).
    # The LLM narrative call is explicitly EXCLUDED from this budget and runs
    # afterwards with its own 5 s timeout. If the synchronous detectors somehow
    # exceed the budget, we return whatever Indicators completed rather than
    # blocking past the deadline.
    budget_seconds = max(settings.analysis_budget_ms, 0) / 1000.0
    try:
        indicators = await asyncio.wait_for(
            _run_sync_detectors(settings, req, records, prior_records),
            timeout=budget_seconds if budget_seconds > 0 else None,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "analyze_budget_exceeded",
            extra={"budget_ms": settings.analysis_budget_ms},
        )
        indicators = []

    # ── LLM narrative similarity (async, own 5 s timeout, budget-excluded) ──
    indicators.extend(await _run_narrative_check(settings, req, prior_records))

    # ── Publish all Indicators to Kafka (Req 3.2, 3.7) ──
    published = 0
    producer = getattr(app.state, "indicator_producer", None)
    if producer is not None and indicators:
        try:
            published = await producer.publish_indicators(indicators)
        except IndicatorPublishError as exc:
            logger.warning("indicator_publish_failed", extra={"reason": repr(exc)})

    logger.info(
        "analyze_complete",
        extra={
            "history_count": len(history),
            "analysed_count": len(records),
            "indicator_count": len(indicators),
            "published_count": published,
        },
    )

    return AnalyzeResponse(
        sin_hash=req.sin_hash,
        claim_id=req.claim_id,
        status="OK",
        history_count=len(history),
        indicators=[i.to_dict() for i in indicators],
    )


async def _run_sync_detectors(
    settings: Any,
    req: "AnalyzeRequest",
    records: list["ClaimHistoryRecord"],
    prior_records: list["ClaimHistoryRecord"],
) -> list[Indicator]:
    """Run the deterministic (non-LLM) detectors and return their Indicators.

    Covers employer-repetition (Req 3.2), sequential-pattern mining (Req 3.3,
    3.8) and hours-clustering (Req 3.4). Pure CPU work wrapped in a coroutine so
    the caller can bound it with ``asyncio.wait_for`` (Req 3.7 wall-clock
    budget).
    """
    indicators: list[Indicator] = []

    indicators.extend(
        check_employer_repetition(
            records,
            window_months=settings.employer_repetition_window_months,
            threshold=settings.employer_repetition_threshold,
            claim_id=req.claim_id,
        )
    )
    indicators.extend(
        sequential_pattern_mining(
            [r.separation_date for r in records],
            tolerance_days=settings.interval_tolerance_days,
            claim_id=req.claim_id,
        )
    )

    # Hours-clustering: only when the current claim's insurable hours are known.
    if req.insurable_hours is not None:
        threshold = settings.min_qualifying_hours(req.economic_region_code)
        indicators.extend(
            check_hours_clustering(
                req.insurable_hours,
                prior_records,
                threshold=threshold,
                tolerance=settings.hours_cluster_tolerance,
                min_prior=settings.hours_cluster_min_prior,
                claim_id=req.claim_id,
                region_code=req.economic_region_code,
            )
        )

    return indicators


async def _run_narrative_check(
    settings: Any,
    req: "AnalyzeRequest",
    prior_records: list["ClaimHistoryRecord"],
) -> list[Indicator]:
    """Run the LLM narrative-similarity check (Req 3.5, 3.6).

    Compares the current claim's separation narrative against prior-claim
    narratives via the LLM Service. On timeout/failure a
    NARRATIVE_ANALYSIS_TIMEOUT Indicator is produced and the engine proceeds.
    Returns an empty list when there are no narratives to compare or no LLM
    client is wired up.
    """
    llm_client = getattr(app.state, "llm_client", None)
    if llm_client is None:
        return []

    # Assemble (claim_id, narrative) tuples: the current claim plus priors.
    narratives: list[tuple[Optional[str], str]] = []
    if req.separation_narrative:
        narratives.append((req.claim_id or "current", req.separation_narrative))
    for entry in req.prior_narratives:
        if isinstance(entry, dict):
            narratives.append(
                (entry.get("claim_id"), str(entry.get("narrative", "") or ""))
            )
    # Fold in any narratives carried on the history records themselves.
    for rec in prior_records:
        text = getattr(rec, "separation_narrative", None)
        if text:
            narratives.append((getattr(rec, "claim_id", None), str(text)))

    pairs = build_narrative_pairs(narratives)
    if not pairs:
        return []

    return await llm_client.check_narratives(pairs, claim_id=req.claim_id)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Liveness/readiness probe.

    Healthy when a claim-history repository is wired up (an in-memory fake in
    dev/test, or an asyncpg-backed repository in production).
    """
    healthy = getattr(app.state, "repository", None) is not None
    code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(
        status_code=code,
        content={"status": "ok" if healthy else "degraded"},
    )


__all__ = [
    "app",
    "AnalyzeRequest",
    "AnalyzeResponse",
    "lifespan",
    "analyze",
    "healthz",
    "_run_sync_detectors",
    "_run_narrative_check",
]
