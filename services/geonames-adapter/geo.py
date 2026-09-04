"""
services/geonames-adapter/geo.py
================================
Core geospatial primitives for the GeoNames Adapter:

  - ``haversine(lat1, lon1, lat2, lon2)`` — great-circle distance in kilometres,
    rounded to 3 decimal places (per the GeoNames Adapter design; the Geographic
    Anomaly Engine uses the same algebraic formula but rounds to 4 dp).
  - Coordinate validation helpers that raise ``InvalidCoordinateError`` for any
    latitude outside [-90.0, 90.0] or longitude outside [-180.0, 180.0]. The
    adapter surfaces this as an ``INVALID_COORDINATE`` result and never computes
    a distance for out-of-bounds input (design: "Invalid input ->
    INVALID_COORDINATE; no default coordinates returned").
  - ``ResolutionFailedError`` / ``RESOLUTION_FAILED`` sentinel handling for
    postal-code resolution failures. Resolution NEVER returns default (0, 0) or
    any fabricated coordinates.

The Haversine formula is algebraically symmetric, so
``haversine(A, B) == haversine(B, A)`` by construction (Property 11).

Design references: design.md "GeoNames Adapter", "Geographic Anomaly Engine"
Requirements: 14.1, 14.2, 14.3, 14.7, 14.8
"""

from __future__ import annotations

from dataclasses import dataclass
from math import atan2, cos, radians, sin, sqrt

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Mean earth radius in kilometres (matches the Geographic Anomaly Engine).
EARTH_RADIUS_KM: float = 6371.0

#: Haversine result precision (decimal places) for the GeoNames Adapter.
DISTANCE_PRECISION_DP: int = 3

#: Valid coordinate bounds.
LAT_MIN: float = -90.0
LAT_MAX: float = 90.0
LON_MIN: float = -180.0
LON_MAX: float = 180.0

#: Sentinel string surfaced by the API when a postal code cannot be resolved.
RESOLUTION_FAILED: str = "RESOLUTION_FAILED"
#: Sentinel string surfaced by the API for out-of-bounds coordinates.
INVALID_COORDINATE: str = "INVALID_COORDINATE"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InvalidCoordinateError(ValueError):
    """Raised when a latitude/longitude pair falls outside valid bounds.

    Attributes:
        lat: The offending latitude.
        lon: The offending longitude.
    """

    code: str = INVALID_COORDINATE

    def __init__(self, lat: float, lon: float) -> None:
        self.lat = lat
        self.lon = lon
        super().__init__(
            f"{INVALID_COORDINATE}: lat={lat} (allowed [{LAT_MIN},{LAT_MAX}]), "
            f"lon={lon} (allowed [{LON_MIN},{LON_MAX}])"
        )


class ResolutionFailedError(LookupError):
    """Raised when a postal code cannot be resolved to coordinates.

    The adapter never substitutes default coordinates; callers must treat this
    as a hard resolution failure.

    Attributes:
        postal_code: The postal code / FSA that failed to resolve.
    """

    code: str = RESOLUTION_FAILED

    def __init__(self, postal_code: str) -> None:
        self.postal_code = postal_code
        super().__init__(f"{RESOLUTION_FAILED}: could not resolve '{postal_code}'")


# ---------------------------------------------------------------------------
# Coordinate model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Coordinate:
    """A validated geographic coordinate.

    Constructing a ``Coordinate`` validates its bounds, so any ``Coordinate``
    instance is guaranteed to be in range.
    """

    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        validate_coordinate(self.latitude, self.longitude)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def is_valid_coordinate(lat: float, lon: float) -> bool:
    """Return ``True`` iff ``lat``/``lon`` fall within valid bounds.

    ``NaN`` and infinities are rejected because the comparison chain below is
    ``False`` for ``NaN`` and any infinity lies outside the closed interval.

    Args:
        lat: Latitude in degrees.
        lon: Longitude in degrees.

    Returns:
        ``True`` if ``LAT_MIN <= lat <= LAT_MAX`` and
        ``LON_MIN <= lon <= LON_MAX``; ``False`` otherwise.
    """
    return (LAT_MIN <= lat <= LAT_MAX) and (LON_MIN <= lon <= LON_MAX)


def validate_coordinate(lat: float, lon: float) -> None:
    """Validate a coordinate, raising ``InvalidCoordinateError`` if out of bounds.

    Args:
        lat: Latitude in degrees.
        lon: Longitude in degrees.

    Raises:
        InvalidCoordinateError: If the coordinate is out of bounds (or NaN/inf).
    """
    if not is_valid_coordinate(lat, lon):
        raise InvalidCoordinateError(lat=lat, lon=lon)


# ---------------------------------------------------------------------------
# Haversine distance
# ---------------------------------------------------------------------------


def haversine(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
    *,
    precision: int = DISTANCE_PRECISION_DP,
) -> float:
    """Great-circle distance between two points in kilometres.

    Uses the standard Haversine formula with a mean earth radius of
    ``EARTH_RADIUS_KM`` (6371.0 km). The result is rounded to ``precision``
    decimal places (default 3 for the GeoNames Adapter).

    Both coordinates are validated first: an out-of-bounds coordinate raises
    ``InvalidCoordinateError`` and NO distance is computed.

    The formula is algebraically symmetric, so
    ``haversine(A, B) == haversine(B, A)`` and ``haversine(A, A) == 0.0``.

    Args:
        lat1: Latitude of the first point (degrees).
        lon1: Longitude of the first point (degrees).
        lat2: Latitude of the second point (degrees).
        lon2: Longitude of the second point (degrees).
        precision: Decimal places to round to.

    Returns:
        The distance in kilometres, rounded to ``precision`` decimal places.

    Raises:
        InvalidCoordinateError: If either coordinate is out of bounds.
    """
    validate_coordinate(lat1, lon1)
    validate_coordinate(lat2, lon2)

    phi1, phi2 = radians(lat1), radians(lat2)
    d_phi = radians(lat2 - lat1)
    d_lambda = radians(lon2 - lon1)

    a = sin(d_phi / 2.0) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2.0) ** 2
    # Clamp ``a`` into [0, 1] to guard against floating point drift that could
    # otherwise push sqrt(1 - a) slightly negative for antipodal points.
    a = min(1.0, max(0.0, a))
    central_angle = 2.0 * atan2(sqrt(a), sqrt(1.0 - a))

    distance_km = EARTH_RADIUS_KM * central_angle
    rounded = round(distance_km, precision)
    # Normalise a possible negative zero to positive zero for clean self-distance.
    return rounded + 0.0


def haversine_coords(a: Coordinate, b: Coordinate, *, precision: int = DISTANCE_PRECISION_DP) -> float:
    """Convenience wrapper computing the distance between two ``Coordinate``s."""
    return haversine(
        a.latitude, a.longitude, b.latitude, b.longitude, precision=precision
    )


__all__ = [
    "EARTH_RADIUS_KM",
    "DISTANCE_PRECISION_DP",
    "LAT_MIN",
    "LAT_MAX",
    "LON_MIN",
    "LON_MAX",
    "RESOLUTION_FAILED",
    "INVALID_COORDINATE",
    "InvalidCoordinateError",
    "ResolutionFailedError",
    "Coordinate",
    "is_valid_coordinate",
    "validate_coordinate",
    "haversine",
    "haversine_coords",
]
