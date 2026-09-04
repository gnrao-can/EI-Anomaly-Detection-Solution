"""
services/stats-canada-adapter/main.py
=====================================
FastAPI application for the Stats Canada Adapter (task 18.1).

Endpoints:
  - ``GET /statscan/lfs/{region_code}/{year_month}`` — returns canonical
    :class:`LabourForceData` records for an ESDC economic region and reference
    month (``YYYY-MM``). Serves from local fixtures in dev/test, otherwise from
    the upstream Statistics Canada LFS API, with Redis caching (30-day TTL) and
    graceful degradation.
  - ``GET /healthz`` — reports Redis connectivity / mock mode.

Fetch + fallback contract (Requirements 10.1-10.6):
  1. In dev/test (``serve_from_mock``): read from ``mock-data/stats-canada/``.
  2. In production: cache-first, then fetch from the LFS API (10 s connect /
     30 s read). On success, write to Redis under
     ``statscan:lfs:{region}:{ym}`` with a 30-day TTL.
  3. On upstream ``5xx``: fall back to the Redis cache and flag the response
     ``DATA_STALE``. If the cache is empty or older than 30 days, respond
     ``DATA_UNAVAILABLE``.
  4. On upstream ``4xx`` or parse failure with no usable cache:
     ``DATA_UNAVAILABLE``.

Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Path, status
from fastapi.responses import JSONResponse

from shared.auth import Role, require_role

try:  # sibling modules imported by bare name in the service directory
    from client import (  # type: ignore
        LFSParseError,
        StatsCanadaCache,
        StatsCanadaFetcher,
        UpstreamClientError,
        UpstreamServerError,
    )
    from config import get_settings  # type: ignore
except ImportError:  # pragma: no cover - when imported as a package
    from .client import (  # type: ignore
        LFSParseError,
        StatsCanadaCache,
        StatsCanadaFetcher,
        UpstreamClientError,
        UpstreamServerError,
    )
    from .config import get_settings  # type: ignore


#: ``YYYY-MM`` validation pattern for the year_month path parameter.
_YEAR_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

#: Response flags surfaced to callers.
FLAG_OK = "OK"
FLAG_DATA_STALE = "DATA_STALE"
FLAG_DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


# ---------------------------------------------------------------------------
# Cache construction (lazy, degrades gracefully when redis is unavailable)
# ---------------------------------------------------------------------------


def _build_cache(settings: Any) -> Any | None:
    """Construct a shared ``RedisCache`` if the ``redis`` package is available.

    Returns ``None`` when redis (or its transitive dependency) is not
    installed, in which case the adapter runs cache-less (fixtures still work).
    """
    try:
        from shared.cache import RedisCache  # imported lazily

        return RedisCache(
            url=settings.redis_url,
            password=getattr(settings, "redis_password", None),
            cluster_mode=getattr(settings, "redis_cluster_mode", False),
        )
    except Exception:  # noqa: BLE001 - redis/structlog absent or misconfigured
        return None


def _build_fetcher(settings: Any) -> StatsCanadaFetcher:
    """Construct a :class:`StatsCanadaFetcher` from settings."""
    return StatsCanadaFetcher(
        lfs_url=settings.statscan_lfs_url,
        connect_timeout=settings.statscan_connect_timeout_seconds,
        read_timeout=settings.statscan_read_timeout_seconds,
        api_version=settings.statscan_api_version,
        use_mock=settings.serve_from_mock,
        mock_dir=settings.statscan_mock_dir,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Wire settings, fetcher, and cache onto app state for the lifecycle."""
    settings = get_settings()
    app.state.settings = settings
    app.state.fetcher = _build_fetcher(settings)
    raw_cache = _build_cache(settings)
    if raw_cache is not None:
        try:
            await raw_cache.connect()
        except Exception:  # noqa: BLE001 - degrade to cache-less in dev
            raw_cache = None
    app.state.raw_cache = raw_cache
    app.state.cache = StatsCanadaCache(raw_cache)
    try:
        yield
    finally:
        if raw_cache is not None:
            try:
                await raw_cache.close()
            except Exception:  # noqa: BLE001
                pass


app = FastAPI(
    title="EI-ADS Stats Canada Adapter",
    version="0.1.0",
    lifespan=lifespan,
)

# ── OpenTelemetry distributed tracing (shared.telemetry, task 32.1 / Req 21.5) ──
# Single, self-contained hook: all tracer setup and span helpers live in
# shared/telemetry.py. No-op when the OpenTelemetry packages are absent.
from shared.telemetry import instrument_tracing  # noqa: E402

instrument_tracing(app, service_name="stats-canada-adapter")
# ── end OpenTelemetry tracing ──

# --- Prometheus metrics + /metrics endpoint (task 31.1) ---
from shared.metrics import instrument_app  # noqa: E402

instrument_app(app, kind="adapter")
# --- end metrics wiring ---


def _get_fetcher() -> StatsCanadaFetcher:
    fetcher = getattr(app.state, "fetcher", None)
    if fetcher is None:
        fetcher = _build_fetcher(get_settings())
        app.state.fetcher = fetcher
    return fetcher


def _get_cache() -> StatsCanadaCache:
    cache = getattr(app.state, "cache", None)
    if cache is None:
        cache = StatsCanadaCache(None)
        app.state.cache = cache
    return cache


def _get_settings() -> Any:
    return getattr(app.state, "settings", None) or get_settings()


def _records_body(records: list[Any], flag: str) -> dict[str, Any]:
    """Build the canonical JSON response body from LabourForceData records."""
    return {
        "flag": flag,
        "count": len(records),
        "records": [r.to_dict() for r in records],
    }


def _unavailable(region_code: str, year_month: str) -> JSONResponse:
    """503 response indicating no fresh or cached data is available."""
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "flag": FLAG_DATA_UNAVAILABLE,
            "region_code": region_code,
            "year_month": year_month,
            "records": [],
            "count": 0,
        },
    )


@app.get("/statscan/lfs/{region_code}/{year_month}")
async def get_lfs(
    region_code: str = Path(..., description="ESDC 2-char economic region code."),
    year_month: str = Path(..., description="Reference month as YYYY-MM."),
    _user: dict[str, Any] = Depends(require_role(Role.INVESTIGATOR)),
) -> Any:
    """Return LFS records for a region and reference month.

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
    fetcher = _get_fetcher()
    cache = _get_cache()

    # ── Dev/test: serve deterministic fixtures (Requirement 10.5) ──
    if settings.serve_from_mock:
        try:
            records = fetcher.fetch_from_mock(region_code, year_month)
        except LFSParseError:
            return _unavailable(region_code, year_month)
        if not records:
            return _unavailable(region_code, year_month)
        return JSONResponse(content=_records_body(records, FLAG_OK))

    # ── Production: cache-first, then live fetch ──
    cached = await cache.read(region_code, year_month)
    if cached is not None:
        records, is_stale = cached
        if not is_stale and records:
            return JSONResponse(content=_records_body(records, FLAG_OK))

    try:
        records = await fetcher.fetch_from_api(region_code, year_month)
    except UpstreamServerError:
        # 5xx → fall back to cache with DATA_STALE, else DATA_UNAVAILABLE.
        if cached is not None:
            records, _ = cached
            if records:
                return JSONResponse(
                    content=_records_body(records, FLAG_DATA_STALE)
                )
        return _unavailable(region_code, year_month)
    except (UpstreamClientError, LFSParseError, RuntimeError):
        # 4xx / parse failure / no httpx → use cache if any, else unavailable.
        if cached is not None:
            records, _ = cached
            if records:
                return JSONResponse(
                    content=_records_body(records, FLAG_DATA_STALE)
                )
        return _unavailable(region_code, year_month)

    if not records:
        return _unavailable(region_code, year_month)

    # Refresh the cache on a successful live fetch.
    await cache.write(region_code, year_month, records)
    return JSONResponse(content=_records_body(records, FLAG_OK))


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Report adapter health: mock mode and Redis connectivity."""
    settings = _get_settings()
    raw_cache = getattr(app.state, "raw_cache", None)
    redis_ok = False
    if raw_cache is not None:
        try:
            client = await raw_cache._get_client()  # noqa: SLF001 - health probe
            redis_ok = bool(await client.ping())
        except Exception:  # noqa: BLE001
            redis_ok = False

    serve_from_mock = settings.serve_from_mock
    # Healthy when serving fixtures (no external dep) or Redis is reachable.
    healthy = serve_from_mock or redis_ok
    code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(
        status_code=code,
        content={
            "status": "ok" if healthy else "degraded",
            "checks": {"redis": redis_ok, "mock_mode": serve_from_mock},
        },
    )


__all__ = [
    "app",
    "lifespan",
    "get_lfs",
    "healthz",
    "FLAG_OK",
    "FLAG_DATA_STALE",
    "FLAG_DATA_UNAVAILABLE",
]
