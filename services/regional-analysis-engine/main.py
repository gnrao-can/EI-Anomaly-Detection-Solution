"""
services/regional-analysis-engine/main.py
=========================================
FastAPI application for the Regional Analysis Engine (task 16.1).

Endpoints:
  * ``POST /engine/analyze`` — accepts a claim payload (economic region, NOC,
    reference month, insurable hourly earnings, and claim period), fetches the
    regional/national unemployment rates from the Stats Canada Adapter and the
    active postings + median wage from the Job Bank Adapter, then runs the
    rate-deviation and job-posting checks from ``analysis.py`` and returns the
    produced Indicators. When the Stats Canada Adapter is unavailable, the
    response is annotated with ``DATA_UNAVAILABLE`` (design: engine annotates
    Indicators with ``DATA_UNAVAILABLE`` and skips the rate-dependent analysis).
  * ``GET /healthz`` — liveness/readiness probe.

Task 16.1 implements rate deviation + job postings. Task 16.2 adds:
  * seasonality annotation of all Indicators when the claim falls in a seasonal
    employment peak (Requirement 8.7);
  * a MEDIUM recurring-seasonal-claim Indicator when the claimant filed in the
    same calendar quarter in >= 2 of the 3 prior years (Requirement 8.8);
  * a Prophet forecast on the trailing 24-month regional unemployment history,
    recorded for monitoring only (never an Indicator trigger; Requirement 8.11);
  * a 500 ms wall-clock budget over analyse + publish, and publication of all
    produced Indicators to the ``indicators.regional-analysis`` Kafka topic
    (Requirement 8.9).

Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 8.9, 8.11
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from uuid import UUID

from fastapi import Depends, FastAPI, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from shared.auth import Role, require_role
from shared.models import EngineType, Indicator, Severity

try:  # sibling modules imported by bare name in the service directory
    from analysis import check_job_postings, check_rate_deviation  # type: ignore
    from client import (  # type: ignore
        JobBankClient,
        JobBankUnavailableError,
        StatsCanadaClient,
        StatsCanadaUnavailableError,
    )
    from config import get_settings  # type: ignore
    from forecast import fit_forecast  # type: ignore
    from kafka_producer import (  # type: ignore
        INDICATORS_TOPIC,
        FakeIndicatorEventProducer,
        IndicatorPublishError,
    )
    from providers import (  # type: ignore
        InMemoryClaimHistoryProvider,
        InMemorySeasonalPatternProvider,
    )
    from seasonality import (  # type: ignore
        annotate_indicators,
        check_recurring_claims,
        seasonal_annotation,
    )
except ImportError:  # pragma: no cover - when imported as a package
    from .analysis import check_job_postings, check_rate_deviation  # type: ignore
    from .client import (  # type: ignore
        JobBankClient,
        JobBankUnavailableError,
        StatsCanadaClient,
        StatsCanadaUnavailableError,
    )
    from .config import get_settings  # type: ignore
    from .forecast import fit_forecast  # type: ignore
    from .kafka_producer import (  # type: ignore
        INDICATORS_TOPIC,
        FakeIndicatorEventProducer,
        IndicatorPublishError,
    )
    from .providers import (  # type: ignore
        InMemoryClaimHistoryProvider,
        InMemorySeasonalPatternProvider,
    )
    from .seasonality import (  # type: ignore
        annotate_indicators,
        check_recurring_claims,
        seasonal_annotation,
    )


#: Response flags surfaced to callers.
FLAG_OK = "OK"
FLAG_DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
#: Flag surfaced when the engine exceeded its 500 ms wall-clock budget (Req 8.9).
FLAG_BUDGET_EXCEEDED = "BUDGET_EXCEEDED"


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


class AnalyzeRequest(BaseModel):
    """Claim payload for a regional-analysis run."""

    claim_id: Optional[UUID] = Field(default=None)
    economic_region_code: str = Field(..., description="ESDC 2-char region code.")
    noc_code: str = Field(..., description="NOC 2021 code.")
    reference_month: str = Field(
        ..., description="Reference month as YYYY-MM (month before claim start)."
    )
    insurable_hourly_earnings: Decimal = Field(
        ..., description="Claimant's insurable hourly earnings (Decimal)."
    )
    claim_period_start: Optional[str] = Field(default=None)
    claim_period_end: Optional[str] = Field(default=None)

    # ── Task 16.2 inputs ────────────────────────────────────────────
    sector_code: Optional[str] = Field(
        default=None, description="Industry sector code for the seasonal lookup."
    )
    claim_key: Optional[str] = Field(
        default=None,
        description="Stable claimant key (e.g. sin_hash) for claim-history lookup.",
    )
    reference_date: Optional[str] = Field(
        default=None,
        description="Claim reference date as YYYY-MM-DD; defaults to the first of "
        "the reference month when omitted.",
    )
    prior_claim_dates: Optional[list[str]] = Field(
        default=None,
        description="Optional inline prior claim start dates (YYYY-MM-DD). When "
        "supplied these take precedence over the claim-history provider.",
    )
    regional_unemployment_history: Optional[list[float]] = Field(
        default=None,
        description="Trailing regional unemployment observations (oldest first) "
        "for the Prophet forecast; ideally 24 months.",
    )


# ---------------------------------------------------------------------------
# Client construction / lifespan
# ---------------------------------------------------------------------------


def _build_statscan_client(settings: Any) -> StatsCanadaClient:
    return StatsCanadaClient(
        base_url=settings.statscan_adapter_url,
        timeout=settings.adapter_timeout_seconds,
        bearer_token=getattr(settings, "service_token", "") or None,
    )


def _build_job_bank_client(settings: Any) -> JobBankClient:
    return JobBankClient(
        base_url=settings.job_bank_adapter_url,
        timeout=settings.adapter_timeout_seconds,
        bearer_token=getattr(settings, "service_token", "") or None,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Wire settings, adapter clients, providers and producer onto app state."""
    settings = get_settings()
    app.state.settings = settings
    app.state.statscan_client = _build_statscan_client(settings)
    app.state.job_bank_client = _build_job_bank_client(settings)
    # Default to in-memory providers / fake producer; production wiring (real DB
    # providers and an aiokafka-backed IndicatorEventProducer) overrides these
    # via app.state before serving traffic.
    app.state.seasonal_provider = InMemorySeasonalPatternProvider()
    app.state.claim_history_provider = InMemoryClaimHistoryProvider()
    app.state.indicator_producer = FakeIndicatorEventProducer(
        topic=settings.indicators_topic
    )
    yield


app = FastAPI(
    title="EI-ADS Regional Analysis Engine",
    version="0.1.0",
    lifespan=lifespan,
)

# ── OpenTelemetry distributed tracing (shared.telemetry, task 32.1 / Req 21.5) ──
# Single, self-contained hook: all tracer setup and span helpers live in
# shared/telemetry.py. No-op when the OpenTelemetry packages are absent.
from shared.telemetry import instrument_tracing  # noqa: E402

instrument_tracing(app, service_name="regional-analysis-engine")
# ── end OpenTelemetry tracing ──

# --- Prometheus metrics + /metrics endpoint (task 31.1) ---
from shared.metrics import instrument_app  # noqa: E402

instrument_app(app, kind="engine")
# --- end metrics wiring ---


def _get_settings() -> Any:
    return getattr(app.state, "settings", None) or get_settings()


def _get_statscan_client() -> StatsCanadaClient:
    client = getattr(app.state, "statscan_client", None)
    if client is None:
        client = _build_statscan_client(_get_settings())
        app.state.statscan_client = client
    return client


def _get_job_bank_client() -> JobBankClient:
    client = getattr(app.state, "job_bank_client", None)
    if client is None:
        client = _build_job_bank_client(_get_settings())
        app.state.job_bank_client = client
    return client


def _get_seasonal_provider() -> Any:
    provider = getattr(app.state, "seasonal_provider", None)
    if provider is None:
        provider = InMemorySeasonalPatternProvider()
        app.state.seasonal_provider = provider
    return provider


def _get_claim_history_provider() -> Any:
    provider = getattr(app.state, "claim_history_provider", None)
    if provider is None:
        provider = InMemoryClaimHistoryProvider()
        app.state.claim_history_provider = provider
    return provider


def _setting(settings: Any, name: str, default: Any) -> Any:
    """Read a settings attribute defensively, falling back to *default*.

    Keeps the engine robust to partial/legacy settings objects that predate the
    task 16.2 fields (e.g. a caller that only wired the 16.1 thresholds).
    """
    value = getattr(settings, name, None)
    return default if value is None else value


def _get_indicator_producer() -> Any:
    producer = getattr(app.state, "indicator_producer", None)
    if producer is None:
        topic = _setting(_get_settings(), "indicators_topic", INDICATORS_TOPIC)
        producer = FakeIndicatorEventProducer(topic=topic)
        app.state.indicator_producer = producer
    return producer


def _parse_reference_date(request: "AnalyzeRequest") -> date:
    """Resolve the claim reference date, defaulting to the reference month's 1st."""
    if request.reference_date:
        return date.fromisoformat(request.reference_date)
    # reference_month is YYYY-MM -> first day of that month.
    year, month = request.reference_month.split("-")
    return date(int(year), int(month), 1)


def _data_unavailable_indicator(
    claim_id: UUID, produced_at: datetime, source: str
) -> Indicator:
    """Build a DATA_UNAVAILABLE annotation Indicator for a missing source."""
    return Indicator(
        indicator_id=uuid.uuid4(),
        claim_id=claim_id,
        engine=EngineType.REGIONAL_ANALYSIS,
        severity=Severity.UNRESOLVED,
        algorithm="data_unavailable",
        title=f"{source} data unavailable",
        explanation_en=(
            f"{source} data was unavailable; regional analysis could not be "
            f"completed for this source."
        ),
        explanation_fr=(
            f"Les donnees de {source} etaient indisponibles; l'analyse "
            f"regionale n'a pas pu etre effectuee pour cette source."
        ),
        payload={"source": source, "status": FLAG_DATA_UNAVAILABLE},
        produced_at=produced_at,
    )


@app.post("/engine/analyze")
async def analyze(
    request: AnalyzeRequest,
    _user: dict[str, Any] = Depends(require_role(Role.INVESTIGATOR)),
) -> JSONResponse:
    """Run the full regional analysis for a claim and publish its Indicators."""
    settings = _get_settings()
    claim_id = request.claim_id or uuid.uuid4()
    produced_at = datetime.now(timezone.utc)
    # Wall-clock budget starts at claim receipt into the engine (Req 8.9).
    start = time.monotonic()

    indicators: list[Indicator] = []
    data_unavailable = False

    # ── Rate deviation (Stats Canada) ──────────────────────────────
    statscan = _get_statscan_client()
    try:
        rates = await statscan.get_rates(
            request.economic_region_code, request.reference_month
        )
    except StatsCanadaUnavailableError:
        # Annotate with DATA_UNAVAILABLE and skip the rate-dependent analysis.
        data_unavailable = True
        indicators.append(
            _data_unavailable_indicator(claim_id, produced_at, "Stats Canada")
        )
    else:
        indicators.extend(
            check_rate_deviation(
                rates.regional_rate,
                rates.national_rate,
                claim_id=claim_id,
                threshold_pp=settings.rate_deviation_threshold_pp,
                produced_at=produced_at,
            )
        )

    # ── Job postings (Job Bank) ────────────────────────────────────
    job_bank = _get_job_bank_client()
    try:
        postings = await job_bank.get_postings(
            request.noc_code,
            request.economic_region_code,
            request.reference_month,
        )
    except JobBankUnavailableError:
        indicators.append(
            _data_unavailable_indicator(claim_id, produced_at, "Job Bank")
        )
    else:
        indicators.extend(
            check_job_postings(
                postings.active_posting_count,
                postings.median_hourly_wage,
                request.insurable_hourly_earnings,
                claim_id=claim_id,
                max_active_postings=settings.max_active_postings,
                wage_ratio_threshold=settings.wage_ratio_threshold,
                produced_at=produced_at,
            )
        )

    # ── Reference date used by seasonality + recurring-claim checks ─
    try:
        reference_date = _parse_reference_date(request)
    except (ValueError, TypeError):
        reference_date = produced_at.date()

    # ── Recurring seasonal claim cycle (Requirement 8.8) ───────────
    prior_dates = await _resolve_prior_claim_dates(request)
    indicators.extend(
        check_recurring_claims(
            reference_date,
            prior_dates,
            claim_id=claim_id,
            produced_at=produced_at,
        )
    )

    # ── Seasonality annotation of ALL Indicators (Requirement 8.7) ──
    seasonal = await _resolve_seasonal_annotation(request, settings)
    if seasonal is not None:
        annotate_indicators(indicators, seasonal)

    # ── Prophet forecast — recorded for monitoring only (Req 8.11) ──
    forecast_record = _record_forecast(request, reference_date)

    # ── Publish all Indicators to Kafka (Requirement 8.9) ──────────
    published = 0
    publish_error: Optional[str] = None
    producer = _get_indicator_producer()
    try:
        published = await producer.publish_indicators(indicators)
    except IndicatorPublishError as exc:  # noqa: BLE001
        publish_error = str(exc)

    # ── Enforce the 500 ms wall-clock budget (Requirement 8.9) ─────
    budget_ms = _setting(settings, "engine_budget_ms", 500)
    elapsed_ms = (time.monotonic() - start) * 1000.0
    budget_exceeded = elapsed_ms > budget_ms

    if data_unavailable:
        flag = FLAG_DATA_UNAVAILABLE
    elif budget_exceeded:
        flag = FLAG_BUDGET_EXCEEDED
    else:
        flag = FLAG_OK

    body = {
        "claim_id": str(claim_id),
        "flag": flag,
        "data_unavailable": data_unavailable,
        "budget_exceeded": budget_exceeded,
        "elapsed_ms": round(elapsed_ms, 3),
        "budget_ms": budget_ms,
        "engine": EngineType.REGIONAL_ANALYSIS.value,
        "count": len(indicators),
        "published_count": published,
        "publish_error": publish_error,
        "seasonal": bool(seasonal),
        "forecast": forecast_record,
        "indicators": [i.to_dict() for i in indicators],
    }
    return JSONResponse(status_code=status.HTTP_200_OK, content=body)


async def _resolve_prior_claim_dates(request: AnalyzeRequest) -> list[date]:
    """Prefer inline prior claim dates; otherwise query the history provider."""
    if request.prior_claim_dates is not None:
        parsed: list[date] = []
        for raw in request.prior_claim_dates:
            try:
                parsed.append(date.fromisoformat(raw))
            except (ValueError, TypeError):
                continue
        return parsed
    if request.claim_key:
        provider = _get_claim_history_provider()
        try:
            return list(await provider.get_prior_claim_dates(request.claim_key))
        except Exception:  # noqa: BLE001 - history lookup failure is non-fatal
            return []
    return []


async def _resolve_seasonal_annotation(
    request: AnalyzeRequest, settings: Any
) -> Optional[dict]:
    """Query the seasonal-pattern DB and build the SEASONAL annotation if in a peak."""
    if not request.sector_code:
        return None
    provider = _get_seasonal_provider()
    try:
        sector = await provider.get_sector_index(
            request.sector_code, request.reference_month
        )
    except Exception:  # noqa: BLE001 - seasonal lookup failure is non-fatal
        return None
    if sector is None:
        return None
    return seasonal_annotation(
        sector.sector_index,
        sector.rolling_avg_12m,
        multiplier=_setting(settings, "seasonal_multiplier", Decimal("1.10")),
    )


def _record_forecast(
    request: AnalyzeRequest, reference_date: date
) -> Optional[dict]:
    """Fit the Prophet forecast for monitoring; never raises, never triggers."""
    history = request.regional_unemployment_history
    if not history or len(history) < 2:
        return None
    try:
        result = fit_forecast(
            history, start_month=date(reference_date.year, reference_date.month, 1)
        )
    except Exception:  # noqa: BLE001 - forecasting is monitoring-only, non-fatal
        return None
    return result.to_dict()


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Liveness/readiness probe. The engine has no hard startup dependency."""
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "ok", "service": _get_settings().service_name},
    )


def _coerce_decimal(value: Any) -> Decimal:  # pragma: no cover - convenience
    """Coerce a value to Decimal via the string path (used by callers/tests)."""
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"cannot parse Decimal from {value!r}") from exc


__all__ = [
    "app",
    "lifespan",
    "analyze",
    "healthz",
    "AnalyzeRequest",
    "FLAG_OK",
    "FLAG_DATA_UNAVAILABLE",
    "FLAG_BUDGET_EXCEEDED",
]
