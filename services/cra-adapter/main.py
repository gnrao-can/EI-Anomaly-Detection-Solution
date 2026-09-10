"""
services/cra-adapter/main.py
============================
FastAPI application for the CRA Adapter (task 19.1).

Endpoints
---------
``GET /cra/t4/{sin_hash}/{tax_year}/{employer_bn}``
    Fetch a claimant's T4 record from the Canada Revenue Agency over the
    Government of Canada secure data-exchange channel (mTLS 1.3 in production;
    synthetic data in dev/test). The response is always:
      - **data-minimised** to ``ALLOWED_FIELDS`` (Requirement 11.2),
      - subject to the ">10 years old" retention rule (Requirement 11.7),
      - bounded by a **5-second total timeout**; on timeout a structured
        ``{"status": "UNAVAILABLE"}`` is returned within ~200 ms of the
        threshold (Requirements 11.3, 11.4),
      - **audited** to ClickHouse ``adapter_audit_log`` within 1 second, keyed by
        Claim_ID, component ``"cra-adapter"``, and the fields accessed
        (Requirements 11.5, 11.6). The raw SIN is never logged — only
        ``sin_hash``.

``GET /healthz``
    Liveness/readiness probe.

The CRA fetch itself is synchronous (``httpx.Client``); it is run in a worker
thread under ``asyncio.wait_for`` so the 5-second total timeout is enforced
without blocking the event loop.

Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 11.8
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, Query, status
from fastapi.responses import JSONResponse

from shared.auth import Role, require_role

try:  # sibling modules imported by bare name in the service directory
    from config import get_settings  # type: ignore
    from client import ALLOWED_FIELDS, CRAClient, T4Result  # type: ignore
    from audit import AuditWriter, ClickHouseAuditSink  # type: ignore
except ImportError:  # pragma: no cover - when imported as a package
    from .config import get_settings  # type: ignore
    from .client import ALLOWED_FIELDS, CRAClient, T4Result  # type: ignore
    from .audit import AuditWriter, ClickHouseAuditSink  # type: ignore

logger = logging.getLogger("cra-adapter.main")


# ---------------------------------------------------------------------------
# Lifespan handler
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Wire the CRA client and audit writer around the app lifecycle.

    Both are best-effort in development: absent mTLS material the client
    synthesises T4 data, and an unavailable ClickHouse leaves the audit sink to
    no-op gracefully.
    """
    settings = get_settings()
    app.state.settings = settings

    app.state.cra_client = CRAClient(settings)

    audit_sink = ClickHouseAuditSink(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
    )
    app.state.audit_writer = AuditWriter(audit_sink, budget_ms=settings.audit_budget_ms)

    try:
        yield
    finally:
        client = getattr(app.state, "cra_client", None)
        if client is not None:
            client.close()


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="EI-ADS CRA Adapter",
    version="0.1.0",
    lifespan=lifespan,
)

# ── OpenTelemetry distributed tracing (shared.telemetry, task 32.1 / Req 21.5) ──
# Single, self-contained hook: all tracer setup and span helpers live in
# shared/telemetry.py. No-op when the OpenTelemetry packages are absent.
from shared.telemetry import instrument_tracing  # noqa: E402

instrument_tracing(app, service_name="cra-adapter")
# ── end OpenTelemetry tracing ──

# --- Prometheus metrics + /metrics endpoint (task 31.1) ---
from shared.metrics import instrument_app  # noqa: E402

instrument_app(app, kind="adapter")
# --- end metrics wiring ---


def _get_client(app_: FastAPI) -> CRAClient:
    client = getattr(app_.state, "cra_client", None)
    if client is None:
        client = CRAClient(getattr(app_.state, "settings", None) or get_settings())
        app_.state.cra_client = client
    return client


def _get_audit_writer(app_: FastAPI) -> AuditWriter:
    writer = getattr(app_.state, "audit_writer", None)
    if writer is None:
        writer = AuditWriter(None)
        app_.state.audit_writer = writer
    return writer


async def _fetch_with_timeout(
    client: CRAClient,
    sin_hash: str,
    tax_year: int,
    employer_bn: str,
    *,
    total_timeout_s: float,
    margin_ms: float,
) -> Optional[T4Result]:
    """Fetch a T4 within the total timeout, returning ``None`` on timeout.

    The synchronous fetch is offloaded to a worker thread and wrapped in
    ``asyncio.wait_for`` with a deadline set ``margin_ms`` *before* the hard
    total-timeout threshold, so a structured UNAVAILABLE is produced within
    ~200 ms of the threshold rather than after it (Requirement 11.4).
    """
    # Reserve the margin so we return UNAVAILABLE just inside the threshold.
    effective_timeout_s = max(0.0, total_timeout_s - (margin_ms / 1000.0))
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(client.get_t4, sin_hash, tax_year, employer_bn),
            timeout=effective_timeout_s,
        )
    except asyncio.TimeoutError:
        return None


@app.get("/cra/t4/{sin_hash}/{tax_year}/{employer_bn}")
async def get_t4(
    sin_hash: str,
    tax_year: int,
    employer_bn: str,
    claim_id: str = Query(default="", description="Claim_ID for the audit trail."),
    _user: dict[str, Any] = Depends(require_role(Role.INVESTIGATOR)),
) -> JSONResponse:
    """Fetch a data-minimised T4 record for the given key.

    Returns 200 with ``{"status": "OK", "record": {...allowed fields...}}`` on
    success, 200 with ``{"status": "NOT_FOUND"}`` when no (in-retention) record
    exists, and 200 with ``{"status": "UNAVAILABLE"}`` when the 5-second total
    timeout is hit. Every outcome is audited to ClickHouse within 1 second.
    """
    settings = getattr(app.state, "settings", None) or get_settings()
    client = _get_client(app)
    audit = _get_audit_writer(app)

    start = time.monotonic()
    result = await _fetch_with_timeout(
        client,
        sin_hash,
        tax_year,
        employer_bn,
        total_timeout_s=settings.cra_timeout_seconds,
        margin_ms=settings.cra_unavailable_margin_ms,
    )
    duration_ms = (time.monotonic() - start) * 1000.0

    if result is None:
        # Timed out — structured UNAVAILABLE (Requirement 11.4).
        await audit.log_access(
            claim_id,
            fields_accessed=[],
            sin_hash=sin_hash,
            outcome="UNAVAILABLE",
            duration_ms=duration_ms,
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "UNAVAILABLE"},
        )

    if result.status == "OK" and result.record is not None:
        fields_accessed = sorted(result.record.keys())
        await audit.log_access(
            claim_id,
            fields_accessed=fields_accessed,
            sin_hash=sin_hash,
            outcome="SUCCESS",
            duration_ms=duration_ms,
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "OK", "record": result.record},
        )

    # NOT_FOUND (absent upstream or discarded by the retention rule).
    await audit.log_access(
        claim_id,
        fields_accessed=[],
        sin_hash=sin_hash,
        outcome="NOT_FOUND",
        duration_ms=duration_ms,
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "NOT_FOUND"},
    )


@app.get("/cra/payroll/{sin_hash}/{employer_bn}")
async def get_payroll(
    sin_hash: str,
    employer_bn: str,
    start: str = Query(default="", description="Employment period start (ISO date)."),
    end: str = Query(default="", description="Employment period end (ISO date)."),
    claim_id: str = Query(default="", description="Claim_ID for the audit trail."),
    _user: dict[str, Any] = Depends(require_role(Role.INVESTIGATOR)),
) -> JSONResponse:
    """Report whether a SIN appears in an employer's CRA payroll for a period.

    Backs the Document Forgery Engine's payroll cross-reference (Requirement
    7.7): a confirmed-absent SIN is a hard forgery signal. Returns 200 with
    ``{"status": "OK", "sin_in_payroll": <bool>}`` on success, and 200 with
    ``{"status": "UNAVAILABLE"}`` when the 5-second total timeout is hit — an
    unavailable adapter is NOT evidence of absence, so the engine must not
    false-flag on it. Every outcome is audited to ClickHouse within 1 second;
    the raw SIN is never logged (only ``sin_hash``).
    """
    settings = getattr(app.state, "settings", None) or get_settings()
    client = _get_client(app)
    audit = _get_audit_writer(app)

    effective_timeout_s = max(
        0.0,
        settings.cra_timeout_seconds - (settings.cra_unavailable_margin_ms / 1000.0),
    )
    start_t = time.monotonic()
    try:
        present = await asyncio.wait_for(
            asyncio.to_thread(
                client.get_payroll_presence, sin_hash, employer_bn, start, end
            ),
            timeout=effective_timeout_s,
        )
    except asyncio.TimeoutError:
        present = None
    duration_ms = (time.monotonic() - start_t) * 1000.0

    if present is None:
        await audit.log_access(
            claim_id,
            fields_accessed=[],
            sin_hash=sin_hash,
            outcome="UNAVAILABLE",
            duration_ms=duration_ms,
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "UNAVAILABLE"},
        )

    await audit.log_access(
        claim_id,
        fields_accessed=["payroll_presence"],
        sin_hash=sin_hash,
        outcome="SUCCESS",
        duration_ms=duration_ms,
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "OK", "sin_in_payroll": bool(present)},
    )


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Report basic readiness for the CRA Adapter."""
    client = getattr(app.state, "cra_client", None)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "ok",
            "synthetic_mode": client.uses_synthetic_data if client else True,
            "allowed_fields": sorted(ALLOWED_FIELDS),
        },
    )


__all__ = ["app", "lifespan", "get_t4", "get_payroll", "healthz"]
