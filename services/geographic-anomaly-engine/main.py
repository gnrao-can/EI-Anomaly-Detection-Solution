"""
services/geographic-anomaly-engine/main.py
===========================================
FastAPI application for the Geographic Anomaly Engine.

Task 12.1 (foundation): resolve the claimant's residential postal code and the
employer work-location to coordinates via the GeoNames Adapter, and compute the
Haversine geodesic distance (4 dp). On resolution failure emit a single MEDIUM
Indicator and skip distance-dependent analysis.

Task 12.2 (this task): the distance-dependent analysis, plus budget enforcement
and Kafka publication.

  * ``POST /engine/analyze`` — after computing the distance:
      - If distance > 100 km (Req 4.3/4.4):
          * Query the MVR Adapter — no registered vehicle -> HIGH Indicator
            (adapter UNAVAILABLE => ownership UNKNOWN, no Indicator).
          * If no public transit route connects the locations -> HIGH Indicator.
          * Compute the lowest-cost weekly transport cost vs the weekly EI
            benefit rate; cost > benefit -> CRITICAL Indicator with CAD amounts
            (2 dp) and the cost-to-benefit ratio (Req 4.5). If transport cost
            data is unavailable from all sources -> MEDIUM Indicator (Req 4.6).
          * If the two most recent prior employers are ALSO > 100 km from the
            residence, merge their names/distances/dates into the produced
            distance Indicator (Req 4.7); omit entirely when < 2 prior
            employers (Req 4.8).
      - Independently, query the CBSA Adapter for cross-border absences during
        employment; >= 10 consecutive days outside Canada -> HIGH Indicator
        (Req 4.9).
  * A 600 ms wall-clock budget bounds the analysis (Req 4.9). On budget
    exhaustion the engine returns whatever Indicators it has already produced.
  * All produced Indicators are published to Kafka
    ``indicators.geographic-anomaly`` (best-effort; publish failure does not
    fail the response but is reported in the body).
  * ``GET /healthz`` — liveness/readiness.

In dev/test the caller injects fakes on ``app.state.geonames`` / ``.mvr`` /
``.cbsa`` / ``.producer``; no live network is required.

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9, 4.10
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Any, Optional

from fastapi import Depends, FastAPI, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from shared.auth import Role, require_role
from shared.models import EngineType, Indicator, Severity

try:  # sibling modules imported by bare name in the service directory
    from cbsa_client import CBSAAdapterClient  # type: ignore
    from client import GeoNamesAdapterClient, ResolutionFailedError  # type: ignore
    from config import get_settings  # type: ignore
    from geo import haversine  # type: ignore
    from indicators import (  # type: ignore
        cross_border_absence_indicator,
        economic_implausibility_indicator,
        merge_prior_employer_payload,
        no_transit_indicator,
        no_vehicle_indicator,
        transport_data_unavailable_indicator,
    )
    from kafka_producer import IndicatorPublishError  # type: ignore
    from mvr_client import MVRAdapterClient  # type: ignore
    from transport import (  # type: ignore
        PriorEmployer,
        exceeds_distance_threshold,
        prior_employers_over_threshold,
        weekly_transport_cost_exceeds_benefit,
    )
except ImportError:  # pragma: no cover - when imported as a package
    from .cbsa_client import CBSAAdapterClient  # type: ignore
    from .client import GeoNamesAdapterClient, ResolutionFailedError  # type: ignore
    from .config import get_settings  # type: ignore
    from .geo import haversine  # type: ignore
    from .indicators import (  # type: ignore
        cross_border_absence_indicator,
        economic_implausibility_indicator,
        merge_prior_employer_payload,
        no_transit_indicator,
        no_vehicle_indicator,
        transport_data_unavailable_indicator,
    )
    from .kafka_producer import IndicatorPublishError  # type: ignore
    from .mvr_client import MVRAdapterClient  # type: ignore
    from .transport import (  # type: ignore
        PriorEmployer,
        exceeds_distance_threshold,
        prior_employers_over_threshold,
        weekly_transport_cost_exceeds_benefit,
    )


logger = logging.getLogger("geographic-anomaly-engine")


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class PriorEmployerInput(BaseModel):
    """A prior employer's resolved distance and employment window (Req 4.7)."""

    employer_name: str
    distance_km: float
    employment_start: Optional[str] = None  # ISO date
    employment_end: Optional[str] = None  # ISO date


class AnalyzeRequest(BaseModel):
    """Payload for ``POST /engine/analyze``.

    ``residential_postal_code`` and ``employer_work_location`` are resolved to
    coordinates via the GeoNames Adapter. The remaining fields drive the
    distance-dependent analysis (task 12.2):

      * ``sin_hash`` / ``residential_province`` — key the MVR and CBSA lookups.
      * ``transit_route_exists`` — whether a public transit route connects the
        two locations (from GeoNames); ``None`` means unknown (no transit
        Indicator raised).
      * ``transport_mode_costs`` — mode -> weekly CAD cost; a ``None`` value
        means that mode is unavailable. Empty / all-``None`` => data unavailable.
      * ``weekly_benefit_rate`` — the claimant's weekly EI benefit rate (CAD).
      * ``prior_employers`` — prior employers ordered most-recent-first.
    """

    claim_id: Optional[str] = None
    residential_postal_code: str
    employer_work_location: str
    employer_bn: Optional[str] = None
    sin_hash: Optional[str] = None
    residential_province: str = ""
    transit_route_exists: Optional[bool] = None
    transport_mode_costs: dict[str, Optional[float]] = {}
    weekly_benefit_rate: Optional[float] = None
    prior_employers: list[PriorEmployerInput] = []


class AnalyzeResponse(BaseModel):
    """Response body: distance (when computed) plus the produced Indicator list."""

    claim_id: Optional[str] = None
    status: str  # "OK" | "RESOLUTION_FAILED"
    distance_km: Optional[float] = None
    residential_coord: Optional[dict[str, float]] = None
    work_coord: Optional[dict[str, float]] = None
    indicators: list[dict[str, Any]]
    elapsed_ms: float = 0.0
    budget_exceeded: bool = False
    published_count: int = 0
    publish_error: Optional[str] = None


# ---------------------------------------------------------------------------
# Lifespan — wire up the adapter clients (fakes in dev/test)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings

    # In dev/test the caller injects fakes on app.state.{geonames,mvr,cbsa,producer}.
    # In production we build HTTP-backed clients pointed at the adapter URLs.
    if not hasattr(app.state, "geonames"):
        try:
            app.state.geonames = GeoNamesAdapterClient(
                settings.geonames_adapter_url,
                connect_timeout=settings.geonames_connect_timeout_seconds,
                read_timeout=settings.geonames_read_timeout_seconds,
            )
        except Exception:  # noqa: BLE001 - httpx unavailable in minimal envs
            app.state.geonames = None

    if not hasattr(app.state, "mvr"):
        try:
            app.state.mvr = MVRAdapterClient(
                settings.mvr_adapter_url,
                connect_timeout=settings.mvr_connect_timeout_seconds,
                read_timeout=settings.mvr_read_timeout_seconds,
            )
        except Exception:  # noqa: BLE001
            app.state.mvr = None

    if not hasattr(app.state, "cbsa"):
        try:
            app.state.cbsa = CBSAAdapterClient(
                settings.cbsa_adapter_url,
                connect_timeout=settings.cbsa_connect_timeout_seconds,
                read_timeout=settings.cbsa_read_timeout_seconds,
            )
        except Exception:  # noqa: BLE001
            app.state.cbsa = None

    if not hasattr(app.state, "producer"):
        # Kafka producer is wired up by deployment infra; absent by default in
        # minimal envs. A None producer simply skips publication.
        app.state.producer = None

    try:
        yield
    finally:
        pass


app = FastAPI(
    title="EI-ADS Geographic Anomaly Engine",
    version="0.2.0",
    lifespan=lifespan,
)

# ── OpenTelemetry distributed tracing (shared.telemetry, task 32.1 / Req 21.5) ──
# Single, self-contained hook: all tracer setup and span helpers live in
# shared/telemetry.py. No-op when the OpenTelemetry packages are absent.
from shared.telemetry import instrument_tracing  # noqa: E402

instrument_tracing(app, service_name="geographic-anomaly-engine")
# ── end OpenTelemetry tracing ──

# --- Prometheus metrics + /metrics endpoint (task 31.1) ---
from shared.metrics import instrument_app  # noqa: E402

instrument_app(app, kind="engine")
# --- end metrics wiring ---


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_uuid(claim_id: Optional[str]) -> uuid.UUID:
    if isinstance(claim_id, uuid.UUID):
        return claim_id
    if claim_id:
        try:
            return uuid.UUID(str(claim_id))
        except (ValueError, AttributeError, TypeError):
            return uuid.uuid5(uuid.NAMESPACE_OID, str(claim_id))
    return uuid.uuid4()


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _resolution_failed_indicator(
    claim_id: Optional[str], failed_locations: list[str]
) -> Indicator:
    """Build the single MEDIUM Indicator emitted when a coordinate cannot resolve."""
    joined = ", ".join(failed_locations) if failed_locations else "unknown"
    return Indicator(
        indicator_id=uuid.uuid4(),
        claim_id=_coerce_uuid(claim_id),
        engine=EngineType.GEOGRAPHIC_ANOMALY,
        severity=Severity.MEDIUM,
        algorithm="coordinate_resolution",
        title="Location coordinate resolution failed",
        explanation_en=(
            "One or more locations could not be resolved to coordinates "
            f"({joined}); distance-dependent geographic analysis was skipped "
            "for this claim."
        ),
        explanation_fr=(
            "Un ou plusieurs emplacements n'ont pas pu etre convertis en "
            f"coordonnees ({joined}); l'analyse geographique dependante de la "
            "distance a ete ignoree pour cette demande."
        ),
        payload={
            "reason": "RESOLUTION_FAILED",
            "failed_locations": failed_locations,
        },
        produced_at=datetime.now(timezone.utc),
    )


def _budget_seconds(settings: Any) -> float:
    ms = getattr(settings, "analysis_budget_ms", 600)
    try:
        return float(ms) / 1000.0
    except (TypeError, ValueError):
        return 0.6


async def _run_distance_dependent_checks(
    req: AnalyzeRequest,
    *,
    distance_km: float,
    mvr_client: Any,
    deadline: float,
) -> list[Indicator]:
    """Run the MVR / transit / economic / prior-employer checks (distance > 100 km).

    Each check is skipped if the wall-clock deadline has already passed, so the
    engine stays within its 600 ms budget (Req 4.9). Returns the produced
    Indicators (may be empty).
    """
    produced: list[Indicator] = []

    if not exceeds_distance_threshold(distance_km):
        return produced

    # Prior-employer pattern (Req 4.7/4.8) — computed once and merged into the
    # first distance-implausibility Indicator produced below.
    prior = [
        PriorEmployer(
            employer_name=pe.employer_name,
            distance_km=pe.distance_km,
            employment_start=_parse_date(pe.employment_start),
            employment_end=_parse_date(pe.employment_end),
        )
        for pe in req.prior_employers
    ]
    pattern = prior_employers_over_threshold(prior)
    prior_merged = False

    def _merge_once(indicator: Indicator) -> Indicator:
        nonlocal prior_merged
        if not prior_merged:
            merge_prior_employer_payload(indicator, pattern)
            prior_merged = True
        return indicator

    # ── MVR: no registered vehicle -> HIGH (Req 4.3) ──
    if time.monotonic() < deadline and mvr_client is not None and req.sin_hash:
        try:
            mvr_result = await mvr_client.get_ownership(
                req.sin_hash, req.residential_province
            )
        except Exception:  # noqa: BLE001 - degrade to UNKNOWN, no Indicator
            mvr_result = None
        # Only a confirmed NO-vehicle disposition raises the Indicator; UNKNOWN
        # (adapter unavailable) and OWNED do not (Req 15.2).
        if mvr_result is not None and mvr_result.no_vehicle:
            produced.append(
                _merge_once(
                    no_vehicle_indicator(
                        req.claim_id,
                        distance_km=distance_km,
                        province=req.residential_province,
                    )
                )
            )

    # ── Transit: no public transit route -> HIGH (Req 4.4) ──
    if time.monotonic() < deadline and req.transit_route_exists is False:
        produced.append(
            _merge_once(
                no_transit_indicator(req.claim_id, distance_km=distance_km)
            )
        )

    # ── Economic implausibility (Req 4.5 / 4.6) ──
    if time.monotonic() < deadline:
        cost = weekly_transport_cost_exceeds_benefit(
            req.transport_mode_costs,
            req.weekly_benefit_rate if req.weekly_benefit_rate is not None else 0,
        )
        if cost is None:
            # Transport cost data unavailable from all sources -> MEDIUM (Req 4.6).
            produced.append(
                transport_data_unavailable_indicator(
                    req.claim_id, distance_km=distance_km
                )
            )
        elif cost.exceeds:
            produced.append(
                _merge_once(
                    economic_implausibility_indicator(
                        req.claim_id, distance_km=distance_km, cost=cost
                    )
                )
            )

    return produced


async def _run_cbsa_check(
    req: AnalyzeRequest, *, cbsa_client: Any, deadline: float
) -> list[Indicator]:
    """Query CBSA for cross-border absences; >= 10 consecutive days -> HIGH (Req 4.9)."""
    if cbsa_client is None or not req.sin_hash or time.monotonic() >= deadline:
        return []
    try:
        result = await cbsa_client.get_absences(
            req.sin_hash, req.residential_province
        )
    except Exception:  # noqa: BLE001 - CBSA failure degrades silently
        return []
    if result is not None and result.has_long_absence:
        return [
            cross_border_absence_indicator(
                req.claim_id,
                max_absence_days=result.max_absence_days,
                partial=result.partial,
            )
        ]
    return []


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
    """Resolve coordinates, compute distance, and run distance-dependent checks.

    Enforces a 600 ms wall-clock budget from the start of analysis; on
    exhaustion the engine returns whatever Indicators it produced (Req 4.9).
    Publishes all produced Indicators to Kafka best-effort.
    """
    start = time.monotonic()
    settings = getattr(app.state, "settings", None) or get_settings()
    deadline = start + _budget_seconds(settings)

    geonames = getattr(app.state, "geonames", None)
    mvr_client = getattr(app.state, "mvr", None)
    cbsa_client = getattr(app.state, "cbsa", None)
    producer = getattr(app.state, "producer", None)

    # ── No resolver wired up: hard resolution failure ──
    if geonames is None:
        logger.info("analyze_resolution_failed", extra={"reason": "no_client"})
        indicator = _resolution_failed_indicator(
            req.claim_id,
            [req.residential_postal_code, req.employer_work_location],
        )
        published, publish_err = await _publish(producer, [indicator])
        return AnalyzeResponse(
            claim_id=req.claim_id,
            status="RESOLUTION_FAILED",
            distance_km=None,
            indicators=[indicator.to_dict()],
            elapsed_ms=_elapsed_ms(start),
            published_count=published,
            publish_error=publish_err,
        )

    # ── Resolve both coordinates; collect any that fail ──
    residential_coord = None
    work_coord = None
    failed: list[str] = []

    try:
        residential_coord = await geonames.resolve(req.residential_postal_code)
    except ResolutionFailedError:
        failed.append(req.residential_postal_code)

    try:
        work_coord = await geonames.resolve(req.employer_work_location)
    except ResolutionFailedError:
        failed.append(req.employer_work_location)

    # ── On any resolution failure: single MEDIUM Indicator, skip distance ──
    if failed:
        logger.info("analyze_resolution_failed", extra={"failed_count": len(failed)})
        indicator = _resolution_failed_indicator(req.claim_id, failed)
        published, publish_err = await _publish(producer, [indicator])
        return AnalyzeResponse(
            claim_id=req.claim_id,
            status="RESOLUTION_FAILED",
            distance_km=None,
            indicators=[indicator.to_dict()],
            elapsed_ms=_elapsed_ms(start),
            published_count=published,
            publish_error=publish_err,
        )

    # ── Both resolved: compute the Haversine geodesic distance (4 dp) ──
    distance_km = haversine(
        residential_coord.latitude,
        residential_coord.longitude,
        work_coord.latitude,
        work_coord.longitude,
    )

    # ── Distance-dependent checks + CBSA absence check (task 12.2) ──
    indicators: list[Indicator] = []
    indicators.extend(
        await _run_distance_dependent_checks(
            req, distance_km=distance_km, mvr_client=mvr_client, deadline=deadline
        )
    )
    indicators.extend(
        await _run_cbsa_check(req, cbsa_client=cbsa_client, deadline=deadline)
    )

    budget_exceeded = time.monotonic() > deadline

    # ── Publish all produced Indicators to Kafka (best-effort) ──
    published, publish_err = await _publish(producer, indicators)

    logger.info(
        "analyze_complete",
        extra={
            "distance_km": distance_km,
            "indicator_count": len(indicators),
            "budget_exceeded": budget_exceeded,
        },
    )

    return AnalyzeResponse(
        claim_id=req.claim_id,
        status="OK",
        distance_km=distance_km,
        residential_coord={
            "latitude": residential_coord.latitude,
            "longitude": residential_coord.longitude,
        },
        work_coord={
            "latitude": work_coord.latitude,
            "longitude": work_coord.longitude,
        },
        indicators=[i.to_dict() for i in indicators],
        elapsed_ms=_elapsed_ms(start),
        budget_exceeded=budget_exceeded,
        published_count=published,
        publish_error=publish_err,
    )


async def _publish(
    producer: Any, indicators: list[Indicator]
) -> tuple[int, Optional[str]]:
    """Publish Indicators best-effort; never fail the response on publish error."""
    if producer is None or not indicators:
        return 0, None
    try:
        count = await producer.publish_indicators(indicators)
        return int(count), None
    except IndicatorPublishError as exc:
        logger.warning("indicator_publish_failed", extra={"error": str(exc)})
        return 0, str(exc)
    except Exception as exc:  # noqa: BLE001 - never propagate publish failures
        logger.warning("indicator_publish_failed", extra={"error": str(exc)})
        return 0, str(exc)


def _elapsed_ms(start: float) -> float:
    return round((time.monotonic() - start) * 1000.0, 3)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Liveness/readiness probe.

    Healthy when a GeoNames Adapter client is wired up (a fake in dev/test or an
    HTTP-backed client in production).
    """
    healthy = getattr(app.state, "geonames", None) is not None
    code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(
        status_code=code,
        content={"status": "ok" if healthy else "degraded"},
    )


__all__ = [
    "app",
    "AnalyzeRequest",
    "AnalyzeResponse",
    "PriorEmployerInput",
    "lifespan",
    "analyze",
    "healthz",
]
