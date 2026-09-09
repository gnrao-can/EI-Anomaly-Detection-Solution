"""
services/geographic-anomaly-engine/mvr_client.py
=================================================
Async client for the MVR (Motor Vehicle Registry) Adapter, used by the
Geographic Anomaly Engine to determine whether the claimant has a vehicle
registered in their residential province (task 12.2).

Adapter response contract (task 23.1 — ``services/mvr-adapter/main.py``):
  * ``GET /mvr/{sin_hash}?province=XX``
  * 200 -> ``{"sin_hash", "province", "status": "OK",
             "record": {"vehicle_ownership_status", "vehicle_class"}}``
  * 502 -> ``{"sin_hash", "province", "status": "UNAVAILABLE", "reason": ...}``

The engine maps the response to one of three ownership dispositions:
  * ``OWNED``    — ``vehicle_ownership_status`` indicates the claimant owns a
                    vehicle. No HIGH "no vehicle" Indicator.
  * ``NONE``     — the adapter confirms NO registered vehicle -> the engine
                    emits a HIGH Indicator (Req 4.3).
  * ``UNKNOWN``  — the adapter is UNAVAILABLE / errored: ownership is treated as
                    *unknown*, NOT *absent*, so NO HIGH Indicator is emitted
                    (design: "treats vehicle ownership as UNKNOWN (not absent)";
                    Req 15.2).

An abstract :class:`MVRClient` protocol plus an injectable
:class:`FakeMVRClient` keep the engine testable without live network.

Requirements: 4.3, 15.2
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
# Ownership dispositions
# ---------------------------------------------------------------------------

#: Claimant owns a registered vehicle.
OWNERSHIP_OWNED = "OWNED"
#: Adapter confirmed NO registered vehicle (triggers the HIGH Indicator).
OWNERSHIP_NONE = "NONE"
#: Ownership could not be determined (adapter UNAVAILABLE); treat as unknown.
OWNERSHIP_UNKNOWN = "UNKNOWN"

#: ``vehicle_ownership_status`` string values that indicate ownership.
_OWNED_VALUES = {"OWNED", "REGISTERED", "ACTIVE", "YES", "TRUE"}
#: ``vehicle_ownership_status`` string values that indicate no vehicle.
_NONE_VALUES = {"NONE", "NOT_REGISTERED", "NO_VEHICLE", "NO", "FALSE", ""}


@dataclass(frozen=True)
class MVRResult:
    """Vehicle-ownership disposition resolved from the MVR Adapter.

    Attributes
    ----------
    disposition:
        One of ``OWNED``, ``NONE``, or ``UNKNOWN``.
    vehicle_ownership_status:
        Raw status string from the adapter (``None`` when unavailable).
    vehicle_class:
        Vehicle class from the adapter (``None`` when unavailable).
    province:
        Residential province code the lookup was performed for.
    """

    disposition: str
    vehicle_ownership_status: Optional[str] = None
    vehicle_class: Optional[str] = None
    province: str = ""

    @property
    def owns_vehicle(self) -> bool:
        return self.disposition == OWNERSHIP_OWNED

    @property
    def no_vehicle(self) -> bool:
        return self.disposition == OWNERSHIP_NONE

    @property
    def unknown(self) -> bool:
        return self.disposition == OWNERSHIP_UNKNOWN


def classify_ownership(
    status: Optional[str],
    *,
    province: str = "",
    vehicle_class: Optional[str] = None,
) -> MVRResult:
    """Classify a raw ``vehicle_ownership_status`` into an :class:`MVRResult`.

    A ``None`` status maps to ``UNKNOWN`` (adapter did not provide it). Any
    unrecognised non-empty status is conservatively treated as ``UNKNOWN`` so
    the engine never emits a false HIGH "no vehicle" Indicator (Req 15.2).
    """
    if status is None:
        return MVRResult(
            disposition=OWNERSHIP_UNKNOWN, province=province, vehicle_class=vehicle_class
        )
    normalised = str(status).strip().upper()
    if normalised in _OWNED_VALUES:
        disposition = OWNERSHIP_OWNED
    elif normalised in _NONE_VALUES:
        disposition = OWNERSHIP_NONE
    else:
        disposition = OWNERSHIP_UNKNOWN
    return MVRResult(
        disposition=disposition,
        vehicle_ownership_status=status,
        vehicle_class=vehicle_class,
        province=province,
    )


# ---------------------------------------------------------------------------
# Client protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class MVRClient(Protocol):
    """Protocol for resolving vehicle ownership for a claimant (async)."""

    async def get_ownership(self, sin_hash: str, province: str) -> MVRResult:
        """Return the vehicle-ownership disposition for a claimant.

        Never raises for adapter unavailability: an UNAVAILABLE / errored
        adapter yields an ``UNKNOWN`` disposition.
        """
        ...


# ---------------------------------------------------------------------------
# HTTP client (production)
# ---------------------------------------------------------------------------


class MVRAdapterClient:
    """Async client calling the MVR Adapter over HTTP.

    Any non-200 response (including the adapter's 502 UNAVAILABLE) or any
    transport/timeout/parse error yields an ``UNKNOWN`` disposition — ownership
    is treated as unknown, not absent (Req 15.2).
    """

    def __init__(
        self,
        base_url: str,
        *,
        connect_timeout: float = 1.0,
        read_timeout: float = 3.0,
        bearer_token: Optional[str] = None,
    ) -> None:
        if not _HAS_HTTPX:  # pragma: no cover - environment guard
            raise RuntimeError("httpx is required for MVRAdapterClient")
        self._base_url = base_url.rstrip("/")
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        # Internal GCKey Bearer presented to the auth-gated MVR Adapter
        # (Req 19.7). ``None`` (unit tests) => no Authorization header.
        self._headers = (
            {"Authorization": f"Bearer {bearer_token}"} if bearer_token else None
        )

    async def get_ownership(self, sin_hash: str, province: str) -> MVRResult:
        url = f"{self._base_url}/mvr/{sin_hash}"
        timeout = httpx.Timeout(
            connect=self._connect_timeout,
            read=self._read_timeout,
            write=self._read_timeout,
            pool=self._connect_timeout,
        )
        try:
            async with httpx.AsyncClient(timeout=timeout, headers=self._headers) as client:
                resp = await client.get(url, params={"province": province})
        except Exception:  # noqa: BLE001 - any transport error -> unknown
            return MVRResult(disposition=OWNERSHIP_UNKNOWN, province=province)

        if resp.status_code != 200:
            return MVRResult(disposition=OWNERSHIP_UNKNOWN, province=province)

        try:
            data: dict[str, Any] = resp.json()
            record = data.get("record") or {}
            status = record.get("vehicle_ownership_status")
            vehicle_class = record.get("vehicle_class")
        except Exception:  # noqa: BLE001 - malformed body -> unknown
            return MVRResult(disposition=OWNERSHIP_UNKNOWN, province=province)

        return classify_ownership(
            status, province=province, vehicle_class=vehicle_class
        )


# ---------------------------------------------------------------------------
# Fake client (tests / dev)
# ---------------------------------------------------------------------------


class FakeMVRClient:
    """In-memory :class:`MVRClient` for tests — no network.

    Register per-(sin_hash) results with :meth:`set_owned`, :meth:`set_none`,
    or :meth:`set_unavailable`. Any sin_hash not registered defaults to
    ``UNKNOWN`` (unavailable), exercising the safe degradation path.
    """

    def __init__(self) -> None:
        self._results: dict[str, MVRResult] = {}
        self.calls: list[tuple[str, str]] = []

    def set_owned(self, sin_hash: str, vehicle_class: str = "PASSENGER") -> None:
        self._results[sin_hash] = MVRResult(
            disposition=OWNERSHIP_OWNED,
            vehicle_ownership_status="OWNED",
            vehicle_class=vehicle_class,
        )

    def set_none(self, sin_hash: str) -> None:
        self._results[sin_hash] = MVRResult(
            disposition=OWNERSHIP_NONE,
            vehicle_ownership_status="NONE",
        )

    def set_unavailable(self, sin_hash: str) -> None:
        self._results[sin_hash] = MVRResult(disposition=OWNERSHIP_UNKNOWN)

    async def get_ownership(self, sin_hash: str, province: str) -> MVRResult:
        self.calls.append((sin_hash, province))
        result = self._results.get(sin_hash)
        if result is None:
            return MVRResult(disposition=OWNERSHIP_UNKNOWN, province=province)
        # Attach the province used for the lookup.
        return MVRResult(
            disposition=result.disposition,
            vehicle_ownership_status=result.vehicle_ownership_status,
            vehicle_class=result.vehicle_class,
            province=province,
        )


__all__ = [
    "OWNERSHIP_OWNED",
    "OWNERSHIP_NONE",
    "OWNERSHIP_UNKNOWN",
    "MVRResult",
    "classify_ownership",
    "MVRClient",
    "MVRAdapterClient",
    "FakeMVRClient",
]
