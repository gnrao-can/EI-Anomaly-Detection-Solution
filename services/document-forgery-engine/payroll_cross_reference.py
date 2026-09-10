"""
services/document-forgery-engine/payroll_cross_reference.py
===========================================================
CRA payroll cross-reference for the Document Forgery Engine (task 15.3).

Design step 6 of the Document Forgery Engine (design "5. Document Forgery
Engine"):

    6. Payroll cross-reference (CRA Adapter):
       - Query CRA for SIN in employer's payroll for the employment period
       - If SIN not found in payroll -> CRITICAL Indicator (Requirement 7.7)

This module owns the engine's outbound dependency on the CRA Adapter's payroll
endpoint and the pure decision logic that turns a payroll lookup into an
Indicator.

The CRA Adapter is queried at
``GET /cra/payroll/{sin_hash}/{employer_bn}?start=..&end=..`` and returns whether
the SIN appears in the employer's payroll records for the employment period.
The HTTP transport is injectable (same pattern as the History Inconsistency
Engine's ``client.py``) so this is fully unit-testable with a fake — no live
network is required, and tests MUST mock the CRA Adapter call.

Failure handling
----------------
A SIN that is *confirmed absent* from payroll is a hard forgery signal
(Requirement 7.7 -> CRITICAL). By contrast, an *unavailable* CRA Adapter
(timeout / 5xx / adapter-reported UNAVAILABLE) is NOT the same as "SIN not in
payroll": flagging CRITICAL on adapter unavailability would produce false
positives whenever CRA is down. Per the pipeline's graceful-degradation posture
(cf. Requirement 6.2 for the sibling engine), an unavailable adapter yields no
CRITICAL Indicator; the absence must be positively confirmed.

Requirements: 7.7
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from shared.models import EngineType, Indicator, Severity

logger = logging.getLogger("document-forgery-engine.payroll")

#: Async transport signature: given a fully-qualified URL and a timeout in
#: seconds, return the parsed JSON body (a dict). Implementations raise on
#: transport / timeout / HTTP errors so the client can map them to UNAVAILABLE.
Transport = Callable[[str, float], Awaitable[dict[str, Any]]]

#: Payroll lookup dispositions.
STATUS_FOUND = "FOUND"
STATUS_NOT_FOUND = "NOT_FOUND"
STATUS_UNAVAILABLE = "UNAVAILABLE"

#: Default CRA Adapter timeout in seconds (mirrors Requirement 11.3 for CRA).
DEFAULT_CRA_TIMEOUT_SECONDS: float = 5.0


@dataclass(frozen=True)
class PayrollLookup:
    """Outcome of a CRA payroll cross-reference lookup.

    Attributes
    ----------
    status:
        One of :data:`STATUS_FOUND`, :data:`STATUS_NOT_FOUND`, or
        :data:`STATUS_UNAVAILABLE`.
    """

    status: str

    @property
    def found(self) -> bool:
        return self.status == STATUS_FOUND

    @property
    def not_found(self) -> bool:
        return self.status == STATUS_NOT_FOUND

    @property
    def unavailable(self) -> bool:
        return self.status == STATUS_UNAVAILABLE


class CRAPayrollUnavailable(Exception):
    """Raised internally when the CRA Adapter cannot be reached / times out."""


class CRAPayrollClient:
    """Async client that checks a SIN against an employer's CRA payroll.

    The transport is injectable for testing. When no transport is supplied a
    default ``httpx.AsyncClient``-backed transport is built lazily using the
    5-second timeout.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = DEFAULT_CRA_TIMEOUT_SECONDS,
        transport: Optional[Transport] = None,
        bearer_token: Optional[str] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        # Internal GCKey Bearer presented to the auth-gated CRA Adapter
        # (Req 19.7). ``None`` (unit tests) => no Authorization header. Ignored
        # when an explicit ``transport`` is injected (tests supply their own).
        self._transport = transport or _build_default_transport(bearer_token)

    async def check_sin_in_payroll(
        self,
        sin_hash: str,
        employer_bn: str,
        employment_start: str,
        employment_end: str,
    ) -> PayrollLookup:
        """Return whether ``sin_hash`` appears in the employer's CRA payroll.

        Maps the CRA Adapter response to a :class:`PayrollLookup`:
          * SIN present in payroll for the period -> :data:`STATUS_FOUND`
          * SIN confirmed absent                   -> :data:`STATUS_NOT_FOUND`
          * adapter timeout / 5xx / UNAVAILABLE    -> :data:`STATUS_UNAVAILABLE`
        """
        url = (
            f"{self._base_url}/cra/payroll/{sin_hash}/{employer_bn}"
            f"?start={employment_start}&end={employment_end}"
        )
        try:
            body = await self._transport(url, self._timeout_seconds)
        except CRAPayrollUnavailable:
            logger.info("cra_payroll_unavailable", extra={"reason": "transport_error"})
            return PayrollLookup(status=STATUS_UNAVAILABLE)
        except Exception as exc:  # noqa: BLE001 - any transport failure => unavailable
            logger.info("cra_payroll_unavailable", extra={"reason": repr(exc)})
            return PayrollLookup(status=STATUS_UNAVAILABLE)

        return _interpret(body)


def _interpret(body: Any) -> PayrollLookup:
    """Map a CRA Adapter JSON body to a :class:`PayrollLookup`."""
    if not isinstance(body, dict):
        return PayrollLookup(status=STATUS_UNAVAILABLE)

    status = str(body.get("status", "OK")).upper()
    if status in {"UNAVAILABLE", "ERROR", "TIMEOUT"}:
        return PayrollLookup(status=STATUS_UNAVAILABLE)

    # Preferred explicit boolean; fall back to a nested record / status word.
    in_payroll = body.get("sin_in_payroll")
    if in_payroll is None:
        in_payroll = body.get("found")
    if in_payroll is None and status in {"FOUND", "PRESENT"}:
        in_payroll = True
    if in_payroll is None and status in {"NOT_FOUND", "ABSENT"}:
        in_payroll = False

    if in_payroll is True:
        return PayrollLookup(status=STATUS_FOUND)
    if in_payroll is False:
        return PayrollLookup(status=STATUS_NOT_FOUND)

    # Ambiguous body -> treat as unavailable so we never false-flag CRITICAL.
    return PayrollLookup(status=STATUS_UNAVAILABLE)


def check_payroll_cross_reference(
    lookup: PayrollLookup,
    *,
    sin_hash: Optional[str] = None,
    employer_bn: Optional[str] = None,
    claim_id: Optional[str] = None,
) -> list[Indicator]:
    """Flag a SIN that is confirmed absent from the employer's CRA payroll.

    Produces exactly one CRITICAL Indicator when the lookup positively confirms
    the SIN is NOT in the employer's payroll for the employment period
    (Requirement 7.7).

    Returns ``[]`` when the SIN is found, or when the CRA Adapter was
    unavailable (an unavailable adapter is not evidence of absence, so no
    CRITICAL Indicator is produced — the pipeline degrades gracefully).
    """
    if not lookup.not_found:
        return []

    indicator = Indicator(
        indicator_id=uuid.uuid4(),
        claim_id=_coerce_uuid(claim_id),
        engine=EngineType.DOCUMENT_FORGERY,
        severity=Severity.CRITICAL,
        algorithm="payroll_cross_reference",
        title="ROE employee SIN not found in employer CRA payroll",
        explanation_en=(
            "The employee identified by SIN on the submitted ROE does not "
            "appear in the employer's payroll records returned by the CRA "
            "Adapter for the declared employment period, indicating the "
            "employment relationship may be fabricated."
        ),
        explanation_fr=(
            "L'employe identifie par le NAS sur le releve d'emploi soumis "
            "n'apparait pas dans les dossiers de paie de l'employeur retournes "
            "par l'adaptateur de l'ARC pour la periode d'emploi declaree, ce "
            "qui indique que la relation d'emploi pourrait etre fabriquee."
        ),
        payload={
            "flag": "SIN_NOT_IN_PAYROLL",
            "sin_hash": sin_hash,
            "employer_bn": employer_bn,
        },
        produced_at=datetime.now(timezone.utc),
    )
    return [indicator]


def _build_default_transport(bearer_token: Optional[str] = None) -> Transport:
    """Build the default ``httpx.AsyncClient``-backed async transport.

    When ``bearer_token`` is set, an ``Authorization: Bearer <token>`` header is
    presented on every request. The CRA Adapter validates a GCKey Bearer on
    every endpoint (Req 19.7 / design "Token validation"), so the engine
    authenticates with its own service credential; absent a token (unit tests)
    no header is sent.
    """
    headers = (
        {"Authorization": f"Bearer {bearer_token}"} if bearer_token else None
    )

    async def _transport(url: str, timeout_seconds: float) -> dict[str, Any]:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - httpx is a pinned dep
            raise CRAPayrollUnavailable("httpx unavailable") from exc

        try:
            async with httpx.AsyncClient(timeout=timeout_seconds, headers=headers) as http:
                response = await http.get(url)
                response.raise_for_status()
                return response.json()
        except Exception as exc:  # noqa: BLE001 - map all failures to unavailable
            raise CRAPayrollUnavailable(str(exc)) from exc

    return _transport


def _coerce_uuid(claim_id: Optional[str]) -> uuid.UUID:
    """Coerce a claim id string to a UUID, tolerating missing / non-UUID ids."""
    if isinstance(claim_id, uuid.UUID):
        return claim_id
    if claim_id:
        try:
            return uuid.UUID(str(claim_id))
        except (ValueError, AttributeError, TypeError):
            return uuid.uuid5(uuid.NAMESPACE_OID, str(claim_id))
    return uuid.uuid4()


__all__ = [
    "Transport",
    "STATUS_FOUND",
    "STATUS_NOT_FOUND",
    "STATUS_UNAVAILABLE",
    "DEFAULT_CRA_TIMEOUT_SECONDS",
    "PayrollLookup",
    "CRAPayrollClient",
    "CRAPayrollUnavailable",
    "check_payroll_cross_reference",
]
