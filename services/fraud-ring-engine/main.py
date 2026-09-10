"""
services/fraud-ring-engine/main.py
===================================
FastAPI application for the Fraud Ring Engine (task 13.1).

Endpoints:
  - ``POST /engine/analyze`` — accepts a claim payload (``employer_bn``,
    ``separation_date``, optional ``claim_id``, and either an inline ``cohort``
    or a signal to fetch it from the wired ``CohortSource``). It runs the
    shell-employer check (via the Business Registry Adapter) and the
    coordinated-layoff check (over the 90-day cohort) and returns the resulting
    Indicators.
  - ``GET /healthz`` — liveness / readiness for the engine.

Task 13.1 scope is shell-employer + coordinated-layoff detection. GNN community
detection and hours-band analysis (task 13.2) are intentionally NOT wired here.

On registry unavailability the shell-employer check is skipped (no crash); on
cohort-source unavailability the coordinated-layoff check is skipped. Both
degrade gracefully.

Requirements: 5.2, 5.3, 5.4
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from fastapi import Depends, FastAPI, status

from shared.auth import Role, require_role
from fastapi.responses import JSONResponse

try:  # sibling modules imported by bare name inside the service directory
    from config import get_settings  # type: ignore
    from client import (  # type: ignore
        BusinessRegistryAdapterClient,
        CohortSource,
        RegistryLookupStatus,
    )
    from detection import (  # type: ignore
        CohortMember,
        check_coordinated_layoff,
        check_shell_employer,
    )
    from community_analysis import (  # type: ignore
        CohortClaimant,
        check_hours_band,
        check_shared_attribute_cluster,
    )
    from graph_client import GraphCallStatus, GraphServiceClient  # type: ignore
    from publisher import (  # type: ignore
        IndicatorPublisher,
        InMemoryIndicatorPublisher,
    )
except ImportError:  # pragma: no cover - when imported as a package
    from .config import get_settings  # type: ignore
    from .client import (  # type: ignore
        BusinessRegistryAdapterClient,
        CohortSource,
        RegistryLookupStatus,
    )
    from .detection import (  # type: ignore
        CohortMember,
        check_coordinated_layoff,
        check_shell_employer,
    )
    from .community_analysis import (  # type: ignore
        CohortClaimant,
        check_hours_band,
        check_shared_attribute_cluster,
    )
    from .graph_client import GraphCallStatus, GraphServiceClient  # type: ignore
    from .publisher import (  # type: ignore
        IndicatorPublisher,
        InMemoryIndicatorPublisher,
    )

try:
    import httpx  # type: ignore

    _HAS_HTTPX = True
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore
    _HAS_HTTPX = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Wire the registry client and cohort source based on environment.

    In development / test no collaborators are wired by default; tests inject
    fakes onto ``app.state``. In higher environments an httpx-backed registry
    client is constructed against the configured Business Registry Adapter URL.
    """
    settings = get_settings()
    app.state.settings = settings
    app.state.http_client = None
    app.state.registry_client = None
    app.state.cohort_source = None
    app.state.graph_client = None
    # An in-memory publisher captures Indicators when no broker is wired; higher
    # environments would inject an AioKafkaIndicatorPublisher.
    app.state.publisher = InMemoryIndicatorPublisher(
        topic=settings.fraud_ring_indicators_topic
    )

    if not settings.is_development and _HAS_HTTPX:  # pragma: no cover - live only
        client = httpx.AsyncClient()
        app.state.http_client = client
        # Internal GCKey service Bearer presented to the auth-gated Business
        # Registry Adapter + Graph Service (Req 19.7); empty => no header.
        service_token = getattr(settings, "service_token", "") or None
        app.state.registry_client = BusinessRegistryAdapterClient(
            settings.business_registry_adapter_url,
            client,
            timeout_seconds=settings.registry_timeout_seconds,
            bearer_token=service_token,
        )
        app.state.graph_client = GraphServiceClient(
            settings.graph_service_url,
            client,
            timeout_seconds=settings.graph_timeout_seconds,
            bearer_token=service_token,
        )

    try:
        yield
    finally:
        if app.state.http_client is not None:  # pragma: no cover
            try:
                await app.state.http_client.aclose()
            except Exception:  # noqa: BLE001
                pass


app = FastAPI(
    title="EI-ADS Fraud Ring Engine",
    version="0.1.0",
    lifespan=lifespan,
)

# ── OpenTelemetry distributed tracing (shared.telemetry, task 32.1 / Req 21.5) ──
# Single, self-contained hook: all tracer setup and span helpers live in
# shared/telemetry.py. No-op when the OpenTelemetry packages are absent.
from shared.telemetry import instrument_tracing  # noqa: E402

instrument_tracing(app, service_name="fraud-ring-engine")
# ── end OpenTelemetry tracing ──

# --- Prometheus metrics + /metrics endpoint (task 31.1) ---
from shared.metrics import instrument_app  # noqa: E402

instrument_app(app, kind="engine")
# --- end metrics wiring ---


def _ensure_state() -> None:
    """Ensure ``app.state`` has the expected attributes when lifespan is skipped."""
    if not hasattr(app.state, "settings"):
        app.state.settings = get_settings()
    for attr in ("registry_client", "cohort_source", "http_client", "graph_client"):
        if not hasattr(app.state, attr):
            setattr(app.state, attr, None)
    if not hasattr(app.state, "publisher"):
        app.state.publisher = InMemoryIndicatorPublisher(
            topic=app.state.settings.fraud_ring_indicators_topic
        )


def _parse_cohort(raw_cohort: Any) -> list[CohortMember]:
    """Parse an inline cohort payload into ``CohortMember`` records."""
    cohort: list[CohortMember] = []
    for raw in raw_cohort or []:
        cohort.append(
            CohortMember(
                sin_hash=str(raw.get("sin_hash", "")),
                separation_date=date.fromisoformat(raw["separation_date"]),
                roe_submission_ts=datetime.fromisoformat(raw["roe_submission_ts"]),
                claim_id=raw.get("claim_id"),
            )
        )
    return cohort


def _to_float(value: Any) -> Optional[float]:
    """Best-effort convert an insurable-hours value (Decimal/str/num) to float."""
    if value is None or value == "":
        return None
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _parse_graph_cohort(raw_cohort: Any) -> list[CohortClaimant]:
    """Parse an inline graph-cohort payload into ``CohortClaimant`` records.

    Each record carries the claimant's identity plus the attributes needed by
    the community-cluster (postal FSA / surname hash) and hours-band checks.
    """
    cohort: list[CohortClaimant] = []
    for raw in raw_cohort or []:
        cohort.append(
            CohortClaimant(
                sin_hash=str(raw.get("sin_hash", "")),
                postal_fsa=raw.get("postal_fsa"),
                surname_hash=raw.get("surname_hash"),
                insurable_hours=_to_float(raw.get("insurable_hours")),
                claim_id=raw.get("claim_id"),
            )
        )
    return cohort


def _budget_start(payload: dict[str, Any]) -> float:
    """Return the monotonic reference for the 800 ms budget.

    The design measures the budget from the ``claim.received`` event timestamp.
    When the caller supplies ``received_at`` we honour it by converting its age
    into a monotonic offset; otherwise the budget starts now.
    """
    now = time.monotonic()
    raw = payload.get("received_at")
    if not raw:
        return now
    try:
        received = datetime.fromisoformat(str(raw))
    except (ValueError, TypeError):
        return now
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - received).total_seconds()
    if elapsed <= 0:
        return now
    return now - elapsed


def _budget_exceeded(start: float, budget_ms: int) -> bool:
    """True when the elapsed wall-clock since ``start`` exceeds ``budget_ms``."""
    return (time.monotonic() - start) * 1000.0 >= budget_ms


@app.post("/engine/analyze")
async def analyze(
    payload: dict[str, Any],
    _user: dict[str, Any] = Depends(require_role(Role.INVESTIGATOR)),
) -> Any:
    """Run the full Fraud Ring Engine analysis for one claim.

    Request body::

        {
          "employer_bn": "123456789RT0001",
          "separation_date": "2024-03-01",
          "claim_id": "optional-uuid",
          "received_at": "optional ISO datetime (claim.received timestamp)",
          "cohort": [ {sin_hash, separation_date, roe_submission_ts, claim_id?}, ... ],
          "graph_cohort": [ {sin_hash, postal_fsa?, surname_hash?, insurable_hours?}, ... ],
          "min_qualifying_hours": optional float override
        }

    Checks (all Indicators published to ``indicators.fraud-ring``):
      * shell-employer (Req 5.2/5.3)
      * coordinated-layoff over the 90-day cohort (Req 5.4)
      * community shared-attribute cluster over the 24-month graph cohort
        (Req 5.5)
      * cohort hours-band (Req 5.6) with NOT_EVALUATED for cohorts < 3 (Req 5.7)

    The engine enforces an 800 ms wall-clock budget (Req 5.8/5.9): once the
    budget is exceeded it stops running further checks and publishes the partial
    results computed so far.
    """
    _ensure_state()
    settings = app.state.settings
    budget_start = _budget_start(payload)

    employer_bn = payload.get("employer_bn")
    claim_id = payload.get("claim_id")
    raw_sep = payload.get("separation_date")

    if not raw_sep:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": "separation_date is required"},
        )
    try:
        separation_date = date.fromisoformat(str(raw_sep))
    except (ValueError, TypeError):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": "separation_date must be ISO-8601 (YYYY-MM-DD)"},
        )

    indicators = []
    checks_run: list[str] = []
    checks_skipped: list[str] = []
    dispositions: dict[str, str] = {}
    budget_exceeded = False

    # ── Shell-employer detection (Req 5.2) ──
    registry_client: Optional[BusinessRegistryAdapterClient] = getattr(
        app.state, "registry_client", None
    )
    if employer_bn and registry_client is not None:
        lookup = await registry_client.lookup_employer(employer_bn)
        if lookup.status is RegistryLookupStatus.UNAVAILABLE:
            checks_skipped.append("shell_employer:REGISTRY_UNAVAILABLE")
        else:
            registration = (
                lookup.registration
                if lookup.status is RegistryLookupStatus.FOUND
                else None
            )
            indicators.extend(
                check_shell_employer(
                    registration,
                    separation_date,
                    window_months=settings.shell_registration_window_months,
                    claim_id=claim_id,
                    employer_bn=employer_bn,
                )
            )
            checks_run.append("shell_employer")
    else:
        checks_skipped.append("shell_employer:NO_REGISTRY_CLIENT")

    # ── Coordinated-layoff detection (Req 5.3, 5.4) ──
    cohort: list[CohortMember] = []
    if payload.get("cohort") is not None:
        cohort = _parse_cohort(payload.get("cohort"))
        cohort_available = True
    else:
        cohort_source: Optional[CohortSource] = getattr(
            app.state, "cohort_source", None
        )
        cohort_available = cohort_source is not None
        if cohort_source is not None and employer_bn:
            try:
                cohort = await cohort_source.load_cohort(
                    employer_bn,
                    as_of=separation_date,
                    window_days=settings.coordinated_layoff_window_days,
                )
            except Exception:  # noqa: BLE001 - source unavailable -> skip check
                cohort_available = False

    if cohort_available:
        indicators.extend(
            check_coordinated_layoff(
                cohort,
                same_date_fraction=settings.coordinated_layoff_same_date_fraction,
                roe_window_hours=settings.coordinated_layoff_roe_window_hours,
                claim_id=claim_id,
                employer_bn=employer_bn,
            )
        )
        checks_run.append("coordinated_layoff")
    else:
        checks_skipped.append("coordinated_layoff:COHORT_UNAVAILABLE")

    # ── Community detection + hours-band analysis (Req 5.5, 5.6, 5.7) ──
    graph_cohort: list[CohortClaimant] = _parse_graph_cohort(
        payload.get("graph_cohort")
    )
    num_communities = 0

    if _budget_exceeded(budget_start, settings.engine_budget_ms):
        budget_exceeded = True
        checks_skipped.append("community_cluster:BUDGET_EXCEEDED")
        checks_skipped.append("hours_band:BUDGET_EXCEEDED")
    else:
        # Construct the current claim's edge and partition the cohort via the
        # Graph Service when a client is wired. Both calls degrade gracefully —
        # a Graph Service outage skips only the community-partition evidence, not
        # the (self-contained) cohort attribute/hours analysis.
        graph_client: Optional[GraphServiceClient] = getattr(
            app.state, "graph_client", None
        )
        if graph_client is not None and employer_bn:
            await graph_client.construct(
                {
                    "sin_hash": payload.get("sin_hash", claim_id or ""),
                    "employer_bn": employer_bn,
                    "separation_date": separation_date.isoformat(),
                    "claim_id": claim_id,
                }
            )
            if graph_cohort:
                edges = [(m.sin_hash, str(employer_bn)) for m in graph_cohort]
                community = await graph_client.community_detect(edges=edges)
                if community.status is GraphCallStatus.OK:
                    num_communities = community.num_communities
                else:
                    checks_skipped.append("graph_community_detect:UNAVAILABLE")

        # Shared-attribute cluster (Req 5.5)
        cluster_result = check_shared_attribute_cluster(
            graph_cohort,
            fraction_threshold=settings.community_shared_attribute_fraction,
            claim_id=claim_id,
            employer_bn=employer_bn,
        )
        if cluster_result.disposition == "NOT_EVALUATED":
            dispositions["community_cluster"] = "NOT_EVALUATED"
            checks_skipped.append("community_cluster:NOT_EVALUATED")
        else:
            indicators.extend(cluster_result.indicators)
            checks_run.append("community_cluster")

        # Hours-band (Req 5.6, 5.7) — re-check budget between checks.
        if _budget_exceeded(budget_start, settings.engine_budget_ms):
            budget_exceeded = True
            checks_skipped.append("hours_band:BUDGET_EXCEEDED")
        else:
            min_qualifying = payload.get("min_qualifying_hours")
            min_qualifying_hours = (
                _to_float(min_qualifying)
                if min_qualifying is not None
                else settings.min_qualifying_hours
            )
            hours_result = check_hours_band(
                graph_cohort,
                min_qualifying_hours,
                band_width=settings.hours_band_width,
                min_cohort=settings.hours_band_min_cohort,
                claim_id=claim_id,
                employer_bn=employer_bn,
            )
            dispositions["hours_band"] = hours_result.disposition
            if hours_result.disposition == "NOT_EVALUATED":
                checks_skipped.append(
                    f"hours_band:NOT_EVALUATED:{hours_result.detail or ''}"
                )
            else:
                indicators.extend(hours_result.indicators)
                checks_run.append("hours_band")

    # ── Publish all Indicators to Kafka indicators.fraud-ring (Req 5.8) ──
    publisher: Optional[IndicatorPublisher] = getattr(app.state, "publisher", None)
    published_count = 0
    if publisher is not None and indicators:
        try:
            published_count = await publisher.publish(indicators)
        except Exception:  # noqa: BLE001 - publish failure must not crash engine
            checks_skipped.append("publish:KAFKA_UNAVAILABLE")

    # Latency-breach detection (Req 5.9): report if the total exceeded budget.
    if _budget_exceeded(budget_start, settings.engine_budget_ms):
        budget_exceeded = True

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "employer_bn": employer_bn,
            "separation_date": separation_date.isoformat(),
            "checks_run": checks_run,
            "checks_skipped": checks_skipped,
            "dispositions": dispositions,
            "cohort_size": len(cohort),
            "graph_cohort_size": len(graph_cohort),
            "num_communities": num_communities,
            "budget_ms": settings.engine_budget_ms,
            "budget_exceeded": budget_exceeded,
            "elapsed_ms": round((time.monotonic() - budget_start) * 1000.0, 2),
            "published_count": published_count,
            "indicators": [i.to_dict() for i in indicators],
        },
    )


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Report engine readiness. The engine is always ready to run its detectors."""
    _ensure_state()
    return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "ok"})


__all__ = ["app", "lifespan"]
