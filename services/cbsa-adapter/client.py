"""
services/cbsa-adapter/client.py
===============================
Transport layer for the CBSA Adapter.

Two collaborators are provided:

  * ``CBSAClient`` — an httpx-based client that fetches travel records over the
    CBSA Protected B secure channel using mutual TLS (client certificate + key
    + CA bundle from settings). It enforces a 5-second total timeout and never
    auto-retries (Requirements 13.1, 13.2). It never logs travel-record
    contents (Requirement 13.4).

  * ``SyntheticCBSAGenerator`` — a deterministic generator used in dev/test that
    derives a stable set of ``CrossingRecord`` objects from the claimant's
    ``sin_hash`` and their province of employment, seeded from fixtures in
    ``mock-data/cbsa/`` (Requirement 13.6). "Deterministic" means the same
    (province, sin_hash) inputs always yield byte-identical records.

Both collaborators are injected into the FastAPI app so unit tests can supply
fakes without any live network or mTLS.

Requirements: 13.1, 13.2, 13.4, 13.6
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, timedelta
from typing import Any

try:  # sibling module imported by bare name in the service directory
    from absence import CrossingRecord  # type: ignore
except ImportError:  # pragma: no cover - when imported as a package
    from .absence import CrossingRecord  # type: ignore

try:
    import httpx  # type: ignore

    _HAS_HTTPX = True
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore
    _HAS_HTTPX = False


class CBSAFetchError(Exception):
    """Raised when the CBSA channel fetch fails (timeout, transport, or HTTP).

    The message is deliberately generic and never contains travel-record
    contents so it is safe to surface and (if necessary) log.

    Any travel records that were successfully received before the failure are
    attached to ``partial_records`` so the caller can preserve them without a
    retry (Requirement 13.2). It defaults to an empty list.
    """

    def __init__(self, message: str, partial_records: list | None = None) -> None:
        super().__init__(message)
        self.partial_records: list = list(partial_records or [])


class CBSAClient:
    """httpx-backed client for the CBSA Protected B secure channel (mTLS).

    The client is constructed with the base URL and mTLS material from
    settings. A single 5-second total timeout bounds every request and NO
    automatic retry is performed: on any failure the caller receives a
    ``CBSAFetchError`` and decides how to degrade (Requirements 13.1, 13.2).
    """

    def __init__(
        self,
        base_url: str,
        *,
        client_cert_path: str = "",
        client_key_path: str = "",
        ca_cert_path: str = "",
        timeout: float = 5.0,
    ) -> None:
        if not _HAS_HTTPX:  # pragma: no cover - guarded import
            raise RuntimeError("httpx is required for the live CBSA client")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

        # Build the mTLS client-certificate tuple for httpx. httpx accepts
        # either a single PEM containing cert+key or a (cert, key) tuple.
        cert: Any = None
        if client_cert_path and client_key_path:
            cert = (client_cert_path, client_key_path)
        elif client_cert_path:
            cert = client_cert_path

        # ``verify`` may be a CA bundle path (server-cert verification). Default
        # True uses the system trust store.
        verify: Any = ca_cert_path if ca_cert_path else True

        # ``retries=0`` on the transport guarantees no auto-retry (Req 13.2).
        transport = httpx.HTTPTransport(retries=0)
        self._client = httpx.Client(
            base_url=self._base_url,
            cert=cert,
            verify=verify,
            timeout=httpx.Timeout(timeout),
            transport=transport,
        )

    def fetch_travel_records(self, sin_hash: str) -> list[CrossingRecord]:
        """Fetch and parse travel records for ``sin_hash`` over the channel.

        A single attempt is made — there is NO automatic retry (Req 13.2). On a
        timeout / transport / HTTP error the response is not usable and a
        ``CBSAFetchError`` is raised. Any records that had already been parsed
        (e.g. the server returns them incrementally) are attached to the error's
        ``partial_records`` so the caller can preserve them (Req 13.2).

        The exception message contains only the failure *type* — never
        travel-record contents (Req 13.4).
        """
        partial: list[CrossingRecord] = []
        try:
            response = self._client.get(f"/cbsa/travel/{sin_hash}")
            response.raise_for_status()
            payload = response.json()
            rows = (
                payload.get("records", [])
                if isinstance(payload, dict)
                else payload
            )
            for row in rows:
                # Parse incrementally so a malformed trailing row still leaves
                # the earlier, well-formed rows available as partial data.
                partial.append(CrossingRecord.from_dict(row))
            return partial
        except Exception as exc:  # noqa: BLE001 - normalise to a safe error
            # Only the exception *type* is surfaced; no travel data is included.
            raise CBSAFetchError(
                f"CBSA channel fetch failed: {type(exc).__name__}",
                partial_records=partial,
            ) from exc

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Deterministic synthetic generator (dev/test)
# ---------------------------------------------------------------------------

# Fallback per-province crossing "profiles" used when a province has no fixture
# file. Each profile is (number_of_absence_trips, base_absence_length_days).
# These are intentionally simple and stable so results are reproducible.
_DEFAULT_PROVINCE_PROFILES: dict[str, dict[str, Any]] = {
    "AB": {"trips": 1, "base_days": 12, "port": "YYC"},
    "BC": {"trips": 2, "base_days": 14, "port": "YVR"},
    "MB": {"trips": 1, "base_days": 8, "port": "YWG"},
    "NB": {"trips": 1, "base_days": 10, "port": "YFC"},
    "NL": {"trips": 0, "base_days": 0, "port": "YYT"},
    "NS": {"trips": 1, "base_days": 9, "port": "YHZ"},
    "NT": {"trips": 0, "base_days": 0, "port": "YZF"},
    "NU": {"trips": 0, "base_days": 0, "port": "YFB"},
    "ON": {"trips": 2, "base_days": 11, "port": "YYZ"},
    "PE": {"trips": 1, "base_days": 7, "port": "YYG"},
    "QC": {"trips": 2, "base_days": 13, "port": "YUL"},
    "SK": {"trips": 1, "base_days": 5, "port": "YXE"},
    "YT": {"trips": 0, "base_days": 0, "port": "YXY"},
}

# Deterministic anchor date used as the base of all generated crossings.
_ANCHOR_DATE = date(2024, 1, 1)


class SyntheticCBSAGenerator:
    """Deterministic synthetic ``CrossingRecord`` generator for dev/test.

    Records are derived from the province of employment and the claimant's
    ``sin_hash`` so that:
      * the same inputs always produce identical records (determinism), and
      * different provinces produce different travel profiles (Req 13.6).

    Province profiles are loaded from ``mock-data/cbsa/<PROVINCE>.json`` when
    present; otherwise a built-in default profile is used.
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
            province, {"trips": 0, "base_days": 0, "port": "UNK"}
        )

    @staticmethod
    def _seed(province: str, sin_hash: str) -> int:
        """A stable integer seed derived from province + sin_hash."""
        digest = hashlib.sha256(f"{province}:{sin_hash}".encode("utf-8")).hexdigest()
        return int(digest[:8], 16)

    def generate(self, sin_hash: str, province: str) -> list[CrossingRecord]:
        """Return deterministic crossing records for a claimant.

        If a fixture supplies an explicit ``records`` list, it is used verbatim
        (still deterministic). Otherwise the profile parameters drive a
        reproducible EXIT/ENTRY sequence.
        """
        province = (province or "").upper()
        profile = self._load_profile(province)

        # A fixture may pin an explicit record list for full control.
        explicit = profile.get("records")
        if isinstance(explicit, list):
            return [CrossingRecord.from_dict(row) for row in explicit]

        trips = int(profile.get("trips", 0))
        base_days = int(profile.get("base_days", 0))
        port = str(profile.get("port", "UNK"))

        if trips <= 0:
            return []

        seed = self._seed(province, sin_hash)
        records: list[CrossingRecord] = []
        cursor = _ANCHOR_DATE
        for trip_index in range(trips):
            # Deterministic per-trip jitter derived from the seed; stays small
            # so profile lengths remain meaningful.
            jitter = (seed >> (trip_index * 3)) % 4  # 0..3 days
            gap_before = 30 + ((seed >> trip_index) % 15)  # days in Canada first
            exit_date = cursor + timedelta(days=gap_before)
            absence_len = base_days + jitter
            entry_date = exit_date + timedelta(days=absence_len)
            records.append(
                CrossingRecord(
                    direction="EXIT", crossing_date=exit_date, port_of_entry=port
                )
            )
            records.append(
                CrossingRecord(
                    direction="ENTRY", crossing_date=entry_date, port_of_entry=port
                )
            )
            cursor = entry_date

        return records


__all__ = [
    "CBSAFetchError",
    "CBSAClient",
    "SyntheticCBSAGenerator",
]
