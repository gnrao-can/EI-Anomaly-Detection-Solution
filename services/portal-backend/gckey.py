"""
services/portal-backend/gckey.py
================================
GCKey OAuth 2.0 authorization-code + PKCE helpers for the Portal BFF
(``AUTH_MODE=gckey``).

The browser is redirected to the GCKey ``/authorize`` endpoint with a PKCE
challenge and CSRF ``state``; GCKey redirects back to
``/auth/gckey/callback`` with an authorization ``code`` which the BFF exchanges
for tokens at the token endpoint. The returned ID token is validated with
:func:`shared.auth.validate_gckey_token` (reusing the JWKS validation shared by
all services) and mapped to a :class:`shared.auth.Role` + ``sin_hash``; the BFF
then sets the same signed session cookie used by the dev-auth path.

``httpx`` is guarded so this module imports even where the driver is absent. The
GCKey path does not need to be verifiable without a live IdP, but it must be
importable and wired.

Requirements: 19.7
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from typing import Any, Optional

from shared.auth import Role, extract_roles, validate_gckey_token

try:  # httpx is used only for the token exchange; guard for minimal envs.
    import httpx  # type: ignore

    _HAS_HTTPX = True
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore
    _HAS_HTTPX = False


__all__ = [
    "generate_pkce_pair",
    "generate_state",
    "build_authorize_url",
    "exchange_code_for_tokens",
    "principal_fields_from_id_token",
]


def generate_pkce_pair() -> tuple[str, str]:
    """Return an ``(code_verifier, code_challenge)`` PKCE pair (S256)."""
    verifier = base64.urlsafe_b64encode(os.urandom(64)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def generate_state() -> str:
    """Return a random CSRF ``state`` token."""
    return secrets.token_urlsafe(32)


def build_authorize_url(
    *,
    authorize_url: str,
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str,
    code_challenge: str,
) -> str:
    """Build the GCKey ``/authorize`` redirect URL with a PKCE challenge."""
    from urllib.parse import urlencode

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    separator = "&" if "?" in authorize_url else "?"
    return f"{authorize_url}{separator}{urlencode(params)}"


async def exchange_code_for_tokens(
    *,
    token_url: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
    code_verifier: str,
) -> dict[str, Any]:
    """Exchange an authorization ``code`` for GCKey tokens.

    Raises ``RuntimeError`` when httpx is unavailable or the token endpoint
    returns a non-2xx response.
    """
    if not _HAS_HTTPX:  # pragma: no cover - depends on environment
        raise RuntimeError("httpx is required for the GCKey token exchange.")

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    if client_secret:
        data["client_secret"] = client_secret

    async with httpx.AsyncClient(timeout=10.0) as client:  # pragma: no cover - IdP
        response = await client.post(token_url, data=data)
        response.raise_for_status()
        return response.json()


def principal_fields_from_id_token(id_token: str) -> tuple[Role, Optional[str]]:
    """Validate a GCKey ID token and derive ``(role, sin_hash)``.

    The token signature/expiry/issuer are validated by
    :func:`shared.auth.validate_gckey_token`. The highest-privilege role present
    in the claims is selected; ``sin_hash`` is read from the ``sin_hash`` claim.
    """
    claims = validate_gckey_token(id_token)
    roles = extract_roles(claims)

    # Prefer the most privileged role the token grants.
    for candidate in (
        Role.ADMINISTRATOR,
        Role.INVESTIGATOR,
        Role.REVIEWER,
        Role.CLAIMANT,
    ):
        if candidate in roles:
            role = candidate
            break
    else:
        role = Role.CLAIMANT

    sin_hash = claims.get("sin_hash")
    if not isinstance(sin_hash, str):
        sin_hash = None
    return role, sin_hash
