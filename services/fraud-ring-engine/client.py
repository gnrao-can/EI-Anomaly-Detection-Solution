"""
services/fraud-ring-engine/client.py
=====================================
External-source clients for the Fraud Ring Engine (task 13.1).

Two collaborators are needed by the shell-employer and coordinated-layoff
detectors:

1. ``BusinessRegistryAdapterClient`` — an async client for the Business Registry
   Adapter's ``GET /registry/employer/{bn}`` endpoint. It returns the single
   most relevant ``EmployerRegistration`` for a BN, or ``None`` when there is no
   registry record. On registry unavailability (transport error, 5xx, or a
   ``SERVICE_UNAVAILABLE`` body) it degrades gracefully by returning a sentinel
   so the engine never crashes — the caller distinguishes "no record" (HIGH
   Indicator) from "registry unavailable" (skip the shell-employer check).

2. ``CohortSource`` — a repository/source that loads all employees separated in
   the 90-day window before a claim's separation date. A real deployment queries
   PostgreSQL (or the Graph Service); here we define the ``CohortSource``
   Protocol plus an in-memory ``FakeCohortSource`` for tests and an
   ``HttpCohortSource`` placeholder for wiring against a live source.

Both collaborators are dependency-injected so tests can supply fakes with no
live network.

Requirements: 5.2, 5.3, 5.4
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Optional, Protocol

try:  # sibling module imported by bare name inside the service directory
    from detection import CohortMember  # type: ignore
except ImportError:  # pragma: no cover - when imported as a package
    from .detection import CohortMember  # type: ignore


# ===========================================================================
# Registry lookup outcome
# ===========================================================================


class RegistryLookupStatus(str, Enum):
    """Outcome of a single employer registry lookup from the engine's view."""

    FOUND = "FOUND"          # A registration record exists.
    NO_RECORD = "NO_RECORD"  # Registry answered, but has no record for the BN.
    UNAVAILABLE = "UNAVAILABLE"  # Registry could not be reached / errored.


@dataclass
class RegistrationLookup:
    """Result of looking up an employer BN in the business registry.

    ``registration`` is the canonical record (a plain ``dict`` as returned over
    the wire, or ``None``). ``status`` disambiguates the three cases the
    shell-employer detector cares about.
    """

    status: RegistryLookupStatus
    registration: Optional[dict[str, Any]] = None


# ===========================================================================
# Business Registry Adapter client
# ===========================================================================


class BusinessRegistryAdapterClient:
    """Async client for the Business Registry Adapter ``/registry/employer/{bn}``.

    The adapter returns a ``RegistryLookupResult`` shape::

        {
          "bn": "...",
          "status": "OK" | "PARTIAL_RESULT" | "SERVICE_UNAVAILABLE",
          "registrations": [ {EmployerRegistration...}, ... ],
          "unreachable": [...]
        }

    This client selects the single most relevant registration (preferring a
    federal ``CA`` record, then the earliest registration date) and reports one
    of FOUND / NO_RECORD / UNAVAILABLE. It never raises on registry failure.
    """

    def __init__(
        self,
        base_url: str,
        http_client: Any,
        *,
        timeout_seconds: float = 5.0,
        bearer_token: Optional[str] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = http_client
        self._timeout = timeout_seconds
        # Internal GCKey Bearer presented to the auth-gated Business Registry
        # Adapter (Req 19.7). ``None`` (unit tests) => no Authorization header.
        self._headers = (
            {"Authorization": f"Bearer {bearer_token}"} if bearer_token else None
        )

    async def lookup_employer(self, bn: str) -> RegistrationLookup:
        """Look up an employer BN; degrade gracefully on any failure."""
        url = f"{self._base_url}/registry/employer/{bn}"
        try:
            resp = await self._http.get(
                url, timeout=self._timeout, headers=self._headers
            )
        except Exception:  # noqa: BLE001 - transport failure -> UNAVAILABLE
            return RegistrationLookup(status=RegistryLookupStatus.UNAVAILABLE)

        status_code = getattr(resp, "status_code", 200)
        if status_code >= 500 or status_code == 429:
            return RegistrationLookup(status=RegistryLookupStatus.UNAVAILABLE)
        if status_code >= 400 and status_code != 404:
            return RegistrationLookup(status=RegistryLookupStatus.UNAVAILABLE)

        try:
            body = resp.json()
        except Exception:  # noqa: BLE001 - malformed body -> UNAVAILABLE
            return RegistrationLookup(status=RegistryLookupStatus.UNAVAILABLE)

        return self._interpret_body(body)

    @staticmethod
    def _interpret_body(body: Any) -> RegistrationLookup:
        """Map an adapter response body to a ``RegistrationLookup``."""
        if not isinstance(body, dict):
            return RegistrationLookup(status=RegistryLookupStatus.UNAVAILABLE)

        if body.get("status") == "SERVICE_UNAVAILABLE":
            return RegistrationLookup(status=RegistryLookupStatus.UNAVAILABLE)

        registrations = body.get("registrations") or []
        if not registrations:
            return RegistrationLookup(status=RegistryLookupStatus.NO_RECORD)

        chosen = BusinessRegistryAdapterClient._select_registration(registrations)
        return RegistrationLookup(
            status=RegistryLookupStatus.FOUND, registration=chosen
        )

    @staticmethod
    def _select_registration(
        registrations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Pick the most relevant registration for shell-employer analysis.

        Preference order:
          1. A federal ``CA`` record (authoritative for the BN).
          2. The earliest registration date (a shell is most suspicious at its
             origin registration).
        """
        def _key(reg: dict[str, Any]) -> tuple[int, str]:
            is_federal = 0 if str(reg.get("jurisdiction", "")).upper() == "CA" else 1
            reg_date = reg.get("registration_date") or "9999-12-31"
            return (is_federal, str(reg_date))

        return sorted(registrations, key=_key)[0]


# ===========================================================================
# Cohort source (employees separated in the 90-day window)
# ===========================================================================


class CohortSource(Protocol):
    """Loads the 90-day separation cohort for an employer BN.

    Implementations return every employee separated within the
    ``window_days`` window BEFORE (and including) ``as_of`` for the given
    employer, as ``CohortMember`` records.
    """

    async def load_cohort(
        self,
        employer_bn: str,
        *,
        as_of: date,
        window_days: int,
    ) -> list[CohortMember]:
        ...


@dataclass
class FakeCohortSource:
    """In-memory ``CohortSource`` for tests (no database / network).

    Members are registered per employer BN via ``add`` / ``add_many`` and
    filtered to the ``window_days`` window ending at ``as_of`` on load. Set
    ``fail`` to simulate an unavailable data source (``load_cohort`` raises).
    """

    _by_employer: dict[str, list[CohortMember]] = field(default_factory=dict)
    fail: bool = False

    def add(self, employer_bn: str, member: CohortMember) -> None:
        self._by_employer.setdefault(employer_bn, []).append(member)

    def add_many(self, employer_bn: str, members: list[CohortMember]) -> None:
        self._by_employer.setdefault(employer_bn, []).extend(members)

    def fail_next(self) -> None:
        self.fail = True

    async def load_cohort(
        self,
        employer_bn: str,
        *,
        as_of: date,
        window_days: int,
    ) -> list[CohortMember]:
        if self.fail:
            raise RuntimeError("cohort source unavailable")

        members = self._by_employer.get(employer_bn, [])
        window_start = as_of.toordinal() - window_days
        filtered = [
            m
            for m in members
            if window_start <= m.separation_date.toordinal() <= as_of.toordinal()
        ]
        return sorted(filtered, key=lambda m: (m.separation_date, m.roe_submission_ts))


class HttpCohortSource:
    """HTTP-backed ``CohortSource`` placeholder for a live cohort endpoint.

    A production deployment would query the transactional store (PostgreSQL) or
    the Graph Service for employees separated in the 90-day window. The transport
    is injected so it can be swapped/mocked; any failure raises so the engine can
    treat the coordinated-layoff check as unavailable and skip it.
    """

    def __init__(
        self,
        base_url: str,
        http_client: Any,
        *,
        timeout_seconds: float = 5.0,
        bearer_token: Optional[str] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = http_client
        self._timeout = timeout_seconds
        # Internal GCKey Bearer (Req 19.7); ``None`` (unit tests) => no header.
        self._headers = (
            {"Authorization": f"Bearer {bearer_token}"} if bearer_token else None
        )

    async def load_cohort(
        self,
        employer_bn: str,
        *,
        as_of: date,
        window_days: int,
    ) -> list[CohortMember]:  # pragma: no cover - exercised against live source
        url = f"{self._base_url}/cohort/{employer_bn}"
        resp = await self._http.get(
            url,
            params={"as_of": as_of.isoformat(), "window_days": window_days},
            timeout=self._timeout,
            headers=self._headers,
        )
        data = resp.json()
        records = data.get("members", data) if isinstance(data, dict) else data
        cohort: list[CohortMember] = []
        for raw in records or []:
            cohort.append(
                CohortMember(
                    sin_hash=raw["sin_hash"],
                    separation_date=date.fromisoformat(raw["separation_date"]),
                    roe_submission_ts=datetime.fromisoformat(
                        raw["roe_submission_ts"]
                    ),
                    claim_id=raw.get("claim_id"),
                )
            )
        return cohort


__all__ = [
    "RegistryLookupStatus",
    "RegistrationLookup",
    "BusinessRegistryAdapterClient",
    "CohortSource",
    "FakeCohortSource",
    "HttpCohortSource",
]
