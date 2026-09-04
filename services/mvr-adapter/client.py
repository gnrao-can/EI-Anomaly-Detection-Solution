"""
services/mvr-adapter/client.py
==============================
Transport + data-minimisation layer for the MVR (Motor Vehicle Registry)
Adapter.

Three collaborators are provided:

  * :data:`ALLOWED_FIELDS` / :func:`project_allowed_fields` — the
    data-minimisation projection applied to *every* MVR response so that only
    ``vehicle_ownership_status`` and ``vehicle_class`` ever leave the adapter.
    No vehicle history, insurance, or financial data can propagate downstream
    (Requirement 15.4).

  * :class:`MVRClient` — an httpx-based client that queries the provincial MVR
    API for the claimant's residential province. It enforces a **3-second total
    timeout** and returns an ``UNAVAILABLE`` result on timeout or any error
    (Requirements 15.2, 15.3). Successful responses are run through the
    minimisation projection before being returned.

  * :class:`SyntheticMVRGenerator` — a deterministic generator used in dev/test
    that derives a stable ownership status + vehicle class from the claimant's
    ``sin_hash`` and residential province, seeded from fixtures in
    ``mock-data/motor-vehicle/`` — one fixture per province (Requirement 15.6).
    The production upstream shape (with extra fields) is preserved in the
    fixtures so the projection is genuinely exercised.

Both collaborators are injected into the FastAPI app so unit tests can supply
fakes without any live network.

Requirements: 15.1, 15.2, 15.3, 15.4, 15.6
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Optional

try:
    import httpx  # type: ignore

    _HAS_HTTPX = True
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore
    _HAS_HTTPX = False


# ---------------------------------------------------------------------------
# Data minimisation (Requirement 15.4)
# ---------------------------------------------------------------------------

#: The only fields permitted to leave the MVR Adapter. Every response — real or
#: synthetic — is filtered to (at most) these keys before it is returned. Any
#: history / insurance / financial fields present upstream are stripped.
ALLOWED_FIELDS: frozenset[str] = frozenset({"vehicle_ownership_status", "vehicle_class"})


#: Canonical ownership-status value used when a claimant's ownership cannot be
#: determined. Distinct from "NONE" (no vehicle) so the Geographic Anomaly
#: Engine can treat it as *unknown* rather than *absent* (Requirement 15.2).
OWNERSHIP_UNKNOWN = "UNKNOWN"
OWNERSHIP_NONE = "NONE"
OWNERSHIP_OWNED = "OWNED"


def project_allowed_fields(record: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *record* containing only :data:`ALLOWED_FIELDS`.

    This is the data-minimisation filter (Requirement 15.4): any field not in
    the allow-list — for example ``vehicle_history``, ``insurance_provider``,
    ``lien_balance``, ``vin`` — is stripped so it can never propagate
    downstream. Missing allowed fields are simply absent from the result (they
    are not fabricated).
    """
    return {k: v for k, v in record.items() if k in ALLOWED_FIELDS}


class MVRFetchError(Exception):
    """Raised when a provincial MVR fetch fails (timeout, transport, or HTTP).

    The message is deliberately generic and never contains vehicle-record
    contents so it is safe to surface and (if necessary) log.
    """


@dataclass
class MVRResult:
    """Outcome of an MVR lookup.

    Attributes
    ----------
    status:
        ``"OK"`` when a minimised record is returned; ``"UNAVAILABLE"`` when the
        provincial API timed out or errored (Requirements 15.2, 15.3).
    record:
        The minimised MVR record (only :data:`ALLOWED_FIELDS`) when ``status``
        is ``"OK"``; otherwise ``None``.
    reason:
        A safe, content-free reason string when ``status`` is ``"UNAVAILABLE"``.
    """

    status: str
    record: Optional[dict[str, Any]] = None
    reason: Optional[str] = None

    @classmethod
    def unavailable(cls, reason: str) -> "MVRResult":
        return cls(status="UNAVAILABLE", record=None, reason=reason)

    @classmethod
    def ok(cls, record: dict[str, Any]) -> "MVRResult":
        # Defensive: always minimise, even for a directly-constructed OK result.
        return cls(status="OK", record=project_allowed_fields(record), reason=None)


# ---------------------------------------------------------------------------
# Live provincial MVR client (production)
# ---------------------------------------------------------------------------


class MVRClient:
    """httpx-backed client for a province's Motor Vehicle Registry API.

    A single 3-second total timeout bounds the request (Requirements 15.3,
    15.5). On timeout or any error an ``UNAVAILABLE`` :class:`MVRResult` is
    returned so the Geographic Anomaly Engine treats ownership as *unknown*
    rather than *absent* (Requirement 15.2). Successful responses are minimised
    to :data:`ALLOWED_FIELDS` (Requirement 15.4).
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        timeout: float = 3.0,
    ) -> None:
        if not _HAS_HTTPX:  # pragma: no cover - guarded import
            raise RuntimeError("httpx is required for the live MVR client")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        # No auto-retry: a single attempt is made within the time budget.
        transport = httpx.HTTPTransport(retries=0)
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout),
            headers=headers,
            transport=transport,
        )

    def fetch_vehicle_record(self, sin_hash: str, province: str) -> MVRResult:
        """Fetch and minimise the claimant's vehicle record for *province*.

        Only ownership status and vehicle class are ever requested/returned
        (Requirement 15.4). On timeout or any error an ``UNAVAILABLE`` result is
        returned (Requirements 15.2, 15.3). The failure reason contains only the
        error *type* — never vehicle-record contents.
        """
        try:
            response = self._client.get(
                f"/mvr/{sin_hash}",
                params={"province": (province or "").upper(), "fields": ",".join(sorted(ALLOWED_FIELDS))},
            )
            response.raise_for_status()
            payload = response.json()
            record = payload.get("record", payload) if isinstance(payload, dict) else {}
            if not isinstance(record, dict):
                record = {}
            return MVRResult.ok(record)
        except Exception as exc:  # noqa: BLE001 - normalise to UNAVAILABLE
            return MVRResult.unavailable(f"MVR fetch failed: {type(exc).__name__}")

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Deterministic synthetic generator (dev/test)
# ---------------------------------------------------------------------------

# Fallback per-province "profiles" used when a province has no fixture file.
# ``owned_ratio`` is the fraction of the seed space that maps to OWNED; the rest
# map to NONE. ``classes`` is the deterministic class pool for owned vehicles.
_DEFAULT_PROVINCE_PROFILES: dict[str, dict[str, Any]] = {
    "AB": {"owned_ratio": 0.80, "classes": ["PASSENGER", "TRUCK"]},
    "BC": {"owned_ratio": 0.70, "classes": ["PASSENGER", "MOTORCYCLE"]},
    "MB": {"owned_ratio": 0.75, "classes": ["PASSENGER", "FARM"]},
    "NB": {"owned_ratio": 0.72, "classes": ["PASSENGER", "TRUCK"]},
    "NL": {"owned_ratio": 0.68, "classes": ["PASSENGER"]},
    "NS": {"owned_ratio": 0.70, "classes": ["PASSENGER", "TRUCK"]},
    "ON": {"owned_ratio": 0.65, "classes": ["PASSENGER", "COMMERCIAL"]},
    "PE": {"owned_ratio": 0.74, "classes": ["PASSENGER"]},
    "QC": {"owned_ratio": 0.66, "classes": ["PASSENGER", "MOTORCYCLE"]},
    "SK": {"owned_ratio": 0.78, "classes": ["PASSENGER", "FARM"]},
}


class SyntheticMVRGenerator:
    """Deterministic synthetic MVR record generator for dev/test.

    Records are derived from the residential province and the claimant's
    ``sin_hash`` so that:
      * the same inputs always produce an identical record (determinism), and
      * different provinces produce different profiles (Requirement 15.6).

    Province profiles are loaded from ``mock-data/motor-vehicle/<PROVINCE>.json``
    when present; otherwise a built-in default profile is used. Fixtures include
    extra (non-allowed) fields so the minimisation projection is exercised; the
    generator itself always returns a full (un-minimised) record and lets the
    projection strip it, matching the production code path.
    """

    def __init__(self, mock_data_dir: str) -> None:
        self._mock_data_dir = mock_data_dir

    def _load_profile(self, province: str) -> dict[str, Any]:
        """Load a province profile from a fixture, falling back to defaults."""
        path = os.path.join(self._mock_data_dir, f"{province}.json")
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        return _DEFAULT_PROVINCE_PROFILES.get(
            province, {"owned_ratio": 0.5, "classes": ["PASSENGER"]}
        )

    @staticmethod
    def _seed(province: str, sin_hash: str) -> int:
        """A stable integer seed derived from province + sin_hash."""
        digest = hashlib.sha256(f"{province}:{sin_hash}".encode("utf-8")).hexdigest()
        return int(digest[:8], 16)

    def generate(self, sin_hash: str, province: str) -> dict[str, Any]:
        """Return a deterministic, production-shaped vehicle record.

        If a fixture supplies an explicit ``record`` object, it is used verbatim
        (still deterministic). Otherwise the profile parameters drive a
        reproducible OWNED/NONE decision and vehicle class. The returned record
        deliberately carries extra production-shaped fields (history, insurance,
        financial) that MUST be stripped by :func:`project_allowed_fields`.
        """
        province = (province or "").upper()
        profile = self._load_profile(province)

        # A fixture may pin an explicit record for full control.
        explicit = profile.get("record")
        if isinstance(explicit, dict):
            return dict(explicit)

        seed = self._seed(province, sin_hash)
        owned_ratio = float(profile.get("owned_ratio", 0.5))
        classes = list(profile.get("classes") or ["PASSENGER"])

        # Deterministic ownership decision in [0, 1) compared to owned_ratio.
        fraction = (seed % 1000) / 1000.0
        owns_vehicle = fraction < owned_ratio

        if owns_vehicle:
            vehicle_class = classes[seed % len(classes)]
            ownership_status = OWNERSHIP_OWNED
        else:
            vehicle_class = None
            ownership_status = OWNERSHIP_NONE

        # Full production-shaped record (extra fields MUST be stripped later).
        return {
            # ── allowed / minimised fields ──
            "vehicle_ownership_status": ownership_status,
            "vehicle_class": vehicle_class,
            # ── extra production-shaped fields that MUST be stripped ──
            "vin": f"SYN{seed:08X}",
            "vehicle_history": ["registered", "renewed"] if owns_vehicle else [],
            "insurance_provider": "Synthetic Mutual" if owns_vehicle else None,
            "lien_balance": round((seed % 25000) / 1.0, 2) if owns_vehicle else 0.0,
            "registered_owner_name": f"Synthetic Owner {province}",
            "province": province,
        }


# ---------------------------------------------------------------------------
# Orchestrating helper used by main.py (dev/test synthetic or live prod)
# ---------------------------------------------------------------------------


def lookup_vehicle(
    settings: Any,
    sin_hash: str,
    province: str,
    *,
    synthetic: Optional["SyntheticMVRGenerator"] = None,
    live_client: Optional["MVRClient"] = None,
) -> MVRResult:
    """Resolve a claimant's minimised vehicle record for their province.

    Dev/test (or no API key) uses the deterministic synthetic generator; a live
    provincial API is queried otherwise. Every path minimises to
    :data:`ALLOWED_FIELDS` and returns ``UNAVAILABLE`` on error (Req 15.2, 15.3).
    """
    if getattr(settings, "use_synthetic", True):
        generator = synthetic or SyntheticMVRGenerator(
            getattr(settings, "mvr_mock_data_dir", "")
        )
        try:
            raw = generator.generate(sin_hash, province)
        except Exception as exc:  # noqa: BLE001 - degrade to UNAVAILABLE
            return MVRResult.unavailable(f"synthetic generation failed: {type(exc).__name__}")
        return MVRResult.ok(raw)

    if live_client is None:
        return MVRResult.unavailable("PROVINCE_API_UNAVAILABLE")
    return live_client.fetch_vehicle_record(sin_hash, province)


__all__ = [
    "ALLOWED_FIELDS",
    "OWNERSHIP_UNKNOWN",
    "OWNERSHIP_NONE",
    "OWNERSHIP_OWNED",
    "project_allowed_fields",
    "MVRFetchError",
    "MVRResult",
    "MVRClient",
    "SyntheticMVRGenerator",
    "lookup_vehicle",
]
