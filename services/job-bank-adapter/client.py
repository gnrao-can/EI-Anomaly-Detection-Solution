"""
services/job-bank-adapter/client.py
===================================
Data-plane for the Job Bank Adapter (task 24.1):

  * :class:`JobBankFetcher` — an ``httpx``-based client that queries the Job
    Bank Canada REST API by NOC code + ESDC economic region for postings active
    during a claim period, and parses the response into :class:`JobBankData`
    (Requirement 16.1). A single request is made with a 5-second timeout.

  * :class:`JobBankSnapshotStore` — reads the open-data snapshot from
    ``mock-data/job-bank/`` for a NOC/region combination. A snapshot older than
    the configured maximum age (7 days) is considered unusable (Requirement
    16.3). In dev/test the snapshot is always used (Requirement 16.4).

  * :class:`JobBankCache` — a thin wrapper around the shared ``RedisCache`` (or
    any duck-typed async cache) that stores the canonical serialised payload
    under ``job_bank:postings:{noc}:{region}:{ym}`` with a 7-day TTL, using the
    ``job_bank_postings_key`` builder from ``shared/cache_keys.py``
    (Requirement 16.2).

Requirements: 16.1, 16.2, 16.3, 16.4
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional, Protocol

from shared.cache_keys import JOB_BANK_POSTINGS_TTL_SECONDS, job_bank_postings_key

try:  # sibling module imported by bare name in the service directory
    from serialization import (  # type: ignore
        JobBankData,
        parse_job_bank_data,
        serialise_job_bank_data,
    )
except ImportError:  # pragma: no cover - when imported as a package
    from .serialization import (  # type: ignore
        JobBankData,
        parse_job_bank_data,
        serialise_job_bank_data,
    )

try:  # httpx is used for the live fetch path only
    import httpx

    _HAS_HTTPX = True
except ImportError:  # pragma: no cover - httpx absent in minimal environments
    httpx = None  # type: ignore[assignment]
    _HAS_HTTPX = False


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class JobBankParseError(Exception):
    """Raised when a Job Bank payload cannot be parsed into JobBankData."""


class JobBankUnavailableError(Exception):
    """Raised when the live Job Bank API is unavailable (timeout / transport /
    5xx / 4xx). The caller falls back to the snapshot (Requirement 16.3)."""


class SnapshotStaleError(Exception):
    """Raised when a snapshot exists but is older than the max allowed age."""


class SnapshotMissingError(Exception):
    """Raised when no snapshot fixture exists for a NOC/region combination."""


# ---------------------------------------------------------------------------
# Cache protocol (duck-typed to shared.cache.RedisCache)
# ---------------------------------------------------------------------------


class AsyncCache(Protocol):
    """Minimal async cache interface used by :class:`JobBankCache`.

    ``shared.cache.RedisCache`` satisfies this protocol; unit tests provide an
    in-memory implementation so no Redis server (or ``redis`` package) is
    required.
    """

    async def get(self, key: str) -> str | None: ...

    async def set_with_ttl(self, key: str, value: str, ttl_seconds: int) -> None: ...


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_api_payload(
    raw: dict[str, Any], noc: str, region: str, *, data_stale: bool = False
) -> JobBankData:
    """Parse a raw Job Bank API payload into :class:`JobBankData`.

    Accepts the design's canonical field names as well as a couple of common
    API spellings so fixtures and live responses both parse.
    """
    if not isinstance(raw, dict):
        raise JobBankParseError("Job Bank payload must be a JSON object")

    def pick(*names: str) -> Any:
        for name in names:
            if name in raw and raw[name] is not None:
                return raw[name]
        return None

    count = pick("active_posting_count", "posting_count", "count")
    wage = pick("median_hourly_wage", "median_advertised_wage", "median_wage")
    if count is None or wage is None:
        raise JobBankParseError(
            "Job Bank payload missing active_posting_count / median_hourly_wage"
        )

    reference_month = pick("reference_month", "ref_month")

    try:
        return JobBankData(
            noc_code=str(pick("noc_code", "noc") or noc),
            region_code=str(pick("region_code", "region", "esdc_region") or region),
            active_posting_count=int(count),
            median_hourly_wage=Decimal(str(wage)),
            reference_month=(
                _coerce_month(reference_month) if reference_month else None
            ),
            data_stale=data_stale,
        )
    except JobBankParseError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalise to a parse error
        raise JobBankParseError(f"could not parse Job Bank payload: {exc}") from exc


def _coerce_month(value: Any) -> date:
    if isinstance(value, date):
        return value
    text = str(value)
    if len(text) == 7:  # YYYY-MM
        text = f"{text}-01"
    return date.fromisoformat(text)


# ---------------------------------------------------------------------------
# Fetcher (live API)
# ---------------------------------------------------------------------------


class JobBankFetcher:
    """Fetches Job Bank posting data from the upstream REST API.

    A single request is made with a total timeout (default 5 s) and NO
    automatic retry, consistent with the adapter time budget. Any failure
    (timeout, transport, non-2xx) raises :class:`JobBankUnavailableError` so the
    caller falls back to the snapshot.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        timeout: float = 5.0,
        client: Any | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        #: Optional pre-built httpx.AsyncClient (injected by tests).
        self._client = client

    async def fetch(
        self,
        noc: str,
        region: str,
        *,
        period_start: Optional[str] = None,
        period_end: Optional[str] = None,
    ) -> JobBankData:
        """Query active postings for ``noc`` in ``region`` over the claim period.

        Args:
            noc: NOC 2021 code.
            region: ESDC economic region code.
            period_start: Optional claim-period start (``YYYY-MM-DD``).
            period_end: Optional claim-period end (``YYYY-MM-DD``).

        Returns:
            The parsed :class:`JobBankData`.

        Raises:
            JobBankUnavailableError: On any transport / HTTP failure.
            JobBankParseError: If a 2xx body cannot be parsed.
        """
        if not _HAS_HTTPX:  # pragma: no cover - exercised only without httpx
            raise JobBankUnavailableError("httpx is required for live fetch")

        url = f"{self._base_url}/postings"
        params: dict[str, str] = {"noc": noc, "region": region}
        if period_start:
            params["period_start"] = period_start
        if period_end:
            params["period_end"] = period_end
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        timeout = httpx.Timeout(self._timeout)

        try:
            if self._client is not None:
                response = await self._client.get(
                    url, params=params, headers=headers, timeout=timeout
                )
            else:  # pragma: no cover - real network path
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.get(url, params=params, headers=headers)
        except Exception as exc:  # noqa: BLE001 - normalise transport failures
            raise JobBankUnavailableError(
                f"Job Bank API request failed: {type(exc).__name__}"
            ) from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise JobBankUnavailableError(
                f"Job Bank API returned {response.status_code}"
            )

        raw = response.json()
        return _parse_api_payload(raw, noc, region, data_stale=False)


# ---------------------------------------------------------------------------
# Snapshot store (open data fallback)
# ---------------------------------------------------------------------------


class JobBankSnapshotStore:
    """Serves Job Bank posting data from the local open-data snapshot.

    Fixtures live in ``mock-data/job-bank/`` and may be either:
      * a per-NOC/region file ``postings_{noc}_{region}.json``, or
      * a shared ``postings.json`` mapping ``"{noc}:{region}"`` -> payload.

    Each fixture payload carries the posting fields plus an optional
    ``snapshot_date`` (``YYYY-MM-DD``). A snapshot whose ``snapshot_date`` is
    older than ``max_age_days`` is rejected as stale (Requirement 16.3). When a
    fixture omits ``snapshot_date`` it is treated as fresh (test fixtures are
    generated fresh).
    """

    def __init__(self, snapshot_dir: str, *, max_age_days: int = 7) -> None:
        self._snapshot_dir = snapshot_dir
        self._max_age_days = max_age_days

    def _load_payload(self, noc: str, region: str) -> Optional[dict[str, Any]]:
        specific = os.path.join(
            self._snapshot_dir, f"postings_{noc}_{region}.json"
        )
        if os.path.isfile(specific):
            with open(specific, "r", encoding="utf-8") as handle:
                return json.load(handle)

        shared_path = os.path.join(self._snapshot_dir, "postings.json")
        if os.path.isfile(shared_path):
            with open(shared_path, "r", encoding="utf-8") as handle:
                index = json.load(handle)
            if isinstance(index, dict):
                return index.get(f"{noc}:{region}")
        return None

    def _snapshot_age_days(self, payload: dict[str, Any]) -> Optional[int]:
        snapshot_date = payload.get("snapshot_date")
        if not snapshot_date:
            return None
        try:
            captured = date.fromisoformat(str(snapshot_date))
        except ValueError:
            return None
        return (date.today() - captured).days

    def load(self, noc: str, region: str, *, data_stale: bool = True) -> JobBankData:
        """Load a snapshot for ``noc`` / ``region``.

        Args:
            noc: NOC 2021 code.
            region: ESDC economic region code.
            data_stale: Value stamped onto the returned model's ``data_stale``
                flag. Snapshot-served data is marked stale by default
                (Requirement 16.3).

        Returns:
            The parsed :class:`JobBankData`.

        Raises:
            SnapshotMissingError: If no snapshot fixture exists.
            SnapshotStaleError: If the snapshot is older than ``max_age_days``.
            JobBankParseError: If the fixture cannot be parsed.
        """
        payload = self._load_payload(noc, region)
        if payload is None:
            raise SnapshotMissingError(
                f"no Job Bank snapshot for noc={noc} region={region}"
            )

        age = self._snapshot_age_days(payload)
        if age is not None and age > self._max_age_days:
            raise SnapshotStaleError(
                f"Job Bank snapshot for noc={noc} region={region} is {age} days old"
            )

        return _parse_api_payload(payload, noc, region, data_stale=data_stale)


# ---------------------------------------------------------------------------
# Cache wrapper
# ---------------------------------------------------------------------------


class JobBankCache:
    """Reads/writes canonical Job Bank payloads to Redis with a 7-day TTL.

    The cached value is the canonical dict from
    :func:`serialise_job_bank_data`, wrapped with a ``cached_at`` timestamp so a
    served entry can be reported accurately (Requirement 16.2). The cache key is
    built via ``job_bank_postings_key`` from ``shared/cache_keys.py``.
    """

    def __init__(self, cache: AsyncCache | None) -> None:
        self._cache = cache

    async def write(self, year_month: str, data: JobBankData) -> None:
        """Write ``data`` under ``job_bank:postings:{noc}:{region}:{ym}``."""
        if self._cache is None:
            return
        key = job_bank_postings_key(data.noc_code, data.region_code, year_month)
        envelope = {
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "payload": serialise_job_bank_data(data),
        }
        await self._cache.set_with_ttl(
            key, json.dumps(envelope), JOB_BANK_POSTINGS_TTL_SECONDS
        )

    async def read(
        self, noc: str, region: str, year_month: str
    ) -> Optional[JobBankData]:
        """Read cached data for a NOC/region/month, or ``None`` on miss."""
        if self._cache is None:
            return None
        key = job_bank_postings_key(noc, region, year_month)
        raw = await self._cache.get(key)
        if raw is None:
            return None
        try:
            envelope = json.loads(raw)
            payload = envelope["payload"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return None
        try:
            return parse_job_bank_data(payload)
        except (JobBankParseError, KeyError, ValueError):
            return None


__all__ = [
    "AsyncCache",
    "JobBankFetcher",
    "JobBankSnapshotStore",
    "JobBankCache",
    "JobBankParseError",
    "JobBankUnavailableError",
    "SnapshotStaleError",
    "SnapshotMissingError",
]
