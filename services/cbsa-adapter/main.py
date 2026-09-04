"""
services/cbsa-adapter/main.py
=============================
FastAPI application for the CBSA Adapter.

Responsibilities (task 21.1):
  * ``GET /cbsa/travel/{sin_hash}`` — fetch a claimant's cross-border travel
    records over the CBSA Protected B secure channel (mTLS) and return them
    alongside the detected absence periods.
      - 5-second total timeout (Requirement 13.1).
      - On timeout, any partial records already received are preserved and
        returned; there is NO automatic retry; a structured failure result is
        included so the Geographic Anomaly Engine can degrade gracefully
        (Requirement 13.2).
      - Travel-record CONTENTS are never written to any general-purpose log
        (Requirement 13.4).
      - Targets a parse/response time under 300 ms for responses with <= 500
        travel records (Requirement 13.5).
      - In dev/test, records come from a deterministic synthetic generator keyed
        by the province of employment (Requirement 13.6).
  * ``GET /healthz`` — liveness/readiness.

mTLS material (client cert/key, CA bundle) and the base URL are supplied via
environment configuration (see ``config.py`` / ``.env.example``); dev/test use
the synthetic generator and require no certificates.

Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from shared.auth import Role, require_role

try:  # sibling modules imported by bare name in the service directory
    from absence import (  # type: ignore
        AbsencePeriod,
        CrossingRecord,
        detect_absence_periods,
    )
    from client import (  # type: ignore
        CBSAClient,
        CBSAFetchError,
        SyntheticCBSAGenerator,
    )
    from config import get_settings  # type: ignore
except ImportError:  # pragma: no cover - when imported as a package
    from .absence import (  # type: ignore
        AbsencePeriod,
        CrossingRecord,
        detect_absence_periods,
    )
    from .client import (  # type: ignore
        CBSAClient,
        CBSAFetchError,
        SyntheticCBSAGenerator,
    )
    from .config import get_settings  # type: ignore


# A dedicated logger. Per Requirement 13.4 we log only non-content metadata
# (record counts, timings, status) — never the crossing dates/ports/directions.
logger = logging.getLogger("cbsa-adapter")


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class TravelResponse(BaseModel):
    """200 response body for a successful (or partial-but-usable) fetch."""

    sin_hash: str
    status: str  # "OK" | "PARTIAL"
    record_count: int
    partial: bool
    absence_periods: list[dict[str, Any]]
    records: list[dict[str, Any]]


class TravelFailure(BaseModel):
    """Structured failure result (no auto-retry) returned on channel failure.

    Any partial records received before the failure are preserved in
    ``records`` so downstream analysis can still use them (Requirement 13.2).
    """

    sin_hash: str
    status: str = "FAILED"
    reason: str
    partial: bool
    record_count: int
    absence_periods: list[dict[str, Any]]
    records: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Lifespan — wire up the transport (synthetic in dev/test, mTLS otherwise)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings

    # Deterministic synthetic generator is always available (dev/test default).
    app.state.synthetic = SyntheticCBSAGenerator(settings.cbsa_mock_data_dir)

    # The live mTLS client is only constructed when we are NOT using synthetic
    # data — i.e. a real environment with certificates. Failure to build it is
    # non-fatal; the endpoint reports it as a failure at request time.
    app.state.cbsa_client = None
    if not settings.use_synthetic:
        try:
            app.state.cbsa_client = CBSAClient(
                settings.cbsa_api_base_url,
                client_cert_path=settings.cbsa_client_cert_path,
                client_key_path=settings.cbsa_client_key_path,
                ca_cert_path=settings.cbsa_ca_cert_path,
                timeout=settings.cbsa_timeout,
            )
        except Exception:  # noqa: BLE001 - degrade; no content logged
            app.state.cbsa_client = None

    try:
        yield
    finally:
        client = getattr(app.state, "cbsa_client", None)
        if client is not None:
            client.close()


app = FastAPI(
    title="EI-ADS CBSA Adapter",
    version="0.1.0",
    lifespan=lifespan,
)

# ── OpenTelemetry distributed tracing (shared.telemetry, task 32.1 / Req 21.5) ──
# Single, self-contained hook: all tracer setup and span helpers live in
# shared/telemetry.py. No-op when the OpenTelemetry packages are absent.
from shared.telemetry import instrument_tracing  # noqa: E402

instrument_tracing(app, service_name="cbsa-adapter")
# ── end OpenTelemetry tracing ──

# --- Prometheus metrics + /metrics endpoint (task 31.1) ---
from shared.metrics import instrument_app  # noqa: E402

instrument_app(app, kind="adapter")
# --- end metrics wiring ---


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialise_records(records: list[CrossingRecord]) -> list[dict[str, Any]]:
    return [r.to_dict() for r in records]


def _serialise_absences(periods: list[AbsencePeriod]) -> list[dict[str, Any]]:
    return [p.to_dict() for p in periods]


def _fetch_records(
    app_: FastAPI, sin_hash: str, province: str
) -> tuple[list[CrossingRecord], bool, Optional[str]]:
    """Fetch travel records for a claimant.

    Returns ``(records, partial, failure_reason)`` where:
      * ``records`` — the crossing records obtained (possibly partial),
      * ``partial`` — True if a failure interrupted a complete fetch,
      * ``failure_reason`` — a safe, content-free reason string on failure,
        else ``None``.

    There is no auto-retry: a single attempt is made (Requirement 13.2).
    """
    settings = getattr(app_.state, "settings", None) or get_settings()

    # ── Dev/test (or no mTLS): deterministic synthetic generator ──
    if settings.use_synthetic:
        generator = getattr(app_.state, "synthetic", None) or SyntheticCBSAGenerator(
            settings.cbsa_mock_data_dir
        )
        records = generator.generate(sin_hash, province)
        return records, False, None

    # ── Production: single attempt over the mTLS channel, no retry ──
    client = getattr(app_.state, "cbsa_client", None)
    if client is None:
        return [], True, "CHANNEL_UNAVAILABLE"

    try:
        records = client.fetch_travel_records(sin_hash)
        return records, False, None
    except CBSAFetchError as exc:
        # Preserve whatever partial records the client managed to attach, if any.
        partial_records = list(getattr(exc, "partial_records", []) or [])
        # ``str(exc)`` is content-free by construction in client.py.
        return partial_records, True, str(exc)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get(
    "/cbsa/travel/{sin_hash}",
    responses={
        200: {"model": TravelResponse},
        502: {"model": TravelFailure},
    },
)
async def get_travel(
    sin_hash: str,
    province: str = Query(
        default="",
        description="Claimant province of employment (drives the dev/test "
        "synthetic generator; e.g. 'ON', 'BC').",
    ),
    _user: dict[str, Any] = Depends(require_role(Role.INVESTIGATOR)),
) -> Any:
    """Return travel records + detected absence periods for a claimant.

    Absence periods are computed with ``detect_absence_periods`` (EXIT -> ENTRY
    gaps >= 10 days). On a channel failure, any partial records are preserved and
    a structured 502 failure result is returned (no auto-retry). Travel-record
    contents are never logged.
    """
    records, partial, failure_reason = _fetch_records(app, sin_hash, province)

    # Absence detection runs on whatever records we have (complete or partial).
    absences = detect_absence_periods(records)

    # Metadata-only logging (counts/timing/status) — never record contents.
    logger.info(
        "cbsa_travel_fetch",
        extra={
            "record_count": len(records),
            "partial": partial,
            "absence_count": len(absences),
            "status": "FAILED" if failure_reason else "OK",
        },
    )

    if failure_reason:
        failure = TravelFailure(
            sin_hash=sin_hash,
            reason=failure_reason,
            partial=partial,
            record_count=len(records),
            absence_periods=_serialise_absences(absences),
            records=_serialise_records(records),
        )
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content=failure.model_dump(),
        )

    return TravelResponse(
        sin_hash=sin_hash,
        status="PARTIAL" if partial else "OK",
        record_count=len(records),
        partial=partial,
        absence_periods=_serialise_absences(absences),
        records=_serialise_records(records),
    )


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Liveness/readiness probe.

    The adapter is healthy when it can serve requests: in dev/test the
    synthetic generator is always available; in production a constructed mTLS
    client (or the ability to construct one) is required.
    """
    settings = getattr(app.state, "settings", None) or get_settings()
    if settings.use_synthetic:
        healthy = getattr(app.state, "synthetic", None) is not None
    else:
        healthy = getattr(app.state, "cbsa_client", None) is not None

    code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(
        status_code=code,
        content={
            "status": "ok" if healthy else "degraded",
            "mode": "synthetic" if settings.use_synthetic else "mtls",
        },
    )


__all__ = [
    "app",
    "TravelResponse",
    "TravelFailure",
    "lifespan",
    "get_travel",
    "healthz",
]
