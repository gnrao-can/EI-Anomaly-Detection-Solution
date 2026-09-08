"""
services/portal-backend/auth.py
===============================
Session-cookie authentication for the Portal BFF.

The Portal BFF authenticates via GCKey OAuth 2.0 (authorization-code + PKCE).
The BFF performs the code exchange, validates the ID token with
:func:`shared.auth.validate_gckey_token`, maps the claims to a
:class:`shared.auth.Role` plus ``sin_hash``, and issues a signed HttpOnly
session cookie to the browser. The GCKey access token is carried inside that
signed session so the BFF can forward it as an ``Authorization: Bearer`` header
on downstream Claim Intake calls (design GCKey flow; Req 19.7).

Every ``/portal`` and ``/api`` handler consumes the same :class:`Principal`
derived from the session cookie (Req 19.7, 22.6).

The cookie is signed (not encrypted) with an HMAC-SHA256 keyed by
``SESSION_SECRET`` using only the Python standard library, so no additional
runtime dependency (``itsdangerous`` / ``jose`` for sessions) is introduced.
The payload is a base64url JSON blob; tampering invalidates the signature and
the cookie is rejected as unauthenticated.

Requirements: 19.7, 22.6
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import Depends, HTTPException, Request, status

from shared.auth import Role

try:  # config lives alongside this module in the service directory
    from config import get_settings  # type: ignore
except ImportError:  # pragma: no cover - when imported as a package
    from .config import get_settings  # type: ignore


__all__ = [
    "Principal",
    "sign_session",
    "verify_session",
    "get_principal",
    "get_optional_principal",
    "require_roles",
    "INVESTIGATOR_ROLES",
    "SessionError",
]


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """The authenticated caller derived from the session cookie.

    Attributes
    ----------
    role:
        The caller's EI-ADS :class:`~shared.auth.Role`.
    sin_hash:
        The claimant's ``sin_hash`` (own-data key). ``None`` for staff roles
        (REVIEWER / INVESTIGATOR / ADMINISTRATOR) who are not tied to a SIN.
    access_token:
        The GCKey access token obtained during the OAuth code exchange, carried
        in the signed session so the BFF can forward it as an
        ``Authorization: Bearer`` header on downstream Claim Intake calls
        (design GCKey flow "Forward with Bearer token"; Req 19.7). ``None`` for
        read-only sessions that never call a downstream API.
    """

    role: Role
    sin_hash: Optional[str] = None
    access_token: Optional[str] = None

    @property
    def roles(self) -> set[Role]:
        return {self.role}

    def can_view_unmasked_pii(self) -> bool:
        """INVESTIGATOR and ADMINISTRATOR see unmasked PII (Req 22.6)."""
        return self.role in {Role.INVESTIGATOR, Role.ADMINISTRATOR}


# Roles permitted to use the investigator queue / detail endpoints (Req 22.1).
INVESTIGATOR_ROLES = (Role.REVIEWER, Role.INVESTIGATOR, Role.ADMINISTRATOR)


class SessionError(Exception):
    """Raised internally when a session cookie is missing or invalid."""


# ---------------------------------------------------------------------------
# Cookie signing (stdlib HMAC-SHA256, no extra dependency)
# ---------------------------------------------------------------------------


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def sign_session(
    payload: dict[str, Any],
    secret: str,
    *,
    max_age: Optional[int] = None,
) -> str:
    """Serialise and HMAC-sign a session ``payload`` into a cookie value.

    The value has the form ``<b64url(json)>.<b64url(hmac)>``. An ``iat`` issued-at
    timestamp is stamped so :func:`verify_session` can enforce ``max_age``.
    """
    body = dict(payload)
    body.setdefault("iat", int(time.time()))
    encoded = _b64url_encode(
        json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = hmac.new(
        secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{encoded}.{_b64url_encode(signature)}"


def verify_session(
    value: str,
    secret: str,
    *,
    max_age: Optional[int] = None,
) -> dict[str, Any]:
    """Verify a signed cookie ``value`` and return its decoded payload.

    Raises
    ------
    SessionError
        When the value is malformed, the signature does not match, or the
        payload has aged past ``max_age``.
    """
    if not value or "." not in value:
        raise SessionError("Malformed session cookie.")

    encoded, _, provided_sig = value.partition(".")
    expected = hmac.new(
        secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
    ).digest()
    try:
        provided = _b64url_decode(provided_sig)
    except (ValueError, TypeError) as exc:
        raise SessionError("Malformed session signature.") from exc

    # Constant-time comparison to avoid signature-timing leaks.
    if not hmac.compare_digest(expected, provided):
        raise SessionError("Session signature mismatch.")

    try:
        payload = json.loads(_b64url_decode(encoded).decode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise SessionError("Malformed session payload.") from exc

    if not isinstance(payload, dict):
        raise SessionError("Malformed session payload.")

    if max_age is not None:
        issued_at = payload.get("iat")
        if not isinstance(issued_at, (int, float)):
            raise SessionError("Session missing issued-at timestamp.")
        if (time.time() - float(issued_at)) > max_age:
            raise SessionError("Session has expired.")

    return payload


# ---------------------------------------------------------------------------
# Principal construction
# ---------------------------------------------------------------------------


def _principal_from_payload(payload: dict[str, Any]) -> Principal:
    """Build a :class:`Principal` from a verified session payload."""
    raw_role = payload.get("role")
    try:
        role = Role(str(raw_role).upper())
    except (ValueError, AttributeError) as exc:
        raise SessionError(f"Unknown role in session: {raw_role!r}") from exc

    sin_hash = payload.get("sin_hash")
    if sin_hash is not None and not isinstance(sin_hash, str):
        raise SessionError("Invalid sin_hash in session.")

    access_token = payload.get("access_token")
    if access_token is not None and not isinstance(access_token, str):
        raise SessionError("Invalid access_token in session.")

    return Principal(
        role=role,
        sin_hash=sin_hash or None,
        access_token=access_token or None,
    )


def _read_principal(request: Request) -> Optional[Principal]:
    """Return the :class:`Principal` for *request*, or ``None`` when unauthenticated."""
    settings = get_settings()
    cookie = request.cookies.get(settings.session_cookie_name)
    if not cookie:
        return None
    try:
        payload = verify_session(
            cookie, settings.session_secret, max_age=settings.session_max_age
        )
        return _principal_from_payload(payload)
    except SessionError:
        return None


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


async def get_optional_principal(request: Request) -> Optional[Principal]:
    """FastAPI dependency yielding the :class:`Principal` or ``None``.

    Used by endpoints (e.g. the SSE stream) that want to distinguish an
    anonymous caller without raising.
    """
    return _read_principal(request)


async def get_principal(request: Request) -> Principal:
    """FastAPI dependency yielding the authenticated :class:`Principal`.

    Raises 401 when no valid session cookie is present (Req 19.7).
    """
    principal = _read_principal(request)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
    return principal


def require_roles(*roles: Role):
    """Return a dependency enforcing that the caller holds one of ``roles``.

    ADMINISTRATOR is always permitted (superuser), mirroring
    :func:`shared.auth.require_role`. Returns 403 when the authenticated caller
    lacks the required role (Req 22.6).
    """
    required = set(roles)

    async def _dependency(
        principal: Principal = Depends(get_principal),
    ) -> Principal:
        if principal.role is Role.ADMINISTRATOR:
            return principal
        if principal.role in required:
            return principal
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient role for this operation.",
        )

    return _dependency
