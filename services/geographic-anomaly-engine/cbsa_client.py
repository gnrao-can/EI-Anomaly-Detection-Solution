"""
services/geographic-anomaly-engine/cbsa_client.py
==================================================
Async client for the CBSA Adapter, used by the Geographic Anomaly Engine to
detect cross-border absences during the employment period (task 12.2).

Adapter response contract (task 21.1 — ``services/cbsa-adapter/main.py``):
  * ``GET /cbsa/travel/{sin_hash}?province=XX``
  * 200 -> ``{"sin_hash", "status": "OK"|"PARTIAL", "record_count", "partial",
             "absence_periods": [{"start", "end", "days"}, ...], "records": [...]}``
  * 502 -> ``{"sin_hash", "status": "FAILED", "reason", "partial",
             "record_count", "absence_periods": [...], "records": [...]}``

The adapter already computes ``absence_periods`` via ``detect_absence_periods``
(EXIT -> ENTRY gaps >= 10 days). The engine only needs to know the maximum
consecutive days outside Canada so it can raise a HIGH Indicator when the
claimant was outside Canada for >= 10 consecutive days during employment
(Requirement 4.9 / design step 6). Even on a 502 FAILED response, any partial
absence periods that were computed are still consulted, so an absence detected
from partial data is not silently dropped.

An abstract :class:`CBSAClient` protocol plus an injectable
:class:`FakeCBSAClient` keep the engine testable without live network.

Requirements: 4.9
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

try:  # httpx is the canonical async HTTP client for the FastAPI stack.
    import httpx  # type: ignore

    _HAS_HTTPX = True
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore
    _HAS_HTTPX = False


#: The minimum consecutive days outside Canada that triggers a HIGH Indicator
#: (inclusive lower bound; matches the CBSA adapter's ``MIN_ABSENCE_DAYS``).
MIN_ABSENCE_DAYS: int = 10


@dataclass(frozen=True)
class CBSAAbsenceResult:
    """Cross-border absence summary resolved from the CBSA Adapter.

    Attributes
    ----------
    available:
        ``False`` when the adapter channel failed with no usable data. When the
        adapter returns partial data (still carrying absence periods), this is
        ``True`` and ``partial`` is set.
    partial:
        ``True`` when the underlying travel data was partial.
    absence_periods:
        The absence periods reported by the adapter, each a dict with
        ``start``, ``end``, and ``days`` keys.
    """

    available: bool
    partial: bool = False
    absence_periods: list[dict[str, Any]] = field(default_factory=list)

    @property
    def max_absence_days(self) -> int:
        """The longest single absence in days (0 when there are none)."""
        if not self.absence_periods:
            return 0
        return max(int(p.get("days", 0)) for p in self.absence_periods)

    @property
    def has_long_absence(self) -> bool:
        """``True`` iff any absence lasted >= 10 consecutive days (Req 4.9)."""
        return self.max_absence_days >= MIN_ABSENCE_DAYS


# ---------------------------------------------------------------------------
# Client protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class CBSAClient(Protocol):
    """Protocol for fetching cross-border absence periods for a claimant (async)."""

    async def get_absences(self, sin_hash: str, province: str) -> CBSAAbsenceResult:
        """Return the claimant's cross-border absence summary.

        Never raises for adapter unavailability: a failed channel yields an
        ``available=False`` result (with any partial periods preserved).
        """
        ...


# ---------------------------------------------------------------------------
# HTTP client (production)
# ---------------------------------------------------------------------------


class CBSAAdapterClient:
    """Async client calling the CBSA Adapter over HTTP.

    A 502 FAILED response still carries any partial absence periods, so the
    engine consults them (``available`` reflects whether the channel returned a
    usable answer). Any transport/timeout/parse error yields an
    ``available=False`` result with no periods.
    """

    def __init__(
        self,
        base_url: str,
        *,
        connect_timeout: float = 1.0,
        read_timeout: float = 5.0,
    ) -> None:
        if not _HAS_HTTPX:  # pragma: no cover - environment guard
            raise RuntimeError("httpx is required for CBSAAdapterClient")
        self._base_url = base_url.rstrip("/")
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout

    async def get_absences(self, sin_hash: str, province: str) -> CBSAAbsenceResult:
        url = f"{self._base_url}/cbsa/travel/{sin_hash}"
        timeout = httpx.Timeout(
            connect=self._connect_timeout,
            read=self._read_timeout,
            write=self._read_timeout,
            pool=self._connect_timeout,
        )
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, params={"province": province})
        except Exception:  # noqa: BLE001 - any transport error -> unavailable
            return CBSAAbsenceResult(available=False)

        try:
            data: dict[str, Any] = resp.json()
        except Exception:  # noqa: BLE001 - malformed body -> unavailable
            return CBSAAbsenceResult(available=False)

        periods = list(data.get("absence_periods") or [])
        partial = bool(data.get("partial", False))
        status = str(data.get("status", ""))

        if resp.status_code == 200:
            return CBSAAbsenceResult(
                available=True, partial=partial, absence_periods=periods
            )

        # Non-200 (e.g. 502 FAILED): the channel failed, but partial periods may
        # still be present and usable.
        return CBSAAbsenceResult(
            available=bool(periods),
            partial=True,
            absence_periods=periods,
        )


# ---------------------------------------------------------------------------
# Fake client (tests / dev)
# ---------------------------------------------------------------------------


class FakeCBSAClient:
    """In-memory :class:`CBSAClient` for tests — no network.

    Register per-(sin_hash) absence periods with :meth:`set_absences`, or mark a
    sin_hash unavailable with :meth:`set_unavailable`. Any sin_hash not
    registered defaults to an available result with no absences.
    """

    def __init__(self) -> None:
        self._results: dict[str, CBSAAbsenceResult] = {}
        self.calls: list[tuple[str, str]] = []

    def set_absences(
        self, sin_hash: str, periods: list[dict[str, Any]], *, partial: bool = False
    ) -> None:
        self._results[sin_hash] = CBSAAbsenceResult(
            available=True, partial=partial, absence_periods=list(periods)
        )

    def set_absence_days(self, sin_hash: str, days: int) -> None:
        """Convenience: register a single absence of ``days`` days."""
        self._results[sin_hash] = CBSAAbsenceResult(
            available=True,
            absence_periods=[{"start": None, "end": None, "days": days}],
        )

    def set_unavailable(self, sin_hash: str) -> None:
        self._results[sin_hash] = CBSAAbsenceResult(available=False)

    async def get_absences(self, sin_hash: str, province: str) -> CBSAAbsenceResult:
        self.calls.append((sin_hash, province))
        return self._results.get(sin_hash, CBSAAbsenceResult(available=True))


__all__ = [
    "MIN_ABSENCE_DAYS",
    "CBSAAbsenceResult",
    "CBSAClient",
    "CBSAAdapterClient",
    "FakeCBSAClient",
]
