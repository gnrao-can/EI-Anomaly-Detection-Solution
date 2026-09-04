"""
services/business-registry-adapter/client.py
=============================================
Concurrent multi-registry query logic, canonical ``EmployerRegistration``
normalisation, and the residential/commercial zoning classifier for the
Business Registry Adapter.

The adapter queries the federal Corporations Canada registry plus the 13
provincial / territorial registries concurrently via ``asyncio.gather`` with
per-source error isolation:

  - Every reachable registry contributes its record(s) to the result.
  - Any unreachable jurisdiction is collected into ``unreachable`` and the
    overall status becomes ``PARTIAL_RESULT`` (Requirement 12.2).
  - If *every* registry fails, the status is ``SERVICE_UNAVAILABLE``
    (Requirement 12.3).

The registered address of each employer is classified as residential or
commercial using the GeoNames Adapter; when GeoNames is unavailable the
classifier falls back to Statistics-Canada FSA residential ranges and flags the
result ``UNVERIFIED_ZONING`` (Requirement 12.4).

In development / test the registry callers and the GeoNames caller are served
from ``mock-data/`` fixtures with no live network access (Requirement 12.5).

Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Awaitable, Callable, Optional, Protocol

try:  # config lives alongside this module in the service directory
    from config import (  # type: ignore
        FEDERAL_JURISDICTION,
        PROVINCIAL_JURISDICTIONS,
        Settings,
        get_settings,
    )
except ImportError:  # pragma: no cover - when imported as a package
    from .config import (  # type: ignore
        FEDERAL_JURISDICTION,
        PROVINCIAL_JURISDICTIONS,
        Settings,
        get_settings,
    )


# ===========================================================================
# Enumerations and canonical model
# ===========================================================================


class AddressType(str, Enum):
    """Zoning classification of an employer's registered address."""

    RESIDENTIAL = "RESIDENTIAL"
    COMMERCIAL = "COMMERCIAL"
    UNKNOWN = "UNKNOWN"


class RegistryStatus(str, Enum):
    """Overall outcome of a multi-registry lookup."""

    OK = "OK"
    PARTIAL_RESULT = "PARTIAL_RESULT"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"


class CorporateStatus(str, Enum):
    """Normalised corporate status across heterogeneous registries."""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    DISSOLVED = "DISSOLVED"
    UNKNOWN = "UNKNOWN"


# Statistics-Canada style residential FSA ranges used as the zoning fallback
# when GeoNames is unavailable. A real deployment loads the full FSA
# classification table; here we keep a representative set so the fallback path
# is exercised deterministically in tests.
RESIDENTIAL_FSA_RANGES: frozenset[str] = frozenset(
    {
        "A1B",  # St. John's, NL (residential)
        "B3H",  # Halifax, NS (residential)
        "K1S",  # Ottawa, ON (residential)
        "L4C",  # Richmond Hill, ON (residential)
        "M4E",  # Toronto Beaches, ON (residential)
        "T2N",  # Calgary, AB (residential)
        "V5K",  # Vancouver, BC (residential)
    }
)


@dataclass
class EmployerRegistration:
    """Canonical business-registration record normalised across all registries.

    Attributes
    ----------
    bn:
        Employer Business Number (9-digit base or 15-char full BN).
    legal_name:
        Registered legal / operating name of the corporation.
    jurisdiction:
        Registry the record came from (``CA`` federal, or a province /
        territory 2-char code).
    registration_date:
        Date the corporation was registered / incorporated.
    corporate_status:
        Normalised corporate status.
    registered_address:
        Full registered address string.
    registered_postal_code:
        Postal code of the registered address (used for zoning classification).
    address_type:
        Residential / commercial / unknown zoning classification.
    unverified_zoning:
        ``True`` when ``address_type`` was derived from the FSA fallback (i.e.
        GeoNames was unavailable) — the ``UNVERIFIED_ZONING`` flag.
    """

    bn: str
    legal_name: str
    jurisdiction: str
    registration_date: Optional[date]
    corporate_status: CorporateStatus
    registered_address: str
    registered_postal_code: str
    address_type: AddressType = AddressType.UNKNOWN
    unverified_zoning: bool = False

    # ------------------------------------------------------------------
    # Equality and hashing (identity = BN + jurisdiction)
    # ------------------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, EmployerRegistration):
            return NotImplemented
        return self.bn == other.bn and self.jurisdiction == other.jurisdiction

    def __hash__(self) -> int:
        return hash((self.bn, self.jurisdiction))

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable ``dict`` representation."""
        return {
            "bn": self.bn,
            "legal_name": self.legal_name,
            "jurisdiction": self.jurisdiction,
            "registration_date": (
                self.registration_date.isoformat()
                if self.registration_date is not None
                else None
            ),
            "corporate_status": self.corporate_status.value,
            "registered_address": self.registered_address,
            "registered_postal_code": self.registered_postal_code,
            "address_type": self.address_type.value,
            "unverified_zoning": self.unverified_zoning,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EmployerRegistration":
        """Restore an ``EmployerRegistration`` from a ``to_dict()`` mapping."""
        raw_reg_date = data.get("registration_date")
        return cls(
            bn=data["bn"],
            legal_name=data["legal_name"],
            jurisdiction=data["jurisdiction"],
            registration_date=(
                date.fromisoformat(raw_reg_date) if raw_reg_date else None
            ),
            corporate_status=CorporateStatus(
                data.get("corporate_status", CorporateStatus.UNKNOWN.value)
            ),
            registered_address=data.get("registered_address", ""),
            registered_postal_code=data.get("registered_postal_code", ""),
            address_type=AddressType(
                data.get("address_type", AddressType.UNKNOWN.value)
            ),
            unverified_zoning=bool(data.get("unverified_zoning", False)),
        )


@dataclass
class RegistryLookupResult:
    """Aggregated result of a federal + provincial multi-registry lookup."""

    bn: str
    status: RegistryStatus
    registrations: list[EmployerRegistration] = field(default_factory=list)
    unreachable: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bn": self.bn,
            "status": self.status.value,
            "registrations": [r.to_dict() for r in self.registrations],
            "unreachable": list(self.unreachable),
        }


# ===========================================================================
# Normalisation
# ===========================================================================


_STATUS_ALIASES: dict[str, CorporateStatus] = {
    "active": CorporateStatus.ACTIVE,
    "in good standing": CorporateStatus.ACTIVE,
    "registered": CorporateStatus.ACTIVE,
    "inactive": CorporateStatus.INACTIVE,
    "suspended": CorporateStatus.INACTIVE,
    "dissolved": CorporateStatus.DISSOLVED,
    "cancelled": CorporateStatus.DISSOLVED,
    "struck": CorporateStatus.DISSOLVED,
}


def _normalise_status(raw: Any) -> CorporateStatus:
    if raw is None:
        return CorporateStatus.UNKNOWN
    return _STATUS_ALIASES.get(str(raw).strip().lower(), CorporateStatus.UNKNOWN)


def _parse_date(raw: Any) -> Optional[date]:
    if raw is None or raw == "":
        return None
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw))
    except (ValueError, TypeError):
        return None


def normalise_registration(raw: dict[str, Any], jurisdiction: str) -> EmployerRegistration:
    """Map a raw registry record onto the canonical ``EmployerRegistration``.

    Heterogeneous registries use different field names; this accepts the common
    aliases (``bn``/``business_number``, ``legal_name``/``name``, etc.) so a
    single canonical model is produced regardless of source jurisdiction.
    """
    bn = str(raw.get("bn") or raw.get("business_number") or raw.get("bn9") or "").strip()
    legal_name = str(
        raw.get("legal_name") or raw.get("name") or raw.get("corporate_name") or ""
    ).strip()
    registered_address = str(
        raw.get("registered_address") or raw.get("address") or ""
    ).strip()
    postal_code = str(
        raw.get("registered_postal_code")
        or raw.get("postal_code")
        or raw.get("postal")
        or ""
    ).strip()

    return EmployerRegistration(
        bn=bn,
        legal_name=legal_name,
        jurisdiction=str(raw.get("jurisdiction") or jurisdiction).strip(),
        registration_date=_parse_date(
            raw.get("registration_date") or raw.get("incorporation_date")
        ),
        corporate_status=_normalise_status(
            raw.get("corporate_status") or raw.get("status")
        ),
        registered_address=registered_address,
        registered_postal_code=postal_code,
    )


# ===========================================================================
# Zoning classifier
# ===========================================================================


class ZoningResolver(Protocol):
    """Callable protocol for resolving a postal code's zoning via GeoNames."""

    async def __call__(self, postal_code: str) -> Optional[bool]:
        """Return ``True`` if residential, ``False`` if commercial, ``None`` if unavailable."""
        ...


@dataclass
class ZoningClassification:
    """Outcome of classifying a registered address."""

    address_type: AddressType
    unverified_zoning: bool


async def classify_address(
    postal_code: str,
    geonames_resolver: Optional[ZoningResolver],
) -> ZoningClassification:
    """Classify a registered address as residential / commercial / unknown.

    Strategy (Requirement 12.4):
      1. Ask the GeoNames Adapter for the postal code's zoning. If it answers,
         use it (verified zoning).
      2. If GeoNames is unavailable (resolver missing, raises, or returns
         ``None``), fall back to the residential FSA ranges and flag the result
         ``UNVERIFIED_ZONING``.
    """
    fsa = (postal_code or "").replace(" ", "").upper()[:3]

    if geonames_resolver is not None:
        try:
            is_residential = await geonames_resolver(postal_code)
        except Exception:  # noqa: BLE001 - GeoNames failure -> fallback path
            is_residential = None
        if is_residential is not None:
            return ZoningClassification(
                address_type=(
                    AddressType.RESIDENTIAL
                    if is_residential
                    else AddressType.COMMERCIAL
                ),
                unverified_zoning=False,
            )

    # ── Fallback: FSA residential ranges; flag UNVERIFIED_ZONING ──
    if fsa in RESIDENTIAL_FSA_RANGES:
        return ZoningClassification(
            address_type=AddressType.RESIDENTIAL, unverified_zoning=True
        )
    if fsa:
        # A known FSA that isn't in the residential set is treated as commercial
        # but still unverified because GeoNames could not confirm it.
        return ZoningClassification(
            address_type=AddressType.COMMERCIAL, unverified_zoning=True
        )
    return ZoningClassification(
        address_type=AddressType.UNKNOWN, unverified_zoning=True
    )


# ===========================================================================
# Registry callers (dependency-injected)
# ===========================================================================

# A registry caller resolves a BN within one jurisdiction to a list of raw
# registry records. Raising any exception marks that jurisdiction unreachable.
RegistryCaller = Callable[[str, str], Awaitable[list[dict[str, Any]]]]


class RegistryUnavailableError(Exception):
    """Raised by a registry caller when the jurisdiction cannot be reached."""


def _read_fixture(path: str) -> Optional[list[dict[str, Any]]]:
    """Load a JSON fixture and normalise it to a list of records (or ``None``)."""
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # Fixture may wrap records under "records"/"registrations", or be a
        # single record keyed by BN.
        for key in ("records", "registrations"):
            if key in data and isinstance(data[key], list):
                return data[key]
        return [data]
    return None


def make_fixture_registry_caller(
    federal_dir: str,
    provincial_dir: str,
    *,
    unreachable_jurisdictions: Optional[set[str]] = None,
) -> RegistryCaller:
    """Build a fixture-backed registry caller for dev/test (no network).

    Federal (``CA``) lookups read from ``federal_dir``; provincial / territorial
    lookups read from ``provincial_dir``. ``unreachable_jurisdictions`` lets a
    test force specific jurisdictions to fail so PARTIAL_RESULT / SERVICE_
    UNAVAILABLE paths can be exercised deterministically.
    """
    unreachable = unreachable_jurisdictions or set()

    async def _caller(bn: str, jurisdiction: str) -> list[dict[str, Any]]:
        if jurisdiction in unreachable:
            raise RegistryUnavailableError(jurisdiction)

        base_dir = federal_dir if jurisdiction == FEDERAL_JURISDICTION else provincial_dir
        # Prefer a BN-specific fixture, then a jurisdiction-wide fixture.
        candidates = [
            os.path.join(base_dir, f"{jurisdiction}_{bn}.json"),
            os.path.join(base_dir, f"{jurisdiction}.json"),
        ]
        if jurisdiction == FEDERAL_JURISDICTION:
            candidates.insert(0, os.path.join(base_dir, f"federal_{bn}.json"))
            candidates.append(os.path.join(base_dir, "federal.json"))

        for path in candidates:
            records = _read_fixture(path)
            if records is None:
                continue
            # Keep only records matching the requested BN when the fixture is a
            # jurisdiction-wide file; BN-specific files pass through unfiltered.
            matched = [r for r in records if str(r.get("bn") or r.get("business_number") or "").strip() == bn]
            return matched if matched else (records if f"_{bn}.json" in path or f"{bn}.json" in path else [])

        # No fixture for this jurisdiction: treat as "reachable, no record".
        return []

    return _caller


# ===========================================================================
# Concurrent multi-registry query
# ===========================================================================


async def query_all_registries(
    bn: str,
    *,
    registry_caller: RegistryCaller,
    geonames_resolver: Optional[ZoningResolver] = None,
    jurisdictions: Optional[list[str]] = None,
) -> RegistryLookupResult:
    """Query the federal + provincial registries concurrently for one BN.

    All jurisdictions are queried concurrently via ``asyncio.gather`` with
    ``return_exceptions=True`` so one failing registry never aborts the others
    (per-source error isolation).

    Result classification (Requirements 12.1–12.3):
      - Every jurisdiction reachable            → ``OK``
      - Some reachable, at least one unreachable → ``PARTIAL_RESULT`` with the
        list of unreachable jurisdictions
      - Every jurisdiction unreachable          → ``SERVICE_UNAVAILABLE``

    Each returned registration's address is classified for zoning
    (Requirement 12.4).
    """
    all_jurisdictions = jurisdictions or (
        [FEDERAL_JURISDICTION] + list(PROVINCIAL_JURISDICTIONS)
    )

    async def _one(jurisdiction: str) -> tuple[str, list[dict[str, Any]]]:
        records = await registry_caller(bn, jurisdiction)
        return jurisdiction, records

    gathered = await asyncio.gather(
        *(_one(j) for j in all_jurisdictions),
        return_exceptions=True,
    )

    registrations: list[EmployerRegistration] = []
    unreachable: list[str] = []
    reachable_count = 0

    for jurisdiction, outcome in zip(all_jurisdictions, gathered):
        if isinstance(outcome, Exception):
            unreachable.append(jurisdiction)
            continue
        reachable_count += 1
        _, records = outcome
        for raw in records:
            registrations.append(normalise_registration(raw, jurisdiction))

    # ── All registries failed -> SERVICE_UNAVAILABLE ──
    if reachable_count == 0:
        return RegistryLookupResult(
            bn=bn,
            status=RegistryStatus.SERVICE_UNAVAILABLE,
            registrations=[],
            unreachable=unreachable,
        )

    # ── Classify zoning for each found registration ──
    for reg in registrations:
        classification = await classify_address(
            reg.registered_postal_code, geonames_resolver
        )
        reg.address_type = classification.address_type
        reg.unverified_zoning = classification.unverified_zoning

    status = (
        RegistryStatus.PARTIAL_RESULT if unreachable else RegistryStatus.OK
    )
    return RegistryLookupResult(
        bn=bn,
        status=status,
        registrations=registrations,
        unreachable=unreachable,
    )


# ===========================================================================
# HTTP-backed callers (production) — constructed from settings
# ===========================================================================


def make_http_registry_caller(
    settings: Settings,
    http_client: Any,
) -> RegistryCaller:
    """Build a registry caller that queries live registry HTTP endpoints.

    ``http_client`` is an ``httpx.AsyncClient``-compatible object. Any transport
    error or non-2xx response for a jurisdiction raises
    ``RegistryUnavailableError`` so the aggregator marks that jurisdiction
    unreachable rather than failing the whole lookup.
    """

    async def _caller(bn: str, jurisdiction: str) -> list[dict[str, Any]]:
        if jurisdiction == FEDERAL_JURISDICTION:
            base = settings.corporations_canada_base_url
        else:
            base = settings.provincial_registry_base_urls.get(jurisdiction)
            if not base:
                raise RegistryUnavailableError(jurisdiction)
        url = f"{base.rstrip('/')}/employers/{bn}"
        try:
            resp = await http_client.get(
                url, timeout=settings.registry_timeout_seconds
            )
        except Exception as exc:  # noqa: BLE001 - transport failure -> unreachable
            raise RegistryUnavailableError(jurisdiction) from exc
        if resp.status_code >= 500 or resp.status_code == 429:
            raise RegistryUnavailableError(jurisdiction)
        if resp.status_code == 404:
            return []
        if resp.status_code >= 400:
            raise RegistryUnavailableError(jurisdiction)
        data = resp.json()
        if isinstance(data, list):
            return data
        return [data]

    return _caller


def make_http_geonames_resolver(
    settings: Settings,
    http_client: Any,
) -> ZoningResolver:
    """Build a GeoNames zoning resolver backed by the GeoNames Adapter."""

    async def _resolver(postal_code: str) -> Optional[bool]:
        url = f"{settings.geonames_adapter_url.rstrip('/')}/geonames/zoning/{postal_code}"
        try:
            resp = await http_client.get(
                url, timeout=settings.geonames_timeout_seconds
            )
        except Exception:  # noqa: BLE001 - GeoNames unavailable -> fallback
            return None
        if resp.status_code != 200:
            return None
        data = resp.json()
        value = data.get("is_residential")
        return bool(value) if value is not None else None

    return _resolver


__all__ = [
    "AddressType",
    "RegistryStatus",
    "CorporateStatus",
    "EmployerRegistration",
    "RegistryLookupResult",
    "ZoningClassification",
    "ZoningResolver",
    "RegistryCaller",
    "RegistryUnavailableError",
    "RESIDENTIAL_FSA_RANGES",
    "normalise_registration",
    "classify_address",
    "query_all_registries",
    "make_fixture_registry_caller",
    "make_http_registry_caller",
    "make_http_geonames_resolver",
]
