"""
services/history-inconsistency-engine/client.py
===============================================
Async CRA Adapter client for the Employment History Inconsistency Engine
(task 14.1).

This module owns the engine's *outbound* dependency on the CRA Adapter, which
serves data-minimised T4 records over ``GET /cra/t4/{sin_hash}/{tax_year}/{bn}``.

The engine must translate three distinct upstream conditions into a single
"UNRESOLVED" signal so the engine layer can emit an UNRESOLVED Indicator
(Requirements 6.1, 6.3):

  1. **Adapter unavailable** — the CRA Adapter did not answer within the
     5-second timeout, refused the connection, returned a 5xx, or the adapter
     itself reported ``{"status": "UNAVAILABLE"}`` / ``{"status": "NOT_FOUND"}``.
  2. **Missing field** — the T4 record came back but has no
     ``employment_income`` field.
  3. **Non-numeric field** — ``employment_income`` is present but cannot be
     parsed as an exact numeric amount.

:class:`T4Lookup` is the structured result the engine consumes: on success it
carries the T4 employment income as an exact :class:`~decimal.Decimal`; on any
of the three failure conditions above ``resolved`` is ``False`` and ``reason``
explains why.

The HTTP transport is injectable. :class:`CRAAdapterClient` accepts an optional
``transport`` callable (async) so tests can supply a fake without any live
network. In production the default transport uses ``httpx.AsyncClient`` with the
configured 5-second timeout.

Requirements: 6.1, 6.2, 6.3
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("history-inconsistency-engine.client")

#: Async transport signature: given a fully-qualified URL and a timeout in
#: seconds, return the parsed JSON body (a dict). Implementations should raise
#: on transport / timeout / HTTP errors so the client can map them to
#: UNRESOLVED.
Transport = Callable[[str, float], Awaitable[dict[str, Any]]]


# Reason codes for an unresolved lookup (surfaced in the UNRESOLVED Indicator).
REASON_UNAVAILABLE = "CRA_UNAVAILABLE"
REASON_MISSING_FIELD = "T4_FIELD_MISSING"
REASON_NON_NUMERIC = "T4_FIELD_NON_NUMERIC"


@dataclass
class T4Lookup:
    """Outcome of a CRA T4 lookup.

    Attributes
    ----------
    resolved:
        ``True`` when a usable, numeric ``employment_income`` was obtained.
    t4_earnings:
        The T4 employment income as an exact ``Decimal`` when ``resolved``;
        otherwise ``None``.
    reason:
        A machine-readable reason code when NOT resolved (one of
        :data:`REASON_UNAVAILABLE`, :data:`REASON_MISSING_FIELD`,
        :data:`REASON_NON_NUMERIC`); ``None`` when resolved.
    noc_category:
        The CRA NOC category when present (used by task 14.2); optional.
    """

    resolved: bool
    t4_earnings: Optional[Decimal] = None
    reason: Optional[str] = None
    noc_category: Optional[str] = None

    @property
    def unresolved(self) -> bool:
        return not self.resolved


class CRAAdapterUnavailable(Exception):
    """Raised internally when the CRA Adapter cannot be reached / times out."""


class CRAAdapterClient:
    """Async client that fetches a T4 record from the CRA Adapter.

    The transport is injectable for testing. When no transport is supplied a
    default ``httpx.AsyncClient``-backed transport is built lazily using the
    5-second timeout from settings.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 5.0,
        transport: Optional[Transport] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport or _build_default_transport()

    async def fetch_t4(
        self, sin_hash: str, tax_year: int, employer_bn: str
    ) -> T4Lookup:
        """Fetch and validate a T4 record, mapping every failure to UNRESOLVED.

        Returns a resolved :class:`T4Lookup` carrying an exact ``Decimal``
        ``t4_earnings`` on success. On adapter unavailability / timeout /
        5xx / adapter-reported UNAVAILABLE or NOT_FOUND, or on a missing or
        non-numeric ``employment_income`` field, returns an unresolved lookup
        with the appropriate reason code (Requirements 6.1, 6.3).
        """
        url = f"{self._base_url}/cra/t4/{sin_hash}/{tax_year}/{employer_bn}"

        try:
            body = await self._transport(url, self._timeout_seconds)
        except CRAAdapterUnavailable:
            logger.info("cra_unavailable", extra={"reason": "transport_error"})
            return T4Lookup(resolved=False, reason=REASON_UNAVAILABLE)
        except Exception as exc:  # noqa: BLE001 - any transport failure => unavailable
            logger.info("cra_unavailable", extra={"reason": repr(exc)})
            return T4Lookup(resolved=False, reason=REASON_UNAVAILABLE)

        return self._interpret(body)

    def _interpret(self, body: dict[str, Any]) -> T4Lookup:
        """Map an adapter JSON body to a :class:`T4Lookup`."""
        if not isinstance(body, dict):
            return T4Lookup(resolved=False, reason=REASON_UNAVAILABLE)

        status = str(body.get("status", "OK")).upper()
        # The adapter reports UNAVAILABLE on its own timeout and NOT_FOUND when
        # no in-retention record exists; both are treated as UNRESOLVED here.
        if status in {"UNAVAILABLE", "NOT_FOUND"}:
            return T4Lookup(resolved=False, reason=REASON_UNAVAILABLE)

        # The record may be nested under "record" (adapter shape) or be the body
        # itself (flat shape) — support both.
        record = body.get("record")
        if not isinstance(record, dict):
            record = body

        if "employment_income" not in record:
            return T4Lookup(resolved=False, reason=REASON_MISSING_FIELD)

        raw_income = record.get("employment_income")
        earnings = _to_decimal_or_none(raw_income)
        if earnings is None:
            return T4Lookup(resolved=False, reason=REASON_NON_NUMERIC)

        noc = record.get("noc_category")
        return T4Lookup(
            resolved=True,
            t4_earnings=earnings,
            noc_category=str(noc) if noc is not None else None,
        )

    async def fetch_employer_noc_category(
        self, sin_hash: str, tax_year: int, employer_bn: str
    ) -> Optional[str]:
        """Return the CRA employer industry NOC category for the T4, or ``None``.

        This reuses the T4 lookup: the CRA-minimised T4 record carries the
        employer's ``noc_category`` (see the CRA Adapter's ``ALLOWED_FIELDS``).
        Returns ``None`` when the lookup is unresolved or the category is absent,
        so the NOC-mismatch check simply does not fire on missing data
        (Requirement 6.5).
        """
        lookup = await self.fetch_t4(sin_hash, tax_year, employer_bn)
        if lookup.unresolved:
            return None
        return lookup.noc_category

    async def fetch_concurrent_employment(
        self, sin_hash: str, claim_start: str, claim_end: str
    ) -> list[dict[str, Any]]:
        """Return employment spans active for the SIN during the claim period.

        Queries the CRA Adapter's concurrent-employment endpoint
        (``/cra/employment/{sin_hash}?start=..&end=..``) for every Business
        Number the SIN was active at during the claim period. Each span is a
        dict shaped ``{"employer_bn", "start_date", "end_date"}`` (end_date may
        be ``None`` for an ongoing span).

        On any transport failure / timeout, or an adapter-reported UNAVAILABLE /
        NOT_FOUND status, an empty list is returned (the engine treats a failed
        concurrent-employment query as "no second employer found" rather than
        crashing; the T4 UNRESOLVED path already covers CRA unavailability for
        the primary analysis).
        """
        url = (
            f"{self._base_url}/cra/employment/{sin_hash}"
            f"?start={claim_start}&end={claim_end}"
        )
        try:
            body = await self._transport(url, self._timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - failure => no concurrent data
            logger.info("cra_concurrent_unavailable", extra={"reason": repr(exc)})
            return []

        if not isinstance(body, dict):
            return []
        status = str(body.get("status", "OK")).upper()
        if status in {"UNAVAILABLE", "NOT_FOUND"}:
            return []

        spans = body.get("employment_spans")
        if spans is None:
            spans = body.get("spans")
        if not isinstance(spans, list):
            return []

        result: list[dict[str, Any]] = []
        for entry in spans:
            if not isinstance(entry, dict):
                continue
            bn = entry.get("employer_bn")
            start = entry.get("start_date")
            if bn is None or start is None:
                continue
            result.append(
                {
                    "employer_bn": str(bn),
                    "start_date": str(start),
                    "end_date": (
                        str(entry["end_date"])
                        if entry.get("end_date") is not None
                        else None
                    ),
                }
            )
        return result


def _to_decimal_or_none(value: Any) -> Optional[Decimal]:
    """Convert a value to an exact ``Decimal`` or ``None`` if not numeric.

    ``None`` and booleans are treated as non-numeric. Numeric strings, ints and
    floats are converted through their string form to preserve exactness and
    avoid binary-float noise.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _build_default_transport() -> Transport:
    """Build the default ``httpx.AsyncClient``-backed async transport.

    The returned coroutine issues a GET with the given per-request timeout and
    returns the parsed JSON body. Any timeout / connection / HTTP-status error
    is raised as :class:`CRAAdapterUnavailable` so the client maps it to an
    UNRESOLVED result.
    """

    async def _transport(url: str, timeout_seconds: float) -> dict[str, Any]:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - httpx is a pinned dep
            raise CRAAdapterUnavailable("httpx unavailable") from exc

        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as http:
                response = await http.get(url)
                response.raise_for_status()
                return response.json()
        except Exception as exc:  # noqa: BLE001 - map all failures to unavailable
            raise CRAAdapterUnavailable(str(exc)) from exc

    return _transport


__all__ = [
    "Transport",
    "T4Lookup",
    "CRAAdapterClient",
    "CRAAdapterUnavailable",
    "REASON_UNAVAILABLE",
    "REASON_MISSING_FIELD",
    "REASON_NON_NUMERIC",
]
