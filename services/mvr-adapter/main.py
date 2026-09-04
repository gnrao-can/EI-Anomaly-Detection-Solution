"""
services/mvr-adapter/main.py
============================
FastAPI application for the MVR (Motor Vehicle Registry) Adapter.

Responsibilities (task 23.1):
  * ``GET /mvr/{sin_hash}?province=XX`` — query the provincial MVR API for the
    claimant's residential province and return a strictly data-minimised
    result: only ``vehicle_ownership_status`` and ``vehicle_class``
    (Requirements 15.1, 15.4). No vehicle history, insurance, or financial data
    is ever returned — the projection strips everything else.
      - 3-second total timeout (Requirements 15.3, 15.5).
      - On timeout or any error, a structured ``{"status": "UNAVAILABLE"}`` is
        returned so the Geographic Anomaly Engine treats ownership as *unknown*
        rather than *absent* (Requirement 15.2).
      - In dev/test, records come from a deterministic synthetic generator keyed
        by the residential province — one fixture per province (Requirement
        15.6).
  * ``GET /healthz`` — liveness/readiness.

The provincial API base URLs and per-province API key are supplied via
environment configuration (see ``config.py`` / ``.env.example``); dev/test use
the synthetic generator and require no keys.

Requirements: 15.1, 15.2, 15.3, 15.4, 15.5, 15.6
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from shared.auth import Role, require_role

try:  # sibling modules imported by bare name in the service directory
    from client import (  # type: ignore
        ALLOWED_FIELDS,
        MVRClient,
        MVRResult,
        SyntheticMVRGenerator,
        lookup_vehicle,
        project_allowed_fields,
    )
    from config import get_settings  # type: ignore
except ImportError:  # pragma: no cover - when imported as a package
    from .client import (  # type: ignore
        ALLOWED_FIELDS,
        MVRClient,
        MVRResult,
        SyntheticMVRGenerator,
        lookup_vehicle,
        project_allowed_fields,
    )
    from .config import get_settings  # type: ignore


# A dedicated logger. We log only non-content metadata (province, status,
# timing) — never the VIN, owner name, or other identifying vehicle details.
logger = logging.getLogger("mvr-adapter")


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class MVRRecord(BaseModel):
    """The data-minimised MVR record — exactly the two allowed fields."""

    vehicle_ownership_status: Optional[str] = None
    vehicle_class: Optional[str] = None


class MVRResponse(BaseModel):
    """200 response body for a successful lookup."""

    sin_hash: str
    province: str
    status: str = "OK"
    record: MVRRecord


class MVRUnavailable(BaseModel):
    """Structured UNAVAILABLE result (timeout or error).

    The Geographic Anomaly Engine treats this as vehicle ownership *unknown*
    rather than *absent* (Requirement 15.2).
    """

    sin_hash: str
    province: str
    status: str = "UNAVAILABLE"
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Lifespan — wire up the transport (synthetic in dev/test, live otherwise)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings

    # Deterministic synthetic generator is always available (dev/test default).
    app.state.synthetic = SyntheticMVRGenerator(settings.mvr_mock_data_dir)

    # Live provincial clients are built lazily per-province at request time in
    # production; nothing to construct up-front here.
    app.state.mvr_clients = {}

    try:
        yield
    finally:
        for client in getattr(app.state, "mvr_clients", {}).values():
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


app = FastAPI(
    title="EI-ADS MVR Adapter",
    version="0.1.0",
    lifespan=lifespan,
)

# ── OpenTelemetry distributed tracing (shared.telemetry, task 32.1 / Req 21.5) ──
# Single, self-contained hook: all tracer setup and span helpers live in
# shared/telemetry.py. No-op when the OpenTelemetry packages are absent.
from shared.telemetry import instrument_tracing  # noqa: E402

instrument_tracing(app, service_name="mvr-adapter")
# ── end OpenTelemetry tracing ──

# --- Prometheus metrics + /metrics endpoint (task 31.1) ---
from shared.metrics import instrument_app  # noqa: E402

instrument_app(app, kind="adapter")
# --- end metrics wiring ---


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_live_client(app_: FastAPI, settings: Any, province: str) -> Optional[MVRClient]:
    """Return (constructing/caching if needed) the live client for a province."""
    province = (province or "").upper()
    clients = getattr(app_.state, "mvr_clients", None)
    if clients is None:
        clients = {}
        app_.state.mvr_clients = clients
    if province in clients:
        return clients[province]
    base_url = settings.base_url_for(province)
    if not base_url:
        return None
    try:
        client = MVRClient(
            base_url, api_key=settings.mvr_api_key, timeout=settings.mvr_timeout
        )
    except Exception:  # noqa: BLE001 - degrade; endpoint returns UNAVAILABLE
        return None
    clients[province] = client
    return client


async def _lookup_with_timeout(
    settings: Any,
    sin_hash: str,
    province: str,
    *,
    synthetic: Optional[SyntheticMVRGenerator],
    live_client: Optional[MVRClient],
    timeout_s: float,
) -> MVRResult:
    """Run the (synchronous) lookup within the 3-second total timeout.

    The blocking lookup is offloaded to a worker thread and wrapped in
    ``asyncio.wait_for`` so the 3-second budget is enforced without blocking the
    event loop. On timeout, an ``UNAVAILABLE`` result is returned (Req 15.3).
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                lookup_vehicle,
                settings,
                sin_hash,
                province,
                synthetic=synthetic,
                live_client=live_client,
            ),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        return MVRResult.unavailable("TIMEOUT")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get(
    "/mvr/{sin_hash}",
    responses={
        200: {"model": MVRResponse},
        502: {"model": MVRUnavailable},
    },
)
async def get_vehicle(
    sin_hash: str,
    province: str = Query(
        default="",
        description="Claimant residential province code (e.g. 'ON', 'BC'). "
        "Selects the provincial MVR API and, in dev/test, the fixture.",
    ),
    _user: dict[str, Any] = Depends(require_role(Role.INVESTIGATOR)),
) -> Any:
    """Return the data-minimised vehicle record for a claimant.

    Only ``vehicle_ownership_status`` and ``vehicle_class`` are ever returned
    (Requirement 15.4). On timeout or error, a structured UNAVAILABLE result is
    returned so ownership is treated as *unknown* rather than *absent*
    (Requirements 15.2, 15.3).
    """
    settings = getattr(app.state, "settings", None) or get_settings()
    province_norm = (province or "").upper()

    synthetic = getattr(app.state, "synthetic", None)
    if synthetic is None:
        synthetic = SyntheticMVRGenerator(settings.mvr_mock_data_dir)
        app.state.synthetic = synthetic

    live_client = None
    if not settings.use_synthetic:
        live_client = _get_live_client(app, settings, province_norm)

    result = await _lookup_with_timeout(
        settings,
        sin_hash,
        province_norm,
        synthetic=synthetic,
        live_client=live_client,
        timeout_s=settings.mvr_timeout,
    )

    # Metadata-only logging — never vehicle-record contents.
    logger.info(
        "mvr_lookup",
        extra={
            "province": province_norm,
            "status": result.status,
        },
    )

    if result.status != "OK" or result.record is None:
        failure = MVRUnavailable(
            sin_hash=sin_hash,
            province=province_norm,
            reason=result.reason or "UNAVAILABLE",
        )
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content=failure.model_dump(),
        )

    # Defence in depth: minimise again at the boundary so ONLY allowed fields
    # can ever be serialised, regardless of upstream/synthetic behaviour.
    minimised = project_allowed_fields(result.record)
    return MVRResponse(
        sin_hash=sin_hash,
        province=province_norm,
        record=MVRRecord(**minimised),
    )


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Liveness/readiness probe.

    The adapter is healthy when it can serve requests: in dev/test the synthetic
    generator is always available; in production the province base-URL map must
    be configured.
    """
    settings = getattr(app.state, "settings", None) or get_settings()
    if settings.use_synthetic:
        healthy = getattr(app.state, "synthetic", None) is not None
    else:
        healthy = bool(settings.mvr_province_base_urls)

    code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(
        status_code=code,
        content={
            "status": "ok" if healthy else "degraded",
            "mode": "synthetic" if settings.use_synthetic else "live",
            "allowed_fields": sorted(ALLOWED_FIELDS),
        },
    )


__all__ = [
    "app",
    "MVRRecord",
    "MVRResponse",
    "MVRUnavailable",
    "lifespan",
    "get_vehicle",
    "healthz",
]
