"""
services/history-inconsistency-engine/main.py
=============================================
FastAPI application for the Employment History Inconsistency Engine (task 14.1).

Endpoints
---------
``POST /engine/analyze``
    Accept a claim payload (``sin_hash``, ``tax_year``, ``employer_bn``,
    ``roe_earnings``), fetch the matching CRA T4 via the CRA Adapter client
    (5-second timeout), then:
      - if the CRA lookup is UNRESOLVED (adapter unavailable / timeout, or the
        ``employment_income`` field is missing or non-numeric) -> return a
        SINGLE UNRESOLVED Indicator (Severity.UNRESOLVED, HISTORY_INCONSISTENCY)
        and run no further analysis (Requirements 6.1, 6.3);
      - otherwise run the earnings discrepancy check
        (``|ROE - T4| > $2,000`` -> CRITICAL Indicator; Requirements 6.2, 6.4)
        and return the resulting Indicators.

``GET /healthz``
    Liveness/readiness probe.

The CRA client is injectable: in dev/test a fake transport is placed on
``app.state`` so no live network is used. Only task 14.1 is implemented here;
NOC validation, concurrent-employment and the LLM contradiction check are task
14.2 and are intentionally NOT implemented.

Requirements: 6.1, 6.2, 6.3, 6.4, 6.10
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from fastapi import Depends, FastAPI, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from shared.auth import Role, require_role
from shared.models import EngineType, Indicator, Severity

try:  # sibling modules imported by bare name in the service directory
    from config import get_settings  # type: ignore
    from client import CRAAdapterClient, T4Lookup  # type: ignore
    from discrepancy import check_earnings_discrepancy  # type: ignore
    from analysis import run_core_analysis, run_narrative_check  # type: ignore
    from narrative import NarrativeContradictionChecker  # type: ignore
    from kafka_publisher import IndicatorPublisher  # type: ignore
    from noc import check_noc_mismatch  # type: ignore
except ImportError:  # pragma: no cover - when imported as a package
    from .config import get_settings  # type: ignore
    from .client import CRAAdapterClient, T4Lookup  # type: ignore
    from .discrepancy import check_earnings_discrepancy  # type: ignore
    from .analysis import run_core_analysis, run_narrative_check  # type: ignore
    from .narrative import NarrativeContradictionChecker  # type: ignore
    from .kafka_publisher import IndicatorPublisher  # type: ignore
    from .noc import check_noc_mismatch  # type: ignore

logger = logging.getLogger("history-inconsistency-engine")


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class AnalyzeRequest(BaseModel):
    """Payload for ``POST /engine/analyze``.

    ``roe_earnings`` is the earnings amount the claimant declared on their ROE;
    it is compared against the CRA T4 employment income retrieved for the same
    ``employer_bn`` and ``tax_year`` keyed by ``sin_hash``. It is typed as a
    string so exact ``Decimal`` values survive JSON transport without float
    rounding.
    """

    sin_hash: str
    tax_year: int
    employer_bn: str
    roe_earnings: str
    claim_id: Optional[str] = None

    # ── task 14.2 inputs (all optional; checks skip when absent) ──
    # The NOC code declared on the ROE (compared against the CRA employer
    # industry category — Requirement 6.5).
    declared_noc: Optional[str] = None
    # Claim period bounds for concurrent-employment detection (Requirement 6.8).
    claim_start_date: Optional[date] = None
    claim_end_date: Optional[date] = None
    # ROE reason-for-separation narrative and the CRA continued-employment
    # evidence, for the LLM contradiction check (Requirements 6.6, 6.7).
    separation_narrative: Optional[str] = None
    cra_continued_employment_evidence: Optional[str] = None


class AnalyzeResponse(BaseModel):
    """Response body: the produced Indicator list plus light metadata."""

    sin_hash: str
    claim_id: Optional[str] = None
    status: str  # "OK" | "UNRESOLVED"
    indicators: list[dict[str, Any]]
    # task 14.2 metadata: wall-clock budget compliance (excluding LLM wait) and
    # how many Indicators were published to Kafka.
    core_elapsed_ms: float = 0.0
    within_budget: bool = True
    published_count: int = 0


# ---------------------------------------------------------------------------
# Lifespan — wire up the CRA client (httpx-backed in prod; fake in dev/test)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings

    # The CRA client is set on app.state. In dev/test the caller injects a
    # client backed by a fake transport before issuing requests. Otherwise we
    # build a default httpx-backed client using the configured 5-second timeout.
    if not hasattr(app.state, "cra_client"):
        app.state.cra_client = CRAAdapterClient(
            settings.cra_adapter_url,
            timeout_seconds=settings.cra_timeout_seconds,
        )

    # The LLM narrative-contradiction checker and Kafka Indicator publisher are
    # optional and injectable. In dev/test they are typically left unset (the
    # narrative check is skipped and Indicators are only returned in the HTTP
    # response); production wiring supplies an LLM client and an aiokafka-backed
    # producer. Kept out of the request path so a missing broker/LLM never
    # blocks analysis (Requirements 6.6, 6.7, 6.9).
    if not hasattr(app.state, "narrative_checker"):
        app.state.narrative_checker = None
    if not hasattr(app.state, "indicator_publisher"):
        app.state.indicator_publisher = IndicatorPublisher(
            None,
            topic=settings.indicators_topic,
            publish_budget_ms=settings.analysis_budget_ms,
        )

    yield


app = FastAPI(
    title="EI-ADS Employment History Inconsistency Engine",
    version="0.1.0",
    lifespan=lifespan,
)

# ── OpenTelemetry distributed tracing (shared.telemetry, task 32.1 / Req 21.5) ──
# Single, self-contained hook: all tracer setup and span helpers live in
# shared/telemetry.py. No-op when the OpenTelemetry packages are absent.
from shared.telemetry import instrument_tracing  # noqa: E402

instrument_tracing(app, service_name="history-inconsistency-engine")
# ── end OpenTelemetry tracing ──

# --- Prometheus metrics + /metrics endpoint (task 31.1) ---
from shared.metrics import instrument_app  # noqa: E402

instrument_app(app, kind="engine")
# --- end metrics wiring ---


def _get_client(app_: FastAPI) -> CRAAdapterClient:
    client = getattr(app_.state, "cra_client", None)
    if client is None:
        settings = getattr(app_.state, "settings", None) or get_settings()
        client = CRAAdapterClient(
            settings.cra_adapter_url, timeout_seconds=settings.cra_timeout_seconds
        )
        app_.state.cra_client = client
    return client


def _get_narrative_checker(
    app_: FastAPI,
) -> Optional[NarrativeContradictionChecker]:
    return getattr(app_.state, "narrative_checker", None)


def _get_indicator_publisher(app_: FastAPI) -> IndicatorPublisher:
    publisher = getattr(app_.state, "indicator_publisher", None)
    if publisher is None:
        settings = getattr(app_.state, "settings", None) or get_settings()
        publisher = IndicatorPublisher(
            None,
            topic=settings.indicators_topic,
            publish_budget_ms=settings.analysis_budget_ms,
        )
        app_.state.indicator_publisher = publisher
    return publisher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unresolved_indicator(
    claim_id: Optional[str], reason: Optional[str]
) -> Indicator:
    """Build the single UNRESOLVED Indicator emitted when the T4 can't be used.

    Covers all three UNRESOLVED conditions: CRA Adapter unavailable / timeout,
    a missing ``employment_income`` field, and a non-numeric
    ``employment_income`` field (Requirements 6.1, 6.3).
    """
    return Indicator(
        indicator_id=uuid.uuid4(),
        claim_id=_coerce_uuid(claim_id),
        engine=EngineType.HISTORY_INCONSISTENCY,
        severity=Severity.UNRESOLVED,
        algorithm="cra_t4_retrieval",
        title="CRA T4 data unavailable or unusable",
        explanation_en=(
            "The CRA T4 record could not be retrieved or was missing a usable "
            "employment income value, so the earnings discrepancy check could "
            "not be completed for this claim."
        ),
        explanation_fr=(
            "Le releve T4 de l'ARC n'a pas pu etre recupere ou ne contenait pas "
            "de revenu d'emploi utilisable; la verification de l'ecart de gains "
            "n'a pas pu etre effectuee pour cette demande."
        ),
        payload={"reason": reason or "CRA_UNAVAILABLE"},
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
    """Run earnings-discrepancy analysis for a claim.

    Fetches the CRA T4 for the claim's ``employer_bn`` / ``tax_year`` keyed by
    ``sin_hash``. On an UNRESOLVED lookup a single UNRESOLVED Indicator is
    returned and NO further analysis runs. Otherwise the ROE-vs-T4 earnings
    discrepancy check runs and its Indicators are returned.
    """
    settings = getattr(app.state, "settings", None) or get_settings()
    client = _get_client(app)

    # ── Retrieve the CRA T4; any failure => single UNRESOLVED Indicator ──
    lookup: T4Lookup = await client.fetch_t4(
        req.sin_hash, req.tax_year, req.employer_bn
    )
    if lookup.unresolved or lookup.t4_earnings is None:
        logger.info("analyze_unresolved", extra={"reason": lookup.reason})
        indicator = _unresolved_indicator(req.claim_id, lookup.reason)
        return AnalyzeResponse(
            sin_hash=req.sin_hash,
            claim_id=req.claim_id,
            status="UNRESOLVED",
            indicators=[indicator.to_dict()],
        )

    # ── Parse the ROE earnings; a non-numeric ROE amount is also UNRESOLVED ──
    roe_earnings = _to_decimal_or_none(req.roe_earnings)
    if roe_earnings is None:
        logger.info("analyze_unresolved", extra={"reason": "ROE_NON_NUMERIC"})
        indicator = _unresolved_indicator(req.claim_id, "ROE_NON_NUMERIC")
        return AnalyzeResponse(
            sin_hash=req.sin_hash,
            claim_id=req.claim_id,
            status="UNRESOLVED",
            indicators=[indicator.to_dict()],
        )

    # ── Run the earnings discrepancy check (task 14.1, Req 6.4) ──
    indicators = check_earnings_discrepancy(
        roe_earnings,
        lookup.t4_earnings,
        claim_id=req.claim_id,
        threshold=settings.discrepancy_threshold,
    )

    # ── Run the task-14.2 core analysis within the 500 ms budget (Req 6.9) ──
    # NOC validation (6.5) + concurrent employment (6.8). The wall-clock budget
    # excludes the LLM narrative wait, which is measured/awaited separately.
    core_elapsed_ms = 0.0
    within_budget = True
    if req.claim_start_date is not None and req.claim_end_date is not None:
        core = await run_core_analysis(
            client,
            sin_hash=req.sin_hash,
            tax_year=req.tax_year,
            employer_bn=req.employer_bn,
            declared_noc=req.declared_noc,
            claim_start=req.claim_start_date,
            claim_end=req.claim_end_date,
            claim_id=req.claim_id,
            budget_ms=settings.analysis_budget_ms,
        )
        indicators.extend(core.indicators)
        core_elapsed_ms = core.elapsed_ms
        within_budget = core.within_budget
    else:
        # Without a claim period the concurrent-employment check cannot run;
        # the NOC check still can (it only needs the declared NOC + CRA record).
        cra_category = await client.fetch_employer_noc_category(
            req.sin_hash, req.tax_year, req.employer_bn
        )
        indicators.extend(
            check_noc_mismatch(
                req.declared_noc, cra_category, claim_id=req.claim_id
            )
        )

    # ── LLM narrative contradiction (6.6, 6.7) — EXCLUDED from the budget ──
    narrative_indicators = await run_narrative_check(
        _get_narrative_checker(app),
        narrative=req.separation_narrative,
        cra_evidence=req.cra_continued_employment_evidence,
        claim_id=req.claim_id,
    )
    indicators.extend(narrative_indicators)

    # ── Publish all Indicators to Kafka (Req 6.9) ──
    publisher = _get_indicator_publisher(app)
    publish_result = await publisher.publish(str(req.claim_id or ""), indicators)

    logger.info(
        "analyze_complete",
        extra={
            "indicator_count": len(indicators),
            "core_elapsed_ms": core_elapsed_ms,
            "within_budget": within_budget,
            "published_count": publish_result.published_count,
        },
    )

    return AnalyzeResponse(
        sin_hash=req.sin_hash,
        claim_id=req.claim_id,
        status="OK",
        indicators=[i.to_dict() for i in indicators],
        core_elapsed_ms=core_elapsed_ms,
        within_budget=within_budget,
        published_count=publish_result.published_count,
    )


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Liveness/readiness probe.

    Healthy when a CRA Adapter client is wired up (a fake in dev/test, or an
    httpx-backed client in production).
    """
    healthy = getattr(app.state, "cra_client", None) is not None
    code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(
        status_code=code,
        content={"status": "ok" if healthy else "degraded"},
    )


def _to_decimal_or_none(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


__all__ = [
    "app",
    "AnalyzeRequest",
    "AnalyzeResponse",
    "lifespan",
    "analyze",
    "healthz",
]
