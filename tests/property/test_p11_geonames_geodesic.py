"""
tests/property/test_p11_geonames_geodesic.py
============================================
Property 11: GeoNames adapter geodesic invariants. Validates: Requirements 14.7, 14.8

Statement
---------
*For all* coordinate pairs handled by the GeoNames_Adapter's Haversine
implementation (``services/geonames-adapter/geo.py``):

  * **Symmetry (Req 14.7):** ``|haversine(A, B) - haversine(B, A)| <= 0.001`` km.
    The Haversine formula is algebraically symmetric, so the two values are in
    fact bit-for-bit equal; we assert within the 0.001 km tolerance mandated by
    the requirement.
  * **Zero self-distance (Req 14.8):** ``haversine(A, A) == 0.0`` km for every
    valid coordinate ``A``.
  * **Non-negativity:** ``haversine(A, B) >= 0`` for every valid pair.
  * **Bounded:** a great-circle distance can never exceed half the earth's
    circumference (~20 015 km), so ``haversine(A, B) <= 20016`` km.
  * **Out-of-bounds (Req 14.6 support):** for any coordinate with
    ``lat ∉ [-90, 90]`` or ``lon ∉ [-180, 180]`` the adapter SHALL raise
    ``InvalidCoordinateError`` (``code == "INVALID_COORDINATE"``) and SHALL NOT
    produce a distance; ``validate_coordinate`` raises and
    ``is_valid_coordinate`` returns ``False``.

The ``geonames-adapter`` directory contains a hyphen, so it is not importable as
a normal package. We load ``geo.py`` from it via the shared, collision-free
``tests/conftest.load_service_module`` helper (preferred pattern).

Property 11: GeoNames adapter geodesic invariants. Validates: Requirements 14.7, 14.8
"""

from __future__ import annotations

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from tests.conftest import load_service_module

# --- Load geo.py from the hyphenated geonames-adapter service directory ------
# ``geo`` imports nothing by bare sibling name, so ``siblings=["geo"]`` is enough.
geo = load_service_module("geonames-adapter", "geo", siblings=["geo"])

haversine = geo.haversine
is_valid_coordinate = geo.is_valid_coordinate
validate_coordinate = geo.validate_coordinate
InvalidCoordinateError = geo.InvalidCoordinateError
INVALID_COORDINATE = geo.INVALID_COORDINATE
LAT_MIN, LAT_MAX = geo.LAT_MIN, geo.LAT_MAX
LON_MIN, LON_MAX = geo.LON_MIN, geo.LON_MAX


# ---------------------------------------------------------------------------
# Hypothesis profile: CI runs 200 examples (design.md — Testing Strategy;
# tasks.md Notes: settings.register_profile("ci", max_examples=200, ...)).
#
# Registration is guarded so a duplicate registration (from a conftest or a
# sibling property-test module already loaded in the same session) does not
# raise. We always (re)load the "ci" profile so this file runs 200 examples.
# ---------------------------------------------------------------------------
try:
    settings.register_profile(
        "ci", max_examples=200, suppress_health_check=[HealthCheck.too_slow]
    )
except Exception:  # noqa: BLE001 - already registered elsewhere
    pass
settings.load_profile("ci")


# The symmetry tolerance mandated by Requirement 14.7.
SYMMETRY_TOLERANCE_KM = 0.001
# Half the earth's circumference (2 * pi * R / 2) with a small margin. A
# great-circle distance can never exceed this.
MAX_EARTH_DISTANCE_KM = 20016.0


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Valid coordinate components — strictly within the closed bounds.
valid_lat = st.floats(
    min_value=LAT_MIN, max_value=LAT_MAX, allow_nan=False, allow_infinity=False
)
valid_lon = st.floats(
    min_value=LON_MIN, max_value=LON_MAX, allow_nan=False, allow_infinity=False
)


@st.composite
def out_of_bounds_coordinate(draw):
    """Draw a ``(lat, lon)`` pair that is guaranteed out of the valid bounds.

    At least one component is pushed beyond its bound by a positive offset, so
    ``is_valid_coordinate`` is always ``False``. The offending axis (lat, lon, or
    both) and the direction (above/below) are all randomised.
    """
    # Which axes are out of bounds: "lat", "lon", or "both".
    which = draw(st.sampled_from(["lat", "lon", "both"]))
    # A strictly-positive offset that pushes a value clear of the bound.
    offset = draw(
        st.floats(
            min_value=1e-6, max_value=1e6, allow_nan=False, allow_infinity=False
        )
    )
    above = draw(st.booleans())

    lat = draw(valid_lat)
    lon = draw(valid_lon)

    def bust_lat(v: float) -> float:
        return (LAT_MAX + offset) if above else (LAT_MIN - offset)

    def bust_lon(v: float) -> float:
        return (LON_MAX + offset) if above else (LON_MIN - offset)

    if which in ("lat", "both"):
        lat = bust_lat(lat)
    if which in ("lon", "both"):
        lon = bust_lon(lon)

    # Defensive: ensure the result really is out of bounds (offsets are > 0 so
    # this holds, but assume() guards against any float edge weirdness).
    assume(not is_valid_coordinate(lat, lon))
    return (lat, lon)


# ---------------------------------------------------------------------------
# Property 11 — symmetry (Req 14.7)
# ---------------------------------------------------------------------------


@given(lat1=valid_lat, lon1=valid_lon, lat2=valid_lat, lon2=valid_lon)
def test_P11_symmetry(lat1, lon1, lat2, lon2):
    """Property 11: GeoNames adapter geodesic invariants. Validates: Requirements 14.7

    ``|haversine(A, B) - haversine(B, A)| <= 0.001`` km for all valid pairs.
    """
    d_ab = haversine(lat1, lon1, lat2, lon2)
    d_ba = haversine(lat2, lon2, lat1, lon1)
    assert abs(d_ab - d_ba) <= SYMMETRY_TOLERANCE_KM, (
        f"symmetry violated: haversine(A,B)={d_ab} vs haversine(B,A)={d_ba} "
        f"differ by {abs(d_ab - d_ba)} > {SYMMETRY_TOLERANCE_KM} km"
    )


# ---------------------------------------------------------------------------
# Property 11 — zero self-distance (Req 14.8)
# ---------------------------------------------------------------------------


@given(lat=valid_lat, lon=valid_lon)
def test_P11_self_distance_is_zero(lat, lon):
    """Property 11: GeoNames adapter geodesic invariants. Validates: Requirements 14.8

    ``haversine(A, A) == 0.0`` km for every valid coordinate ``A``.
    """
    d_aa = haversine(lat, lon, lat, lon)
    assert d_aa == 0.0, f"self-distance for ({lat},{lon}) was {d_aa}, expected 0.0"


# ---------------------------------------------------------------------------
# Property 11 — non-negative and bounded (supporting invariants)
# ---------------------------------------------------------------------------


@given(lat1=valid_lat, lon1=valid_lon, lat2=valid_lat, lon2=valid_lon)
def test_P11_non_negative_and_bounded(lat1, lon1, lat2, lon2):
    """Property 11: GeoNames adapter geodesic invariants. Validates: Requirements 14.7, 14.8

    A great-circle distance is always ``>= 0`` and never exceeds half the
    earth's circumference (~20 015 km).
    """
    d = haversine(lat1, lon1, lat2, lon2)
    assert d >= 0.0, f"distance was negative: {d}"
    assert d <= MAX_EARTH_DISTANCE_KM, (
        f"distance {d} exceeds max earth distance {MAX_EARTH_DISTANCE_KM} km"
    )


# ---------------------------------------------------------------------------
# Property 11 — out-of-bounds coordinates raise, no distance produced
# (Req 14.6 support: INVALID_COORDINATE for out-of-bounds input)
# ---------------------------------------------------------------------------


@given(bad=out_of_bounds_coordinate(), other_lat=valid_lat, other_lon=valid_lon)
def test_P11_out_of_bounds_raises_and_produces_no_distance(bad, other_lat, other_lon):
    """Property 11: GeoNames adapter geodesic invariants. Validates: Requirements 14.7, 14.8

    For any out-of-bounds coordinate:
      * ``is_valid_coordinate`` returns ``False``;
      * ``validate_coordinate`` raises ``InvalidCoordinateError`` with
        ``code == "INVALID_COORDINATE"``;
      * ``haversine`` raises ``InvalidCoordinateError`` whether the bad
        coordinate is the first or second argument — NO distance is produced.
    """
    bad_lat, bad_lon = bad

    # is_valid_coordinate rejects it.
    assert is_valid_coordinate(bad_lat, bad_lon) is False

    # validate_coordinate raises with the right code.
    try:
        validate_coordinate(bad_lat, bad_lon)
        raise AssertionError("validate_coordinate did not raise for out-of-bounds")
    except InvalidCoordinateError as exc:
        assert exc.code == INVALID_COORDINATE == "INVALID_COORDINATE"

    # haversine raises when the bad coordinate is the FIRST point; no distance.
    try:
        haversine(bad_lat, bad_lon, other_lat, other_lon)
        raise AssertionError("haversine produced a distance for out-of-bounds A")
    except InvalidCoordinateError as exc:
        assert exc.code == INVALID_COORDINATE

    # haversine raises when the bad coordinate is the SECOND point; no distance.
    try:
        haversine(other_lat, other_lon, bad_lat, bad_lon)
        raise AssertionError("haversine produced a distance for out-of-bounds B")
    except InvalidCoordinateError as exc:
        assert exc.code == INVALID_COORDINATE


# ===========================================================================
# Concrete regression cases (enumerated, not property-generated)
# ===========================================================================

# Approximate coordinates.
OTTAWA = (45.4215, -75.6972)
TORONTO = (43.6532, -79.3832)
NORTH_POLE = (90.0, 0.0)
SOUTH_POLE = (-90.0, 0.0)


def test_P11_regression_ottawa_toronto_symmetric_and_plausible():
    """Property 11: GeoNames adapter geodesic invariants. Validates: Requirements 14.7

    Ottawa <-> Toronto is ~350 km and symmetric to within the 0.001 km
    tolerance.
    """
    d_ot = haversine(*OTTAWA, *TORONTO)
    d_to = haversine(*TORONTO, *OTTAWA)
    assert abs(d_ot - d_to) <= SYMMETRY_TOLERANCE_KM
    # Sanity band around the well-known ~350 km great-circle distance.
    assert 300.0 <= d_ot <= 400.0, f"Ottawa-Toronto distance {d_ot} km outside 300-400 km"


def test_P11_regression_self_distances_zero():
    """Property 11: GeoNames adapter geodesic invariants. Validates: Requirements 14.8

    Self-distance is exactly 0.0 for representative points including a pole.
    """
    assert haversine(*OTTAWA, *OTTAWA) == 0.0
    assert haversine(*NORTH_POLE, *NORTH_POLE) == 0.0


def test_P11_regression_pole_to_pole_sanity():
    """Property 11: GeoNames adapter geodesic invariants. Validates: Requirements 14.7, 14.8

    Pole-to-pole is half the earth's circumference (~20 015 km), symmetric, and
    within the bounded maximum.
    """
    d_ns = haversine(*NORTH_POLE, *SOUTH_POLE)
    d_sn = haversine(*SOUTH_POLE, *NORTH_POLE)
    assert abs(d_ns - d_sn) <= SYMMETRY_TOLERANCE_KM
    # ~20 015 km; allow a small band.
    assert 20000.0 <= d_ns <= MAX_EARTH_DISTANCE_KM, (
        f"pole-to-pole distance {d_ns} km outside expected ~20015 km band"
    )


def test_P11_regression_out_of_bounds_examples():
    """Property 11: GeoNames adapter geodesic invariants. Validates: Requirements 14.7, 14.8

    Concrete out-of-bounds coordinates raise INVALID_COORDINATE and yield no
    distance; boundary values (exactly at the limits) are valid.
    """
    # Latitude just past the north pole.
    for bad in [(90.0001, 0.0), (-90.0001, 0.0), (0.0, 180.0001), (0.0, -180.0001)]:
        assert is_valid_coordinate(*bad) is False
        try:
            haversine(*bad, 0.0, 0.0)
            raise AssertionError(f"haversine produced a distance for {bad}")
        except InvalidCoordinateError as exc:
            assert exc.code == INVALID_COORDINATE

    # Exact boundary coordinates are valid and compute a distance.
    assert is_valid_coordinate(90.0, 180.0) is True
    assert is_valid_coordinate(-90.0, -180.0) is True
    d = haversine(90.0, 180.0, -90.0, -180.0)
    assert d >= 0.0
