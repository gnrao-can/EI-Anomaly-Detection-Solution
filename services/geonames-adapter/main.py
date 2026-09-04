"""
services/geonames-adapter/main.py
=================================
FastAPI application for the GeoNames Adapter.

Responsibilities (task 22.1, Requirements 14.1-14.6):

  * ``GET /geonames/resolve/{postal_code}`` — resolve a Canadian postal code /
    FSA to (latitude, longitude) via the GeoNames REST API. On any failure it
    returns ``RESOLUTION_FAILED`` and NEVER default/fabricated coordinates.
  * ``POST /geonames/distance`` — validate both coordinates (out-of-bounds ->
    ``INVALID_COORDINATE`` with NO distance computed) then compute the Haversine
    distance rounded to 3 decimal places.
  * ``GET /healthz`` — Redis / local-dump connectivity report.

Caching (design "Redis Caching Strategy"):
  * ``geonames:coord:{postal_code}`` — 90-day TTL (coordinate resolution).
  * ``geonames:dist:{hash(A,B)}``   — 7-day TTL (computed distances).

Fallback / dev-test:
  * When the GeoNames API is unavailable (or in dev/test), resolution falls back
    to the local dump at ``mock-data/geonames/`` (must be <= 90 days old).
  * In dev/test the adapter ALWAYS uses the local dump; no network calls.

Rate limiting (design: 2,000 credits/hour):
  * A per-hour Redis counter gates outbound GeoNames API calls; a 429 from the
    API triggers exponential backoff and a fall back to the local dump.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from shared.auth import Role, require_role
from shared.cache import RedisCache
from shared.cache_keys import (
    GEONAMES_COORD_TTL_SECONDS,
    GEONAMES_DIST_TTL_SECONDS,
    geonames_coord_key,
    geonames_dist_key,
)

try:  # sibling modules imported by bare name in the service directory
    from config import get_settings  # type: ignore
    from geo import (  # type: ignore
        INVALID_COORDINATE,
        RESOLUTION_FAILED,
        Coordinate,
        InvalidCoordinateError,
        ResolutionFailedError,
        haversine,
        is_valid_coordinate,
    )
except ImportError:  # pragma: no cover - when imported as a package
    from .config import get_settings  # type: ignore
    from .geo import (  # type: ignore
        INVALID_COORDINATE,
        RESOLUTION_FAILED,
        Coordinate,
        InvalidCoordinateError,
        ResolutionFailedError,
        haversine,
        is_valid_coordinate,
    )

try:  # httpx is used for the outbound GeoNames REST call.
    import httpx  # type: ignore

    _HAS_HTTPX = True
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore
    _HAS_HTTPX = False


# ---------------------------------------------------------------------------
# Rate-limit constants
# ---------------------------------------------------------------------------

_RATE_LIMIT_KEY = "geonames:ratelimit:{hour}"
_RATE_LIMIT_WINDOW_SECONDS = 3600
# Backoff schedule (seconds) applied on a 429 from the GeoNames API.
_BACKOFF_SCHEDULE = (0.5, 1.0, 2.0)


# ---------------------------------------------------------------------------
# Local dump loader
# ---------------------------------------------------------------------------


class LocalDump:
    """Offline postal-code -> coordinate lookup backed by a JSON dump file.

    The dump must have been refreshed within ``max_age_days`` (design: 90 days);
    a stale dump still resolves (so dev/test never breaks) but ``is_stale`` is
    reported via ``/healthz``.
    """

    def __init__(self, path: Path, max_age_days: int = 90) -> None:
        self._path = path
        self._max_age_days = max_age_days
        self._entries: dict[str, dict[str, Any]] = {}
        self._generated_at: Optional[datetime] = None
        self._loaded = False

    def load(self) -> None:
        """Load (or reload) the dump from disk. Missing/invalid -> empty dump."""
        self._loaded = True
        self._entries = {}
        self._generated_at = None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        entries = raw.get("entries", {})
        # Normalise keys so lookups are case/space-insensitive.
        self._entries = {self._norm(k): v for k, v in entries.items()}
        meta = raw.get("_metadata", {})
        gen = meta.get("generated_at")
        if gen:
            try:
                self._generated_at = datetime.fromisoformat(
                    gen.replace("Z", "+00:00")
                )
            except ValueError:
                self._generated_at = None

    @staticmethod
    def _norm(postal_code: str) -> str:
        return (postal_code or "").replace(" ", "").upper()

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def is_empty(self) -> bool:
        return not self._entries

    @property
    def is_stale(self) -> bool:
        """True if the dump is older than ``max_age_days`` (design: 90 days)."""
        if self._generated_at is None:
            return True
        age = datetime.now(timezone.utc) - self._generated_at
        return age.days > self._max_age_days

    def resolve(self, postal_code: str) -> Optional[Coordinate]:
        """Return the ``Coordinate`` for a postal code / FSA, or ``None``.

        Tries the full postal code first, then falls back to the 3-char FSA.
        Never returns fabricated coordinates.
        """
        if not self._loaded:
            self.load()
        key = self._norm(postal_code)
        entry = self._entries.get(key)
        if entry is None and len(key) > 3:
            entry = self._entries.get(key[:3])  # FSA fallback
        if entry is None:
            return None
        try:
            lat = float(entry["latitude"])
            lon = float(entry["longitude"])
        except (KeyError, TypeError, ValueError):
            return None
        if not is_valid_coordinate(lat, lon):
            return None
        return Coordinate(latitude=lat, longitude=lon)


# ---------------------------------------------------------------------------
# Resolver — coordinates the API call, cache, rate limit, and local fallback
# ---------------------------------------------------------------------------


class GeoNamesResolver:
    """Resolves postal codes to coordinates and computes cached distances."""

    def __init__(self, settings: Any, cache: Optional[RedisCache], local_dump: LocalDump) -> None:
        self._settings = settings
        self._cache = cache
        self._local_dump = local_dump

    # ── Rate limiting ────────────────────────────────────────────────

    async def _check_and_increment_rate_limit(self) -> bool:
        """Return True if an API call is permitted under the per-hour budget.

        Uses a Redis counter keyed by the current hour with a 1-hour TTL. When
        Redis is unavailable, we fail open (allow the call) since the API itself
        will 429 if the true limit is exceeded.
        """
        if self._cache is None:
            return True
        hour = int(time.time() // _RATE_LIMIT_WINDOW_SECONDS)
        key = _RATE_LIMIT_KEY.format(hour=hour)
        try:
            client = await self._cache._get_client()  # noqa: SLF001 - internal use
            count = await client.incr(key)
            if count == 1:
                await client.expire(key, _RATE_LIMIT_WINDOW_SECONDS)
            return int(count) <= int(self._settings.geonames_rate_limit_per_hour)
        except Exception:  # noqa: BLE001 - fail open on cache error
            return True

    # ── Coordinate resolution ────────────────────────────────────────

    async def resolve(self, postal_code: str) -> Coordinate:
        """Resolve ``postal_code`` to a ``Coordinate``.

        Order of resolution:
          1. Redis cache (``geonames:coord:{postal_code}``).
          2. In dev/test: local dump only (no network).
          3. Otherwise: GeoNames REST API (subject to rate limiting); on any
             failure or 429 backoff exhaustion, fall back to the local dump.

        Raises:
            ResolutionFailedError: If the postal code cannot be resolved by any
                source. NO default coordinates are ever returned.
        """
        # 1. Cache
        cached = await self._get_cached_coord(postal_code)
        if cached is not None:
            return cached

        coord: Optional[Coordinate] = None

        if self._settings.is_development:
            # 2. Dev/test: always use the local dump; never hit the network.
            coord = self._local_dump.resolve(postal_code)
        else:
            # 3. Production: try the API, then fall back to the local dump.
            coord = await self._resolve_via_api(postal_code)
            if coord is None:
                coord = self._local_dump.resolve(postal_code)

        if coord is None:
            raise ResolutionFailedError(postal_code)

        await self._set_cached_coord(postal_code, coord)
        return coord

    async def _resolve_via_api(self, postal_code: str) -> Optional[Coordinate]:
        """Call the GeoNames REST API. Returns ``None`` on any failure."""
        if not _HAS_HTTPX:
            return None
        if not await self._check_and_increment_rate_limit():
            # Over budget for this hour — do not call; caller falls back.
            return None

        url = f"{self._settings.geonames_api_url.rstrip('/')}/postalCodeSearchJSON"
        params = {
            "postalcode": postal_code,
            "country": "CA",
            "maxRows": 1,
            "username": self._settings.geonames_username,
        }
        timeout = httpx.Timeout(
            connect=self._settings.geonames_connect_timeout_seconds,
            read=self._settings.geonames_read_timeout_seconds,
            write=self._settings.geonames_read_timeout_seconds,
            pool=self._settings.geonames_connect_timeout_seconds,
        )
        for attempt, backoff in enumerate((*_BACKOFF_SCHEDULE, None)):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.get(url, params=params)
                if resp.status_code == 429:
                    # Rate limited by the API — back off, then give up -> fallback.
                    if backoff is None:
                        return None
                    await asyncio.sleep(backoff)
                    continue
                if resp.status_code >= 400:
                    return None
                data = resp.json()
                return self._parse_api_response(data)
            except Exception:  # noqa: BLE001 - any transport/parse error -> fallback
                return None
        return None

    @staticmethod
    def _parse_api_response(data: dict[str, Any]) -> Optional[Coordinate]:
        """Extract a validated coordinate from a GeoNames JSON response."""
        codes = data.get("postalCodes") or []
        if not codes:
            return None
        first = codes[0]
        try:
            lat = float(first["lat"])
            lon = float(first["lng"])
        except (KeyError, TypeError, ValueError):
            return None
        if not is_valid_coordinate(lat, lon):
            return None
        return Coordinate(latitude=lat, longitude=lon)

    async def _get_cached_coord(self, postal_code: str) -> Optional[Coordinate]:
        if self._cache is None:
            return None
        payload = await self._cache.get_json(geonames_coord_key(postal_code))
        if not payload:
            return None
        try:
            lat = float(payload["latitude"])
            lon = float(payload["longitude"])
        except (KeyError, TypeError, ValueError):
            return None
        if not is_valid_coordinate(lat, lon):
            return None
        return Coordinate(latitude=lat, longitude=lon)

    async def _set_cached_coord(self, postal_code: str, coord: Coordinate) -> None:
        if self._cache is None:
            return
        await self._cache.set_json_with_ttl(
            geonames_coord_key(postal_code),
            {"latitude": coord.latitude, "longitude": coord.longitude},
            GEONAMES_COORD_TTL_SECONDS,
        )

    # ── Distance ──────────────────────────────────────────────────────

    async def distance(
        self, lat1: float, lon1: float, lat2: float, lon2: float
    ) -> tuple[float, bool]:
        """Compute the Haversine distance (km, 3 dp) between two points.

        Coordinates are validated by ``haversine`` (out-of-bounds ->
        ``InvalidCoordinateError`` with no computation). Results are cached under
        ``geonames:dist:{hash(A,B)}`` with a 7-day TTL; the cache key is
        symmetric so ``(A,B)`` and ``(B,A)`` share an entry.

        Returns:
            ``(distance_km, cache_hit)``.

        Raises:
            InvalidCoordinateError: If either coordinate is out of bounds.
        """
        # Validate BEFORE touching the cache so out-of-bounds never computes or
        # returns a stored value.
        if not is_valid_coordinate(lat1, lon1):
            raise InvalidCoordinateError(lat=lat1, lon=lon1)
        if not is_valid_coordinate(lat2, lon2):
            raise InvalidCoordinateError(lat=lat2, lon=lon2)

        key = geonames_dist_key(lat1, lon1, lat2, lon2)
        if self._cache is not None:
            cached = await self._cache.get_json(key)
            if cached is not None and "distance_km" in cached:
                try:
                    return float(cached["distance_km"]), True
                except (TypeError, ValueError):
                    pass

        distance_km = haversine(lat1, lon1, lat2, lon2)
        if self._cache is not None:
            await self._cache.set_json_with_ttl(
                key, {"distance_km": distance_km}, GEONAMES_DIST_TTL_SECONDS
            )
        return distance_km, False


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class CoordinateResolution(BaseModel):
    """200 response for a successful postal-code resolution."""

    postal_code: str
    latitude: float
    longitude: float
    source: str = Field(description="'cache', 'api', or 'local_dump'.")


class ResolutionError(BaseModel):
    """422 response when a postal code cannot be resolved."""

    error: str = RESOLUTION_FAILED
    postal_code: str


class DistanceRequest(BaseModel):
    """Inbound body for ``POST /geonames/distance``."""

    lat1: float
    lon1: float
    lat2: float
    lon2: float


class DistanceResponse(BaseModel):
    """200 response carrying the computed distance."""

    distance_km: float
    cache_hit: bool = False


class InvalidCoordinateResponse(BaseModel):
    """422 response for an out-of-bounds coordinate (no distance computed)."""

    error: str = INVALID_COORDINATE
    lat: float
    lon: float


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open the Redis cache and load the local dump around the app lifecycle."""
    settings = get_settings()
    app.state.settings = settings

    # ── Local dump ──────────────────────────────────────────────
    local_dump = LocalDump(
        settings.local_dump_path, settings.geonames_local_dump_max_age_days
    )
    local_dump.load()
    app.state.local_dump = local_dump

    # ── Redis cache (best-effort) ───────────────────────────────
    cache: Optional[RedisCache] = None
    try:
        cache = RedisCache(url=settings.redis_url, cluster_mode=False)
        await cache.connect()
    except Exception:  # noqa: BLE001 - degrade gracefully in dev
        cache = None
    app.state.cache = cache

    app.state.resolver = GeoNamesResolver(settings, cache, local_dump)

    try:
        yield
    finally:
        if cache is not None:
            try:
                await cache.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="EI-ADS GeoNames Adapter",
    version="0.1.0",
    lifespan=lifespan,
)

# ── OpenTelemetry distributed tracing (shared.telemetry, task 32.1 / Req 21.5) ──
# Single, self-contained hook: all tracer setup and span helpers live in
# shared/telemetry.py. No-op when the OpenTelemetry packages are absent.
from shared.telemetry import instrument_tracing  # noqa: E402

instrument_tracing(app, service_name="geonames-adapter")
# ── end OpenTelemetry tracing ──

# --- Prometheus metrics + /metrics endpoint (task 31.1) ---
from shared.metrics import instrument_app  # noqa: E402

instrument_app(app, kind="adapter")
# --- end metrics wiring ---


def _get_resolver() -> GeoNamesResolver:
    """Return the resolver, building a fallback one if lifespan has not run."""
    resolver = getattr(app.state, "resolver", None)
    if resolver is None:
        settings = getattr(app.state, "settings", None) or get_settings()
        local_dump = getattr(app.state, "local_dump", None)
        if local_dump is None:
            local_dump = LocalDump(
                settings.local_dump_path, settings.geonames_local_dump_max_age_days
            )
            local_dump.load()
            app.state.local_dump = local_dump
        cache = getattr(app.state, "cache", None)
        resolver = GeoNamesResolver(settings, cache, local_dump)
        app.state.resolver = resolver
    return resolver


@app.get(
    "/geonames/resolve/{postal_code}",
    response_model=CoordinateResolution,
    responses={422: {"model": ResolutionError}},
)
async def resolve_postal_code(
    postal_code: str,
    _user: dict[str, Any] = Depends(require_role(Role.INVESTIGATOR)),
) -> Any:
    """Resolve a postal code / FSA to coordinates.

    On any failure returns HTTP 422 with ``RESOLUTION_FAILED`` and never
    default/fabricated coordinates.
    """
    resolver = _get_resolver()
    try:
        coord = await resolver.resolve(postal_code)
    except ResolutionFailedError:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": RESOLUTION_FAILED, "postal_code": postal_code},
        )
    return CoordinateResolution(
        postal_code=postal_code,
        latitude=coord.latitude,
        longitude=coord.longitude,
        source="resolved",
    )


@app.post(
    "/geonames/distance",
    response_model=DistanceResponse,
    responses={422: {"model": InvalidCoordinateResponse}},
)
async def compute_distance(
    req: DistanceRequest,
    _user: dict[str, Any] = Depends(require_role(Role.INVESTIGATOR)),
) -> Any:
    """Compute the Haversine distance (km, 3 dp) between two coordinates.

    If either coordinate is out of bounds, returns HTTP 422 with
    ``INVALID_COORDINATE`` and NO distance is computed.
    """
    resolver = _get_resolver()
    try:
        distance_km, cache_hit = await resolver.distance(
            req.lat1, req.lon1, req.lat2, req.lon2
        )
    except InvalidCoordinateError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": INVALID_COORDINATE, "lat": exc.lat, "lon": exc.lon},
        )
    return DistanceResponse(distance_km=distance_km, cache_hit=cache_hit)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Report Redis and local-dump health.

    The adapter is considered healthy when the local dump is loaded and
    non-empty (it is the offline source of truth); Redis is reported but not
    required, since caching degrades gracefully.
    """
    local_dump: Optional[LocalDump] = getattr(app.state, "local_dump", None)
    cache: Optional[RedisCache] = getattr(app.state, "cache", None)

    dump_ok = local_dump is not None and not local_dump.is_empty
    dump_stale = local_dump.is_stale if local_dump is not None else True

    redis_ok = False
    if cache is not None:
        try:
            client = await cache._get_client()  # noqa: SLF001
            redis_ok = bool(await client.ping())
        except Exception:  # noqa: BLE001
            redis_ok = False

    healthy = dump_ok
    code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(
        status_code=code,
        content={
            "status": "ok" if healthy else "degraded",
            "checks": {
                "local_dump_loaded": dump_ok,
                "local_dump_stale": dump_stale,
                "redis": redis_ok,
            },
        },
    )


__all__ = [
    "app",
    "lifespan",
    "GeoNamesResolver",
    "LocalDump",
    "DistanceRequest",
    "DistanceResponse",
    "CoordinateResolution",
]
