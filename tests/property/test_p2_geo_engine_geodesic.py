"""
tests/property/test_p2_geo_engine_geodesic.py
=============================================
Property 2: Geodesic distance symmetry and self-distance. Validates: Requirements 4.10

Statement
---------
*For any* pair of valid coordinate points A and B within the bounds
latitude ∈ [-90, 90] and longitude ∈ [-180, 180], the Geographic_Anomaly_Engine's
distance function (``services/geographic-anomaly-engine/geo.py::haversine``) SHALL
return:

  * **Symmetry:** ``distance(A, B) == distance(B, A)`` within a tolerance of
    0.001 km. The Haversine formula is algebraically symmetric, so the two
    values are in fact bit-for-bit equal; we assert within the mandated
    0.001 km tolerance.
  * **Zero self-distance:** ``distance(A, A) == 0.0`` km for every valid
    coordinate ``A``.

The ``geographic-anomaly-engine`` directory contains a hyphen, so it is not
importable as a normal package. We load ``geo.py`` from it via the shared,
collision-free ``tests/conftest.load_service_module`` helper (preferred pattern)
rather than ad-hoc importlib juggling.

This is a distinct test file from the concurrent unit-test task on the same
service, to avoid write conflicts.

Property 2: Geodesic distance symmetry and self-distance. Validates: Requirements 4.10
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.conftest import load_service_module

# --- Load geo.py from the hyphenated geographic-anomaly-engine directory -----
# ``geo`` imports nothing by bare sibling name, so ``siblings=["geo"]`` suffices.
geo = load_service_module("geographic-anomaly-engine", "geo", siblings=["geo"])

haversine = geo.haversine
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


# The symmetry tolerance mandated by Requirement 4.10.
SYMMETRY_TOLERANCE_KM = 0.001


# ---------------------------------------------------------------------------
# Strategies — valid coordinate components, strictly within the closed bounds.
# ---------------------------------------------------------------------------

valid_lat = st.floats(
    min_value=LAT_MIN, max_value=LAT_MAX, allow_nan=False, allow_infinity=False
)
valid_lon = st.floats(
    min_value=LON_MIN, max_value=LON_MAX, allow_nan=False, allow_infinity=False
)


# ---------------------------------------------------------------------------
# Property 2 — symmetry (Req 4.10)
# ---------------------------------------------------------------------------


@given(lat1=valid_lat, lon1=valid_lon, lat2=valid_lat, lon2=valid_lon)
def test_P2_distance_symmetry(lat1, lon1, lat2, lon2):
    """Property 2: Geodesic distance symmetry and self-distance. Validates: Requirements 4.10

    ``|distance(A, B) - distance(B, A)| <= 0.001`` km for all valid pairs.
    """
    d_ab = haversine(lat1, lon1, lat2, lon2)
    d_ba = haversine(lat2, lon2, lat1, lon1)
    assert abs(d_ab - d_ba) <= SYMMETRY_TOLERANCE_KM, (
        f"symmetry violated: distance(A,B)={d_ab} vs distance(B,A)={d_ba} "
        f"differ by {abs(d_ab - d_ba)} > {SYMMETRY_TOLERANCE_KM} km"
    )


# ---------------------------------------------------------------------------
# Property 2 — zero self-distance (Req 4.10)
# ---------------------------------------------------------------------------


@given(lat=valid_lat, lon=valid_lon)
def test_P2_self_distance_is_zero(lat, lon):
    """Property 2: Geodesic distance symmetry and self-distance. Validates: Requirements 4.10

    ``distance(A, A) == 0.0`` km for every valid coordinate ``A``.
    """
    d_aa = haversine(lat, lon, lat, lon)
    assert d_aa == 0.0, f"self-distance for ({lat},{lon}) was {d_aa}, expected 0.0"


# ===========================================================================
# Concrete regression cases (enumerated, not property-generated)
# ===========================================================================

# Approximate coordinates.
OTTAWA = (45.4215, -75.6972)
TORONTO = (43.6532, -79.3832)
NORTH_POLE = (90.0, 0.0)
SOUTH_POLE = (-90.0, 0.0)


def test_P2_regression_symmetric_pairs():
    """Property 2: Geodesic distance symmetry and self-distance. Validates: Requirements 4.10

    Representative pairs are symmetric to within the 0.001 km tolerance.
    """
    for a, b in [(OTTAWA, TORONTO), (NORTH_POLE, SOUTH_POLE), (OTTAWA, NORTH_POLE)]:
        d_ab = haversine(*a, *b)
        d_ba = haversine(*b, *a)
        assert abs(d_ab - d_ba) <= SYMMETRY_TOLERANCE_KM


def test_P2_regression_self_distances_zero():
    """Property 2: Geodesic distance symmetry and self-distance. Validates: Requirements 4.10

    Self-distance is exactly 0.0 for representative points including a pole.
    """
    assert haversine(*OTTAWA, *OTTAWA) == 0.0
    assert haversine(*TORONTO, *TORONTO) == 0.0
    assert haversine(*NORTH_POLE, *NORTH_POLE) == 0.0
