"""
services/business-registry-adapter/main.py
===========================================
FastAPI application for the Business Registry Adapter (task 20.1).

Responsibilities:
  - ``GET /registry/employer/{bn}`` — query the federal Corporations Canada
    registry plus the 13 provincial / territorial registries concurrently
    (``asyncio.gather``) with per-source error isolation. Some sources
    unreachable → ``PARTIAL_RESULT`` listing the unreachable jurisdictions; all
    sources unreachable → ``SERVICE_UNAVAILABLE`` (HTTP 503). Each returned
    registration's registered address is classified residential / commercial
    via the GeoNames Adapter, with an FSA-range fallback that flags
    ``UNVERIFIED_ZONING`` when GeoNames is unavailable.
  - ``GET /healthz`` — liveness / readiness for the adapter.

In development / test the adapter serves from ``mock-data/business-registries/``
and ``mock-data/provincial-registries/`` fixtures with no live network access.

Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, status
from fastapi.responses import JSONResponse

from shared.auth import Role, require_role

try:  # sibling modules imported by bare name inside the service directory
    from config import get_settings  # type: ignore
    from client import (  # type: ignore
        RegistryStatus,
        make_fixture_registry_caller,
        make_http_geonames_resolver,
        make_http_registry_caller,
        query_all_registries,
    )
except ImportError:  # pragma: no cover - when imported as a package
    from .config import get_settings  # type: ignore
    from .client import (  # type: ignore
        RegistryStatus,
        make_fixture_registry_caller,
        make_http_geonames_resolver,
        make_http_registry_caller,
        query_all_registries,
    )

try:
    import httpx  # type: ignore

    _HAS_HTTPX = True
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore
    _HAS_HTTPX = False


def _resolve_mock_dir(configured: str) -> str:
    """Resolve a mock-data directory to an absolute path.

    Configured paths are relative to the monorepo root; this walks up from the
    service directory (``services/business-registry-adapter``) to the repo root
    so the adapter finds fixtures regardless of the process CWD.
    """
    if os.path.isabs(configured):
        return configured
    repo_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..")
    )
    return os.path.join(repo_root, configured)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Wire the registry caller and GeoNames resolver based on environment.

    In development / test we use fixture-backed callers (no network). In other
    environments we build httpx-backed callers against the configured registry
    and GeoNames Adapter URLs.
    """
    settings = get_settings()
    app.state.settings = settings
    app.state.http_client = None

    federal_dir = _resolve_mock_dir(settings.business_registries_dir)
    provincial_dir = _resolve_mock_dir(settings.provincial_registries_dir)

    if settings.is_development or not _HAS_HTTPX:
        app.state.registry_caller = make_fixture_registry_caller(
            federal_dir, provincial_dir
        )
        # Dev/test relies on the FSA fallback (UNVERIFIED_ZONING) unless a
        # GeoNames resolver is explicitly injected by a test.
        app.state.geonames_resolver = None
    else:  # pragma: no cover - exercised only against live services
        client = httpx.AsyncClient()
        app.state.http_client = client
        app.state.registry_caller = make_http_registry_caller(settings, client)
        app.state.geonames_resolver = make_http_geonames_resolver(settings, client)

    try:
        yield
    finally:
        if app.state.http_client is not None:  # pragma: no cover
            try:
                await app.state.http_client.aclose()
            except Exception:  # noqa: BLE001
                pass


app = FastAPI(
    title="EI-ADS Business Registry Adapter",
    version="0.1.0",
    lifespan=lifespan,
)

# ── OpenTelemetry distributed tracing (shared.telemetry, task 32.1 / Req 21.5) ──
# Single, self-contained hook: all tracer setup and span helpers live in
# shared/telemetry.py. No-op when the OpenTelemetry packages are absent.
from shared.telemetry import instrument_tracing  # noqa: E402

instrument_tracing(app, service_name="business-registry-adapter")
# ── end OpenTelemetry tracing ──

# --- Prometheus metrics + /metrics endpoint (task 31.1) ---
from shared.metrics import instrument_app  # noqa: E402

instrument_app(app, kind="adapter")
# --- end metrics wiring ---


def _ensure_wired() -> None:
    """Lazily wire fixture-backed callers when the lifespan hasn't run.

    A ``TestClient`` used without entering the lifespan context (or a direct
    endpoint call) still gets working fixture-backed callers.
    """
    if getattr(app.state, "registry_caller", None) is not None:
        return
    settings = getattr(app.state, "settings", None) or get_settings()
    app.state.settings = settings
    federal_dir = _resolve_mock_dir(settings.business_registries_dir)
    provincial_dir = _resolve_mock_dir(settings.provincial_registries_dir)
    app.state.registry_caller = make_fixture_registry_caller(
        federal_dir, provincial_dir
    )
    if not hasattr(app.state, "geonames_resolver"):
        app.state.geonames_resolver = None


@app.get("/registry/employer/{bn}")
async def get_employer_registration(
    bn: str,
    _user: dict[str, Any] = Depends(require_role(Role.INVESTIGATOR)),
) -> Any:
    """Look up an employer BN across the federal + 13 provincial registries.

    Returns the canonical registration records with zoning classification, the
    overall status, and (when applicable) the list of unreachable jurisdictions.
    A ``SERVICE_UNAVAILABLE`` outcome is surfaced as HTTP 503.
    """
    _ensure_wired()

    result = await query_all_registries(
        bn,
        registry_caller=app.state.registry_caller,
        geonames_resolver=getattr(app.state, "geonames_resolver", None),
    )

    body = result.to_dict()
    if result.status is RegistryStatus.SERVICE_UNAVAILABLE:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=body
        )
    return JSONResponse(status_code=status.HTTP_200_OK, content=body)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Report adapter readiness.

    The adapter is healthy whenever its registry caller is wired (fixtures in
    dev/test, or an httpx client in higher environments).
    """
    _ensure_wired()
    wired = getattr(app.state, "registry_caller", None) is not None
    code = status.HTTP_200_OK if wired else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(
        status_code=code,
        content={"status": "ok" if wired else "degraded"},
    )


__all__ = ["app", "lifespan"]
