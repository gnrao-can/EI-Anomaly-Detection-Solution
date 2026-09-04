"""
services/regional-analysis-engine/client.py
============================================
Async adapter clients for the Regional Analysis Engine (task 16.1).

  * :class:`StatsCanadaClient` — queries the Stats Canada Adapter
    (``GET /statscan/lfs/{region_code}/{year_month}``) for the regional and
    national unemployment rates for the claim's reference month. On any failure
    (timeout, transport, non-2xx, ``DATA_UNAVAILABLE`` body, unparseable rates)
    it raises :class:`StatsCanadaUnavailableError` so ``main.py`` can annotate
    Indicators with ``DATA_UNAVAILABLE``.

  * :class:`JobBankClient` — queries the Job Bank Adapter
    (``GET /jobbank/postings/{noc}/{region}/{year_month}``) for the active
    posting count and median advertised wage. On failure it raises
    :class:`JobBankUnavailableError`.

Both clients accept an injected, duck-typed async HTTP client (anything with an
awaitable ``get(url, ...)`` returning an object exposing ``status_code`` and
``json()``), so unit tests can supply fakes with no live network and ``httpx``
is only required for the real network path.

Rates and wages are parsed via the ``Decimal(str(value))`` path to preserve the
>= 4 decimal places the Stats Canada Adapter guarantees (Requirement 10.6) and
the exact Job Bank median wage (Requirement 16.5).

Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

try:  # httpx is used for the live fetch path only
    import httpx

    _HAS_HTTPX = True
except ImportError:  # pragma: no cover - httpx absent in minimal environments
    httpx = None  # type: ignore[assignment]
    _HAS_HTTPX = False


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StatsCanadaUnavailableError(Exception):
    """Raised when regional/national rates cannot be obtained.

    ``main.py`` catches this to annotate produced Indicators (or a
    stand-alone Indicator) with ``DATA_UNAVAILABLE``.
    """


class JobBankUnavailableError(Exception):
    """Raised when Job Bank posting data cannot be obtained."""


# ---------------------------------------------------------------------------
# Result value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegionalRates:
    """Regional and national unemployment rates for a reference month."""

    regional_rate: Decimal
    national_rate: Decimal
    data_stale: bool = False


@dataclass(frozen=True)
class JobPostings:
    """Active posting count and median advertised wage for a NOC/region."""

    active_posting_count: int
    median_hourly_wage: Decimal
    data_stale: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_decimal(value: Any) -> Decimal:
    if value is None:
        raise ValueError("expected a numeric value, got None")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"cannot parse Decimal from {value!r}") from exc


async def _http_get(client: Any, url: str, timeout: float) -> Any:
    """Perform an async GET using an injected client or a fresh httpx client."""
    if client is not None:
        return await client.get(url)
    if not _HAS_HTTPX:  # pragma: no cover - exercised only without httpx
        raise RuntimeError("httpx is required for live fetch but is absent")
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as http:  # pragma: no cover - real network path
        return await http.get(url)


# ---------------------------------------------------------------------------
# Stats Canada client
# ---------------------------------------------------------------------------


class StatsCanadaClient:
    """Fetches regional + national unemployment rates from the Stats Canada Adapter."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout: float = 5.0,
        client: Any | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = client

    async def get_rates(self, region_code: str, year_month: str) -> RegionalRates:
        """Return the regional and national unemployment rates for a month.

        Args:
            region_code: ESDC economic region code.
            year_month: Reference month as ``YYYY-MM`` (month preceding the
                claim start date).

        Raises:
            StatsCanadaUnavailableError: On any transport / HTTP / body failure,
                or when the adapter reports ``DATA_UNAVAILABLE`` / returns no
                usable record.
        """
        url = f"{self._base_url}/statscan/lfs/{region_code}/{year_month}"
        try:
            response = await _http_get(self._client, url, self._timeout)
        except Exception as exc:  # noqa: BLE001 - normalise transport failures
            raise StatsCanadaUnavailableError(
                f"Stats Canada request failed: {type(exc).__name__}"
            ) from exc

        status_code = getattr(response, "status_code", 500)
        if status_code < 200 or status_code >= 300:
            raise StatsCanadaUnavailableError(
                f"Stats Canada Adapter returned {status_code}"
            )

        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            raise StatsCanadaUnavailableError("Stats Canada body was not JSON") from exc

        if not isinstance(body, dict):
            raise StatsCanadaUnavailableError("Stats Canada body was not an object")

        flag = body.get("flag")
        if flag == "DATA_UNAVAILABLE":
            raise StatsCanadaUnavailableError("Stats Canada reported DATA_UNAVAILABLE")

        records = body.get("records") or []
        if not records:
            raise StatsCanadaUnavailableError("Stats Canada returned no records")

        record = records[0]
        try:
            regional = _to_decimal(record.get("unemployment_rate"))
            national = _to_decimal(record.get("national_unemployment_rate"))
        except (ValueError, AttributeError) as exc:
            raise StatsCanadaUnavailableError(
                "Stats Canada record missing usable rates"
            ) from exc

        return RegionalRates(
            regional_rate=regional,
            national_rate=national,
            data_stale=flag == "DATA_STALE",
        )


# ---------------------------------------------------------------------------
# Job Bank client
# ---------------------------------------------------------------------------


class JobBankClient:
    """Fetches active postings + median wage from the Job Bank Adapter."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout: float = 5.0,
        client: Any | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = client

    async def get_postings(
        self, noc_code: str, region_code: str, year_month: str
    ) -> JobPostings:
        """Return the active posting count and median wage for a NOC/region.

        Raises:
            JobBankUnavailableError: On any transport / HTTP / body failure or
                when the adapter reports ``DATA_UNAVAILABLE``.
        """
        url = (
            f"{self._base_url}/jobbank/postings/"
            f"{noc_code}/{region_code}/{year_month}"
        )
        try:
            response = await _http_get(self._client, url, self._timeout)
        except Exception as exc:  # noqa: BLE001
            raise JobBankUnavailableError(
                f"Job Bank request failed: {type(exc).__name__}"
            ) from exc

        status_code = getattr(response, "status_code", 500)
        if status_code < 200 or status_code >= 300:
            raise JobBankUnavailableError(
                f"Job Bank Adapter returned {status_code}"
            )

        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            raise JobBankUnavailableError("Job Bank body was not JSON") from exc

        if not isinstance(body, dict):
            raise JobBankUnavailableError("Job Bank body was not an object")

        if body.get("flag") == "DATA_UNAVAILABLE":
            raise JobBankUnavailableError("Job Bank reported DATA_UNAVAILABLE")

        count = body.get("active_posting_count")
        wage = body.get("median_hourly_wage")
        if count is None or wage is None:
            raise JobBankUnavailableError("Job Bank body missing posting fields")

        try:
            return JobPostings(
                active_posting_count=int(count),
                median_hourly_wage=_to_decimal(wage),
                data_stale=body.get("flag") == "DATA_STALE",
            )
        except (ValueError, TypeError) as exc:
            raise JobBankUnavailableError("Job Bank body had unparseable fields") from exc


__all__ = [
    "StatsCanadaClient",
    "JobBankClient",
    "StatsCanadaUnavailableError",
    "JobBankUnavailableError",
    "RegionalRates",
    "JobPostings",
]
