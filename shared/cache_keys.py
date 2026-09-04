"""
shared/cache_keys.py
====================
Canonical Redis cache key patterns, TTLs, and key-builder functions for the
EI Anomaly Detection Solution.

Every external-data adapter and the pipeline orchestrator MUST build cache keys
through the builder functions defined here rather than formatting strings
inline. This keeps the key namespace and TTLs in one authoritative place and
guarantees that the cache configuration matches the design's caching strategy.

Key patterns and TTLs are taken directly from the design's "Redis Caching
Strategy" table and the individual adapter specifications:

    | Pattern                                   | TTL      | Owning service            |
    |-------------------------------------------|----------|---------------------------|
    | statscan:lfs:{region_code}:{year_month}   | 30 days  | Stats Canada Adapter      |
    | cra:t4:{sin_hash}:{bn}:{year}             | 24 hours | CRA Adapter               |
    | geonames:coord:{postal_code}              | 90 days  | GeoNames Adapter          |
    | geonames:dist:{hash(A,B)}                 | 7 days   | GeoNames Adapter          |
    | biz_reg:employer:{bn}                     | 24 hours | Business Registry Adapter |
    | mvr:ownership:{sin_hash}:{province}       | 1 hour   | MVR Adapter               |
    | job_bank:postings:{noc}:{region}:{ym}     | 7 days   | Job Bank Adapter          |
    | cbsa:travel:{sin_hash}:{period_hash}      | 1 hour   | CBSA Adapter              |
    | orchestrator:pairing:{claim_id}           | 60 sec   | Pipeline Orchestrator     |

Requirements: 17.4, 23.5
"""

from __future__ import annotations

import hashlib

# ---------------------------------------------------------------------------
# TTL constants (seconds)
# ---------------------------------------------------------------------------

_SECONDS_PER_HOUR: int = 3600
_SECONDS_PER_DAY: int = 24 * _SECONDS_PER_HOUR

#: LFS unemployment rates — refreshed monthly.
STATSCAN_LFS_TTL_SECONDS: int = 30 * _SECONDS_PER_DAY
#: CRA T4 records — held only for a claim lifecycle.
CRA_T4_TTL_SECONDS: int = 24 * _SECONDS_PER_HOUR
#: Postal code -> coordinate resolution — very stable, long TTL.
GEONAMES_COORD_TTL_SECONDS: int = 90 * _SECONDS_PER_DAY
#: Computed Haversine distances between two points.
GEONAMES_DIST_TTL_SECONDS: int = 7 * _SECONDS_PER_DAY
#: Employer registration records.
BIZ_REG_EMPLOYER_TTL_SECONDS: int = 24 * _SECONDS_PER_HOUR
#: Vehicle ownership status.
MVR_OWNERSHIP_TTL_SECONDS: int = 1 * _SECONDS_PER_HOUR
#: Job Bank posting counts / wages.
JOB_BANK_POSTINGS_TTL_SECONDS: int = 7 * _SECONDS_PER_DAY
#: CBSA travel records.
CBSA_TRAVEL_TTL_SECONDS: int = 1 * _SECONDS_PER_HOUR
#: Orchestrator event-pairing store (claim.received <-> roe.received).
#: The design specifies a 60-second correlation timeout.
ORCHESTRATOR_PAIRING_TTL_SECONDS: int = 60


# ---------------------------------------------------------------------------
# Key template constants
# ---------------------------------------------------------------------------

STATSCAN_LFS: str = "statscan:lfs:{region_code}:{year_month}"
CRA_T4: str = "cra:t4:{sin_hash}:{bn}:{year}"
GEONAMES_COORD: str = "geonames:coord:{postal_code}"
GEONAMES_DIST: str = "geonames:dist:{pair_hash}"
BIZ_REG_EMPLOYER: str = "biz_reg:employer:{bn}"
MVR_OWNERSHIP: str = "mvr:ownership:{sin_hash}:{province}"
JOB_BANK_POSTINGS: str = "job_bank:postings:{noc}:{region}:{year_month}"
CBSA_TRAVEL: str = "cbsa:travel:{sin_hash}:{period_hash}"
ORCHESTRATOR_PAIRING: str = "orchestrator:pairing:{claim_id}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _coordinate_pair_hash(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> str:
    """Return a stable, order-independent hash for a pair of coordinates.

    The Haversine distance between A and B is symmetric (distance(A, B) ==
    distance(B, A)), so the cache key must be identical regardless of argument
    order. We canonicalise by sorting the two ``(lat, lon)`` tuples before
    hashing.

    Args:
        lat1: Latitude of the first point.
        lon1: Longitude of the first point.
        lat2: Latitude of the second point.
        lon2: Longitude of the second point.

    Returns:
        A 16-character hex digest uniquely identifying the unordered pair.
    """
    point_a = (float(lat1), float(lon1))
    point_b = (float(lat2), float(lon2))
    low, high = sorted((point_a, point_b))
    raw = f"{low[0]:.6f},{low[1]:.6f}|{high[0]:.6f},{high[1]:.6f}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Key builder functions
# ---------------------------------------------------------------------------


def statscan_lfs_key(region_code: str, year_month: str) -> str:
    """Build the cache key for Stats Canada LFS data.

    Args:
        region_code: ESDC 2-char economic region code.
        year_month: Reference month as ``YYYY-MM``.

    Returns:
        The formatted cache key.
    """
    return STATSCAN_LFS.format(region_code=region_code, year_month=year_month)


def cra_t4_key(sin_hash: str, bn: str, year: str | int) -> str:
    """Build the cache key for a CRA T4 record.

    Args:
        sin_hash: SHA-256 hash of the claimant SIN.
        bn: 15-char employer Business Number.
        year: Tax year.

    Returns:
        The formatted cache key.
    """
    return CRA_T4.format(sin_hash=sin_hash, bn=bn, year=year)


def geonames_coord_key(postal_code: str) -> str:
    """Build the cache key for a postal-code -> coordinate resolution.

    Args:
        postal_code: Canadian postal code (full or FSA).

    Returns:
        The formatted cache key.
    """
    return GEONAMES_COORD.format(postal_code=postal_code)


def geonames_dist_key(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> str:
    """Build the cache key for a computed Haversine distance.

    The key is symmetric: swapping (A, B) for (B, A) yields the same key.

    Args:
        lat1: Latitude of the first point.
        lon1: Longitude of the first point.
        lat2: Latitude of the second point.
        lon2: Longitude of the second point.

    Returns:
        The formatted cache key.
    """
    pair_hash = _coordinate_pair_hash(lat1, lon1, lat2, lon2)
    return GEONAMES_DIST.format(pair_hash=pair_hash)


def biz_reg_employer_key(bn: str) -> str:
    """Build the cache key for an employer registration record.

    Args:
        bn: 15-char employer Business Number.

    Returns:
        The formatted cache key.
    """
    return BIZ_REG_EMPLOYER.format(bn=bn)


def mvr_ownership_key(sin_hash: str, province: str) -> str:
    """Build the cache key for a vehicle-ownership lookup.

    Args:
        sin_hash: SHA-256 hash of the claimant SIN.
        province: Two-letter province code.

    Returns:
        The formatted cache key.
    """
    return MVR_OWNERSHIP.format(sin_hash=sin_hash, province=province)


def job_bank_postings_key(noc: str, region: str, year_month: str) -> str:
    """Build the cache key for Job Bank posting data.

    Args:
        noc: NOC 2021 code.
        region: ESDC economic region code.
        year_month: Reference month as ``YYYY-MM``.

    Returns:
        The formatted cache key.
    """
    return JOB_BANK_POSTINGS.format(noc=noc, region=region, year_month=year_month)


def cbsa_travel_key(sin_hash: str, period_hash: str) -> str:
    """Build the cache key for a CBSA travel-record lookup.

    Args:
        sin_hash: SHA-256 hash of the claimant SIN.
        period_hash: Hash of the employment-period bounds.

    Returns:
        The formatted cache key.
    """
    return CBSA_TRAVEL.format(sin_hash=sin_hash, period_hash=period_hash)


def orchestrator_pairing_key(claim_id: str) -> str:
    """Build the cache key for the orchestrator's event-pairing store.

    The pipeline orchestrator waits for both ``claim.received`` and
    ``roe.received`` events for a claim; this key holds the first-arriving
    event until its pair arrives or the 60-second correlation timeout expires.

    Args:
        claim_id: The claim UUID (as a string).

    Returns:
        The formatted cache key.
    """
    return ORCHESTRATOR_PAIRING.format(claim_id=claim_id)


__all__ = [
    # TTLs
    "STATSCAN_LFS_TTL_SECONDS",
    "CRA_T4_TTL_SECONDS",
    "GEONAMES_COORD_TTL_SECONDS",
    "GEONAMES_DIST_TTL_SECONDS",
    "BIZ_REG_EMPLOYER_TTL_SECONDS",
    "MVR_OWNERSHIP_TTL_SECONDS",
    "JOB_BANK_POSTINGS_TTL_SECONDS",
    "CBSA_TRAVEL_TTL_SECONDS",
    "ORCHESTRATOR_PAIRING_TTL_SECONDS",
    # Templates
    "STATSCAN_LFS",
    "CRA_T4",
    "GEONAMES_COORD",
    "GEONAMES_DIST",
    "BIZ_REG_EMPLOYER",
    "MVR_OWNERSHIP",
    "JOB_BANK_POSTINGS",
    "CBSA_TRAVEL",
    "ORCHESTRATOR_PAIRING",
    # Builders
    "statscan_lfs_key",
    "cra_t4_key",
    "geonames_coord_key",
    "geonames_dist_key",
    "biz_reg_employer_key",
    "mvr_ownership_key",
    "job_bank_postings_key",
    "cbsa_travel_key",
    "orchestrator_pairing_key",
]
