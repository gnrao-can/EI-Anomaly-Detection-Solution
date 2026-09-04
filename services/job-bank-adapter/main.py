"""
services/job-bank-adapter/main.py
=================================
FastAPI application for the Job Bank Adapter (task 24.1).

Endpoints:
  * ``GET /jobbank/postings/{noc}/{region}/{year_month}`` — returns the active
    posting count and median advertised hourly wage for a NOC code within an
    ESDC economic region, for the given reference month (``YYYY-MM``). The
    response body is the canonical serialised :class:`JobBankData` plus a
    ``flag`` describing the data provenance.
  * ``GET /healthz`` — liveness / readiness.

Fetch + fallback contract (Requirements 16.1-16.4):
  1. In dev/test (``serve_from_snapshot``): always serve from the open-data
     snapshot in ``mock-data/job-bank/`` (Requirement 16.4).
  2. In production: cache-first (``job_bank:postings:{noc}:{region}:{ym}``, 7-day
     TTL), then a single live API query (Requirement 16.1, 16.2).
  3. On API unavailability: serve from the snapshot when it is no older than 7
     days, flagging the response ``DATA_STALE`` (Requirement 16.3). If the
     snapshot is missing or stale, respond ``DATA_UNAVAILABLE``.

The median wage crosses the wire as a string (never a JSON number) to preserve
exact ``Decimal`` precision (supports the P12 round-trip property).

Requirements: 16.1, 16.2, 16.3, 16.4
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, Path, Query, status
from fastapi.responses import JSONResponse

from shared.auth import Role, require_role

try:  # sibling modules imported by bare name in the service directory
    from client import (  # type: ignore
        JobBankCache,
        JobBankFetcher,
        JobBankParseError,
        JobBankSnapshotStore,
        JobBankUnavailableError,
        SnapshotMissingError,
        SnapshotStaleError,
    )
    from config import get_settings  # type: ignore
    from serialization import JobBankData, serialise_job_bank_data  # type: ignore
except ImportError:  # pragma: no cover - when imported as a package
    from .client import (  # type: ignore
        JobBankCache,
        JobBankFetcher,
        JobBankParseError,
        JobBankSnapshotStore,
        JobBankUnavailableError,
        SnapshotMissingError,
        SnapshotStaleError,
    )
    from .config import get_settings  # type: ignore
    from .serialization import JobBankData, serialise_job_bank_data  # type: ignore


#: ``YYYY-MM`` validation pattern for the year_month path parameter.
_YEAR_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

#: Response provenance flags surfaced to callers.
FLAG_OK = "OK"
FLAG_DATA_STALE = "DATA_STALE"
FLAG_DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


# ---------------------------------------------------------------------------
# Collaborator construction
# ---------------------------------------------------------------------------


def _build_cache(settings: Any) -> Any | None:
    """Construct a shared ``RedisCache`` if ``redis`` is available, else None."""
    try:
        from shared.cache import RedisCache  # imported lazily

        return RedisCache(
            url=settings.redis_url,
            password=getattr(settings, "redis_password", None),
            cluster_mode=getattr(settings, "redis_cluster_mode", False),
        )
    except Exception:  # noqa: BLE001 - redis/structlog absent or misconfigured
        return None


def _build_fetcher(settings: Any) -> JobBankFetcher:
    return JobBankFetcher(
        base_url=settings.job_bank_api_base_url,
        api_key=getattr(settings, "job_bank_api_key", ""),
        timeout=settings.job_bank_timeout,
    )


def _build_snapshot(settings: Any) -> JobBankSnapshotStore:
    return JobBankSnapshotStore(
        settings.job_bank_snapshot_dir,
        max_age_days=settings.job_bank_snapshot_max_age_days,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.fetcher = _build_fetcher(settings)
    app.state.snapshot = _build_snapshot(settings)

    raw_cache = _build_cache(settings)
    if raw_cache is not None:
        try:
            await raw_cache.connect()
        except Exception:  # noqa: BLE001 - degrade to cache-less in dev
            raw_cache = None
    app.state.raw_cache = raw_cache
    app.state.cache = JobBankCache(raw_cache)

    try:
        yield
    finally:
        if raw_cache is not None:
            try:
                await raw_cache.close()
            except Exception:  # noqa: BLE001
                pass


app = FastAPI(
    title="EI-ADS Job Bank Adapter",
    version="0.1.0",
    lifespan=lifespan,
)

# ── OpenTelemetry distributed tracing (shared.telemetry, task 32.1 / Req 21.5) ──
# Single, self-contained hook: all tracer setup and span helpers live in
# shared/telemetry.py. No-op when the OpenTelemetry packages are absent.
from shared.telemetry import instrument_tracing  # noqa: E402

instrument_tracing(app, service_name="job-bank-adapter")
# ── end OpenTelemetry tracing ──

# --- Prometheus metrics + /metrics endpoint (task 31.1) ---
from shared.metrics import instrument_app  # noqa: E402

instrument_app(app, kind="adapter")
# --- end metrics wiring ---


# ---------------------------------------------------------------------------
# State accessors (with lazy fallbacks so the app works outside lifespan)
# ---------------------------------------------------------------------------


def _get_settings() -> Any:
    return getattr(app.state, "settings", None) or get_settings()


def _get_fetcher() -> JobBankFetcher:
    fetcher = getattr(app.state, "fetcher", None)
    if fetcher is None:
        fetcher = _build_fetcher(_get_settings())
        app.state.fetcher = fetcher
    return fetcher


def _get_snapshot() -> JobBankSnapshotStore:
    snapshot = getattr(app.state, "snapshot", None)
    if snapshot is None:
        snapshot = _build_snapshot(_get_settings())
        app.state.snapshot = snapshot
    return snapshot


def _get_cache() -> JobBankCache:
    cache = getattr(app.state, "cache", None)
    if cache is None:
        cache = JobBankCache(None)
        app.state.cache = cache
    return cache


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _body(data: JobBankData, flag: str) -> dict[str, Any]:
    """Canonical response body: serialised JobBankData plus a provenance flag."""
    payload = serialise_job_bank_data(data)
    payload["flag"] = flag
    return payload


def _unavailable(noc: str, region: str, year_month: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "flag": FLAG_DATA_UNAVAILABLE,
            "noc_code": noc,
            "region_code": region,
            "reference_month": year_month,
        },
    )


def _serve_from_snapshot(
    noc: str, region: str, year_month: str, *, flag: str
) -> Optional[JSONResponse]:
    """Attempt a snapshot read; return a JSONResponse or ``None`` if unusable."""
    snapshot = _get_snapshot()
    try:
        data = snapshot.load(noc, region, data_stale=(flag == FLAG_DATA_STALE))
    except (SnapshotMissingError, SnapshotStaleError, JobBankParseError):
        return None
    return JSONResponse(content=_body(data, flag))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/jobbank/postings/{noc}/{region}/{year_month}")
async def get_postings(
    noc: str = Path(..., description="NOC 2021 code."),
    region: str = Path(..., description="ESDC economic region code."),
    year_month: str = Path(..., description="Reference month as YYYY-MM."),
    period_start: str = Query(
        default="", description="Optional claim-period start (YYYY-MM-DD)."
    ),
    period_end: str = Query(
        default="", description="Optional claim-period end (YYYY-MM-DD)."
    ),
    _user: dict[str, Any] = Depends(require_role(Role.INVESTIGATOR)),
) -> Any:
    """Return Job Bank posting data for a NOC code within an ESDC region.

    See the module docstring for the full fetch / fallback contract.
    """
    if not _YEAR_MONTH_RE.match(year_month):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": {
                    "code": "INVALID_YEAR_MONTH",
                    "message": "year_month must be formatted as YYYY-MM.",
                }
            },
        )

    settings = _get_settings()

    # ── Dev/test: always serve from the open-data snapshot (Req 16.4) ──
    if settings.serve_from_snapshot:
        served = _serve_from_snapshot(noc, region, year_month, flag=FLAG_OK)
        return served or _unavailable(noc, region, year_month)

    # ── Production: cache-first ──
    cache = _get_cache()
    cached = await cache.read(noc, region, year_month)
    if cached is not None:
        return JSONResponse(content=_body(cached, FLAG_OK))

    # ── Live API query (single attempt) ──
    try:
        data = await _get_fetcher().fetch(
            noc,
            region,
            period_start=period_start or None,
            period_end=period_end or None,
        )
    except (JobBankUnavailableError, JobBankParseError):
        # On API unavailability, fall back to the snapshot with DATA_STALE
        # (Requirement 16.3). If the snapshot is missing or > 7 days old,
        # respond DATA_UNAVAILABLE.
        served = _serve_from_snapshot(
            noc, region, year_month, flag=FLAG_DATA_STALE
        )
        return served or _unavailable(noc, region, year_month)

    # Cache the fresh result under the 7-day-TTL key and return it.
    await cache.write(year_month, data)
    return JSONResponse(content=_body(data, FLAG_OK))


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Report adapter health: snapshot mode and Redis connectivity."""
    settings = _get_settings()
    raw_cache = getattr(app.state, "raw_cache", None)
    redis_ok = False
    if raw_cache is not None:
        try:
            client = await raw_cache._get_client()  # noqa: SLF001 - health probe
            redis_ok = bool(await client.ping())
        except Exception:  # noqa: BLE001
            redis_ok = False

    serve_from_snapshot = settings.serve_from_snapshot
    # Healthy when serving the snapshot (no external dep) or Redis is reachable.
    healthy = serve_from_snapshot or redis_ok
    code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(
        status_code=code,
        content={
            "status": "ok" if healthy else "degraded",
            "checks": {"redis": redis_ok, "snapshot_mode": serve_from_snapshot},
        },
    )


__all__ = [
    "app",
    "lifespan",
    "get_postings",
    "healthz",
    "FLAG_OK",
    "FLAG_DATA_STALE",
    "FLAG_DATA_UNAVAILABLE",
]
