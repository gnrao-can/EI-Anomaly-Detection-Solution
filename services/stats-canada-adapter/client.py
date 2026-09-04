"""
services/stats-canada-adapter/client.py
=======================================
HTTP fetch, canonical parsing, and Redis caching for the Stats Canada Adapter.

This module implements the data-plane of the adapter (task 18.1):

  - :func:`parse_lfs_response` — converts a raw Statistics Canada LFS JSON
    payload into ``list[LabourForceData]``, retaining every rate value to at
    least 4 decimal places via ``Decimal(str(value))`` (Requirement 10.6).
  - :func:`serialise` — the inverse of :func:`parse_lfs_response`; emits a
    canonical dict payload that round-trips back through the parser without loss
    (used by the P10 round-trip property test in task 18.2).
  - :class:`StatsCanadaFetcher` — an ``httpx``-based fetcher configured with a
    10 s connect / 30 s read timeout (Requirement 10.1) that can also serve from
    local ``mock-data/stats-canada/`` fixtures in dev/test (Requirement 10.5).
  - :class:`StatsCanadaCache` — a thin wrapper around the shared ``RedisCache``
    (or any duck-typed async cache) that stores the canonical payload under the
    ``statscan:lfs:{region_code}:{year_month}`` key with a 30-day TTL, and
    reports whether a served entry is older than 30 days (Requirements 10.2,
    10.3, 10.4).

Failure / fallback strategy (implemented in ``main.py`` on top of these
primitives): upstream ``5xx`` → Redis cache with a ``DATA_STALE`` flag; if the
cache is empty or older than 30 days → ``DATA_UNAVAILABLE`` (Requirements 10.2,
10.4).

Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional, Protocol

from shared.cache_keys import STATSCAN_LFS_TTL_SECONDS, statscan_lfs_key
from shared.models import LabourForceData

try:  # httpx is used for the live fetch path only
    import httpx

    _HAS_HTTPX = True
except ImportError:  # pragma: no cover - httpx absent in minimal environments
    httpx = None  # type: ignore[assignment]
    _HAS_HTTPX = False


#: Number of days after which a cached LFS entry is considered stale.
STALE_AFTER_DAYS = 30


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UpstreamServerError(Exception):
    """Raised when the LFS API responds with a 5xx status code."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"Stats Canada LFS API returned {status_code}")


class UpstreamClientError(Exception):
    """Raised when the LFS API responds with a 4xx status code."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"Stats Canada LFS API returned {status_code}")


class LFSParseError(Exception):
    """Raised when the LFS payload cannot be parsed into LabourForceData."""


# ---------------------------------------------------------------------------
# Cache protocol (duck-typed to shared.cache.RedisCache)
# ---------------------------------------------------------------------------


class AsyncCache(Protocol):
    """Minimal async cache interface used by :class:`StatsCanadaCache`.

    ``shared.cache.RedisCache`` satisfies this protocol; unit tests provide an
    in-memory implementation so no Redis server (or ``redis`` package) is
    required.
    """

    async def get(self, key: str) -> str | None: ...

    async def set_with_ttl(self, key: str, value: str, ttl_seconds: int) -> None: ...


# ---------------------------------------------------------------------------
# Parsing / serialisation
# ---------------------------------------------------------------------------


def _to_decimal(value: Any) -> Decimal:
    """Convert a raw value to ``Decimal`` via the string path (Requirement 10.6).

    Using ``Decimal(str(value))`` preserves the exact decimal digits present in
    the source (e.g. ``6.1234`` stays ``6.1234``) instead of introducing binary
    floating-point error.
    """
    if value is None:
        raise LFSParseError("rate value is missing")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _pick(record: dict[str, Any], *names: str) -> Any:
    """Return the first present, non-``None`` value among ``names``.

    Statistics Canada payloads and our canonical fixtures use slightly different
    field spellings; this lets the parser accept either.
    """
    for name in names:
        if name in record and record[name] is not None:
            return record[name]
    return None


def _parse_reference_month(value: Any) -> date:
    """Parse a reference month that may be ``YYYY-MM`` or ``YYYY-MM-DD``."""
    if isinstance(value, date):
        return value
    if value is None:
        raise LFSParseError("reference_month is missing")
    text = str(value)
    if len(text) == 7:  # YYYY-MM
        text = f"{text}-01"
    try:
        return date.fromisoformat(text)
    except ValueError as exc:  # pragma: no cover - defensive
        raise LFSParseError(f"invalid reference_month {value!r}") from exc


def parse_lfs_response(
    raw: dict[str, Any],
    *,
    api_version: str | None = None,
    retrieved_at: datetime | None = None,
    data_stale: bool = False,
) -> list[LabourForceData]:
    """Parse a raw LFS payload into a list of :class:`LabourForceData`.

    Args:
        raw: The decoded LFS JSON payload. Expected shape::

            {
              "source_api_version": "98100424",
              "records": [
                {
                  "reference_month": "2024-05",   # or REF_DATE
                  "esdc_region_code": "35",        # or GEO
                  "region_name_en": "...",
                  "region_name_fr": "...",
                  "unemployment_rate": 5.4321,     # or VALUE
                  "employment_rate": 61.2,
                  "participation_rate": 64.7,
                  "national_unemployment_rate": 5.9
                },
                ...
              ]
            }

        api_version: Overrides the payload's ``source_api_version`` when given.
        retrieved_at: Timestamp stamped onto every record; defaults to now (UTC).
        data_stale: Sets the ``data_stale`` flag on every produced record.

    Returns:
        One :class:`LabourForceData` per record, covering every ESDC region in
        the payload, with all rate fields preserved to >= 4 decimal places.

    Raises:
        LFSParseError: If the payload is malformed or a record is missing a
            required field.
    """
    if not isinstance(raw, dict):
        raise LFSParseError("LFS payload must be a JSON object")

    records = raw.get("records")
    if records is None or not isinstance(records, Iterable):
        raise LFSParseError("LFS payload must contain a 'records' array")

    resolved_version = (
        api_version
        or raw.get("source_api_version")
        or raw.get("api_version")
        or "unknown"
    )
    ts = retrieved_at or datetime.now(timezone.utc)

    result: list[LabourForceData] = []
    for record in records:
        if not isinstance(record, dict):
            raise LFSParseError("each LFS record must be a JSON object")
        try:
            result.append(
                LabourForceData(
                    reference_month=_parse_reference_month(
                        _pick(record, "reference_month", "REF_DATE", "ref_date")
                    ),
                    esdc_region_code=str(
                        _pick(record, "esdc_region_code", "GEO", "region_code")
                    ),
                    region_name_en=str(_pick(record, "region_name_en") or ""),
                    region_name_fr=str(_pick(record, "region_name_fr") or ""),
                    unemployment_rate=_to_decimal(
                        _pick(record, "unemployment_rate", "VALUE", "value")
                    ),
                    employment_rate=_to_decimal(_pick(record, "employment_rate")),
                    participation_rate=_to_decimal(
                        _pick(record, "participation_rate")
                    ),
                    national_unemployment_rate=_to_decimal(
                        _pick(record, "national_unemployment_rate")
                    ),
                    source_api_version=str(resolved_version),
                    retrieved_at=ts,
                    data_stale=data_stale,
                )
            )
        except LFSParseError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalise to a parse error
            raise LFSParseError(f"could not parse LFS record: {exc}") from exc

    return result


def serialise(records: list[LabourForceData]) -> dict[str, Any]:
    """Serialise parsed records back to a canonical LFS payload dict.

    ``parse_lfs_response(serialise(parse_lfs_response(raw)))`` preserves every
    region and every rate to >= 4 decimal places (the P10 round-trip invariant,
    task 18.2). Decimal fields are emitted as strings to avoid float loss.
    """
    version = records[0].source_api_version if records else "unknown"
    return {
        "source_api_version": version,
        "records": [
            {
                "reference_month": r.reference_month.isoformat(),
                "esdc_region_code": r.esdc_region_code,
                "region_name_en": r.region_name_en,
                "region_name_fr": r.region_name_fr,
                "unemployment_rate": str(r.unemployment_rate),
                "employment_rate": str(r.employment_rate),
                "participation_rate": str(r.participation_rate),
                "national_unemployment_rate": str(r.national_unemployment_rate),
            }
            for r in records
        ],
    }


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------


class StatsCanadaFetcher:
    """Fetches LFS data from the upstream API or from local mock fixtures.

    In development/test the fetcher reads deterministic fixtures from
    ``mock-data/stats-canada/`` (Requirement 10.5). In production it performs an
    async ``httpx`` GET against the configured LFS URL with a 10 s connect /
    30 s read timeout (Requirement 10.1).
    """

    def __init__(
        self,
        *,
        lfs_url: str,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        api_version: str = "unknown",
        use_mock: bool = True,
        mock_dir: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._lfs_url = lfs_url
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._api_version = api_version
        self._use_mock = use_mock
        self._mock_dir = mock_dir
        #: Optional pre-built httpx.AsyncClient (injected by tests).
        self._client = client

    # -- mock path -----------------------------------------------------

    def _mock_path(self, region_code: str, year_month: str) -> Optional[str]:
        """Return the fixture path for a region/month, if the file exists.

        Tries the region+month specific fixture first, then a shared
        ``lfs_all.json`` fallback covering every region.
        """
        if not self._mock_dir:
            return None
        candidates = [
            os.path.join(self._mock_dir, f"lfs_{region_code}_{year_month}.json"),
            os.path.join(self._mock_dir, f"lfs_{region_code}.json"),
            os.path.join(self._mock_dir, "lfs_all.json"),
        ]
        for path in candidates:
            if os.path.isfile(path):
                return path
        return None

    def fetch_from_mock(
        self, region_code: str, year_month: str
    ) -> list[LabourForceData]:
        """Load and parse a fixture for ``region_code`` / ``year_month``.

        Raises:
            LFSParseError: If no fixture is found or it cannot be parsed.
        """
        path = self._mock_path(region_code, year_month)
        if path is None:
            raise LFSParseError(
                f"no LFS fixture for region={region_code} month={year_month}"
            )
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        records = parse_lfs_response(raw, api_version=self._api_version)
        return _filter_region_month(records, region_code, year_month)

    # -- live path -----------------------------------------------------

    async def fetch_from_api(
        self, region_code: str, year_month: str
    ) -> list[LabourForceData]:
        """Fetch and parse LFS data from the upstream API.

        Raises:
            RuntimeError: If ``httpx`` is not installed.
            UpstreamServerError: On a 5xx response (caller falls back to cache).
            UpstreamClientError: On a 4xx response.
            LFSParseError: If the response body cannot be parsed.
        """
        if not _HAS_HTTPX:  # pragma: no cover - exercised only without httpx
            raise RuntimeError("httpx is required for live LFS fetch but is absent")

        timeout = httpx.Timeout(
            connect=self._connect_timeout,
            read=self._read_timeout,
            write=self._read_timeout,
            pool=self._connect_timeout,
        )

        if self._client is not None:
            response = await self._client.get(self._lfs_url, timeout=timeout)
        else:  # pragma: no cover - real network path
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(self._lfs_url)

        status_code = response.status_code
        if 500 <= status_code < 600:
            raise UpstreamServerError(status_code)
        if 400 <= status_code < 500:
            raise UpstreamClientError(status_code)

        raw = response.json()
        records = parse_lfs_response(raw, api_version=self._api_version)
        return _filter_region_month(records, region_code, year_month)


def _filter_region_month(
    records: list[LabourForceData], region_code: str, year_month: str
) -> list[LabourForceData]:
    """Filter parsed records to a single region + reference month.

    ``year_month`` is ``YYYY-MM``; a record matches when its
    ``esdc_region_code`` equals ``region_code`` and its ``reference_month``
    falls within that month. If no record carries the month (single-month
    fixtures), region-only matches are returned.
    """
    region_matches = [r for r in records if r.esdc_region_code == region_code]
    month_matches = [
        r for r in region_matches if r.reference_month.strftime("%Y-%m") == year_month
    ]
    return month_matches or region_matches


# ---------------------------------------------------------------------------
# Cache wrapper
# ---------------------------------------------------------------------------


class StatsCanadaCache:
    """Reads/writes canonical LFS payloads to Redis with a 30-day TTL.

    The cached value is the canonical dict from :func:`serialise`, wrapped with
    a ``cached_at`` timestamp so staleness (> 30 days) can be evaluated on read
    without depending on Redis TTL semantics (Requirements 10.2, 10.3, 10.4).
    """

    def __init__(self, cache: AsyncCache | None) -> None:
        self._cache = cache

    async def write(
        self, region_code: str, year_month: str, records: list[LabourForceData]
    ) -> None:
        """Write records under ``statscan:lfs:{region}:{ym}`` with a 30-day TTL."""
        if self._cache is None:
            return
        key = statscan_lfs_key(region_code, year_month)
        envelope = {
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "payload": serialise(records),
        }
        await self._cache.set_with_ttl(
            key, json.dumps(envelope), STATSCAN_LFS_TTL_SECONDS
        )

    async def read(
        self, region_code: str, year_month: str
    ) -> tuple[list[LabourForceData], bool] | None:
        """Read cached records for a region/month.

        Returns:
            ``(records, is_stale)`` where ``is_stale`` is ``True`` when the
            entry is older than 30 days, or ``None`` on a cache miss / malformed
            entry.
        """
        if self._cache is None:
            return None
        key = statscan_lfs_key(region_code, year_month)
        raw = await self._cache.get(key)
        if raw is None:
            return None
        try:
            envelope = json.loads(raw)
            cached_at = datetime.fromisoformat(envelope["cached_at"])
            payload = envelope["payload"]
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            return None

        age_days = (datetime.now(timezone.utc) - cached_at).days
        is_stale = age_days > STALE_AFTER_DAYS
        records = parse_lfs_response(
            payload,
            retrieved_at=cached_at,
            data_stale=is_stale,
        )
        records = _filter_region_month(records, region_code, year_month)
        return records, is_stale


__all__ = [
    "AsyncCache",
    "LFSParseError",
    "StatsCanadaCache",
    "StatsCanadaFetcher",
    "UpstreamClientError",
    "UpstreamServerError",
    "STALE_AFTER_DAYS",
    "parse_lfs_response",
    "serialise",
]
