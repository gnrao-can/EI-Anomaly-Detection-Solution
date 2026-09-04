"""
services/geographic-anomaly-engine/geo.py
==========================================
Core geospatial primitive for the Geographic Anomaly Engine.

This is the engine's OWN copy of the Haversine great-circle distance function.
It is algebraically identical to the GeoNames Adapter's implementation but
rounds the result to **4 decimal places** (per the design's
"Geographic Anomaly Engine" pseudo-code), whereas the GeoNames Adapter rounds to
3 dp. Keeping a local copy avoids a cross-service import and lets each service
own its documented precision.

Design reference (design.md, "2. Geographic Anomaly Engine"):

    def haversine(lat1, lon1, lat2, lon2) -> float:
        R = 6371.0  # Earth radius in km
        ...
        return round(R * 2 * atan2(sqrt(a), sqrt(1-a)), 4)

The formula is algebraically symmetric, so ``haversine(A, B) == haversine(B, A)``
and ``haversine(A, A) == 0.0`` by construction (Property 2).

Requirements: 4.1, 4.10
"""

from __future__ import annotations

from math import atan2, cos, radians, sin, sqrt

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Mean earth radius in kilometres (matches the GeoNames Adapter / design).
EARTH_RADIUS_KM: float = 6371.0

#: Haversine result precision (decimal places) for the Geographic Anomaly
#: Engine. NOTE: distinct from the GeoNames Adapter, which uses 3 dp.
DISTANCE_PRECISION_DP: int = 4

#: Valid coordinate bounds.
LAT_MIN: float = -90.0
LAT_MAX: float = 90.0
LON_MIN: float = -180.0
LON_MAX: float = 180.0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InvalidCoordinateError(ValueError):
    """Raised when a latitude/longitude pair falls outside valid bounds.

    Attributes:
        lat: The offending latitude.
        lon: The offending longitude.
    """

    def __init__(self, lat: float, lon: float) -> None:
        self.lat = lat
        self.lon = lon
        super().__init__(
            f"INVALID_COORDINATE: lat={lat} (allowed [{LAT_MIN},{LAT_MAX}]), "
            f"lon={lon} (allowed [{LON_MIN},{LON_MAX}])"
        )


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def is_valid_coordinate(lat: float, lon: float) -> bool:
    """Return ``True`` iff ``lat``/``lon`` fall within valid bounds.

    ``NaN`` and infinities are rejected because the comparison chain is
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
    decimal places (default 4 for the Geographic Anomaly Engine).

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


__all__ = [
    "EARTH_RADIUS_KM",
    "DISTANCE_PRECISION_DP",
    "LAT_MIN",
    "LAT_MAX",
    "LON_MIN",
    "LON_MAX",
    "InvalidCoordinateError",
    "is_valid_coordinate",
    "validate_coordinate",
    "haversine",
]
