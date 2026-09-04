"""
services/geographic-anomaly-engine/client.py
=============================================
Async client for the GeoNames Adapter, used by the Geographic Anomaly Engine to
resolve postal codes / work locations to coordinates.

The engine resolves TWO coordinates per claim:
  * the claimant's residential postal code, and
  * the employer work-location,
by calling the GeoNames Adapter's ``GET /geonames/resolve/{postal_code}``
endpoint (see ``services/geonames-adapter/main.py``).

Adapter response contract (task 22.1):
  * 200 -> ``{"postal_code", "latitude", "longitude", "source"}``
  * 422 -> ``{"error": "RESOLUTION_FAILED", "postal_code": ...}``

On ``RESOLUTION_FAILED`` — or ANY transport/timeout/parse error — the client
raises :class:`ResolutionFailedError`. The engine treats that as a hard
resolution failure: it emits a single MEDIUM Indicator and skips all
distance-dependent analysis (design: "On resolution failure, generate MEDIUM
Indicator and skip distance-dependent analysis"). No default/fabricated
coordinates are ever returned.

An abstract :class:`GeoNamesClient` protocol plus an injectable
:class:`FakeGeoNamesClient` keep the engine testable without live network.

Requirements: 4.1, 4.2, 4.10
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

try:  # httpx is the canonical async HTTP client for the FastAPI stack.
    import httpx  # type: ignore

    _HAS_HTTPX = True
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore
    _HAS_HTTPX = False


# ---------------------------------------------------------------------------
# Result / error types
# ---------------------------------------------------------------------------


#: Sentinel surfaced when a postal code cannot be resolved to coordinates.
RESOLUTION_FAILED: str = "RESOLUTION_FAILED"


@dataclass(frozen=True)
class ResolvedCoordinate:
    """A coordinate resolved by the GeoNames Adapter.

    Attributes:
        postal_code: The postal code / FSA that was resolved.
        latitude: Latitude in degrees.
        longitude: Longitude in degrees.
        source: Where the coordinate came from ('cache', 'api', 'local_dump',
            'resolved', ...), passed through from the adapter for observability.
    """

    postal_code: str
    latitude: float
    longitude: float
    source: str = "resolved"


class ResolutionFailedError(LookupError):
    """Raised when the GeoNames Adapter cannot resolve a postal code.

    The engine surfaces this as a MEDIUM Indicator and skips distance-dependent
    analysis; no default coordinates are substituted.

    Attributes:
        postal_code: The postal code / location that failed to resolve.
        reason: Short machine-readable reason (defaults to ``RESOLUTION_FAILED``).
    """

    def __init__(self, postal_code: str, reason: str = RESOLUTION_FAILED) -> None:
        self.postal_code = postal_code
        self.reason = reason
        super().__init__(f"{reason}: could not resolve '{postal_code}'")


# ---------------------------------------------------------------------------
# Client protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class GeoNamesClient(Protocol):
    """Protocol for resolving a postal code to a coordinate (async)."""

    async def resolve(self, postal_code: str) -> ResolvedCoordinate:
        """Resolve a postal code / FSA to a :class:`ResolvedCoordinate`.

        Raises:
            ResolutionFailedError: If the location cannot be resolved.
        """
        ...


# ---------------------------------------------------------------------------
# HTTP client (production)
# ---------------------------------------------------------------------------


class GeoNamesAdapterClient:
    """Async client calling the GeoNames Adapter's resolve endpoint over HTTP."""

    def __init__(
        self,
        base_url: str,
        *,
        connect_timeout: float = 2.0,
        read_timeout: float = 3.0,
    ) -> None:
        if not _HAS_HTTPX:  # pragma: no cover - environment guard
            raise RuntimeError("httpx is required for GeoNamesAdapterClient")
        self._base_url = base_url.rstrip("/")
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout

    async def resolve(self, postal_code: str) -> ResolvedCoordinate:
        """Resolve ``postal_code`` via ``GET /geonames/resolve/{postal_code}``.

        Any non-200 response (including the adapter's 422 ``RESOLUTION_FAILED``)
        or any transport/timeout/parse error is converted to
        :class:`ResolutionFailedError`. No default coordinates are returned.
        """
        url = f"{self._base_url}/geonames/resolve/{postal_code}"
        timeout = httpx.Timeout(
            connect=self._connect_timeout,
            read=self._read_timeout,
            write=self._read_timeout,
            pool=self._connect_timeout,
        )
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url)
        except Exception:  # noqa: BLE001 - any transport error -> resolution failed
            raise ResolutionFailedError(postal_code)

        if resp.status_code != 200:
            raise ResolutionFailedError(postal_code)

        try:
            data: dict[str, Any] = resp.json()
            lat = float(data["latitude"])
            lon = float(data["longitude"])
            source = str(data.get("source", "resolved"))
        except Exception:  # noqa: BLE001 - malformed body -> resolution failed
            raise ResolutionFailedError(postal_code)

        return ResolvedCoordinate(
            postal_code=postal_code,
            latitude=lat,
            longitude=lon,
            source=source,
        )


# ---------------------------------------------------------------------------
# Fake client (tests / dev)
# ---------------------------------------------------------------------------


class FakeGeoNamesClient:
    """In-memory :class:`GeoNamesClient` for tests — no network.

    Register known postal codes with :meth:`add` (or via the constructor). Any
    postal code not registered raises :class:`ResolutionFailedError`, exercising
    the engine's resolution-failure path.
    """

    def __init__(
        self, coords: Optional[dict[str, tuple[float, float]]] = None
    ) -> None:
        self._coords: dict[str, tuple[float, float]] = {}
        if coords:
            for pc, (lat, lon) in coords.items():
                self.add(pc, lat, lon)
        self.calls: list[str] = []

    @staticmethod
    def _norm(postal_code: str) -> str:
        return (postal_code or "").replace(" ", "").upper()

    def add(self, postal_code: str, latitude: float, longitude: float) -> None:
        """Register a postal code -> (lat, lon) mapping for resolution."""
        self._coords[self._norm(postal_code)] = (latitude, longitude)

    async def resolve(self, postal_code: str) -> ResolvedCoordinate:
        self.calls.append(postal_code)
        entry = self._coords.get(self._norm(postal_code))
        if entry is None:
            raise ResolutionFailedError(postal_code)
        lat, lon = entry
        return ResolvedCoordinate(
            postal_code=postal_code,
            latitude=lat,
            longitude=lon,
            source="fake",
        )


__all__ = [
    "RESOLUTION_FAILED",
    "ResolvedCoordinate",
    "ResolutionFailedError",
    "GeoNamesClient",
    "GeoNamesAdapterClient",
    "FakeGeoNamesClient",
]
