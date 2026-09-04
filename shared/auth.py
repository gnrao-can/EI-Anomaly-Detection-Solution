"""
shared/auth.py
==============
GCKey OAuth 2.0 / OIDC token validation and Role-Based Access Control (RBAC)
helpers for all EI-ADS FastAPI services.

This module provides:
  - ``Role`` — the four EI-ADS RBAC roles (CLAIMANT, REVIEWER, INVESTIGATOR,
    ADMINISTRATOR).
  - ``validate_gckey_token(token)`` — validates a GCKey-issued JWT against the
    GCKey JWKS endpoint using ``python-jose``.  Returns the decoded claims on
    success, or raises an ``HTTPException`` with status 401 on any failure.
  - ``get_current_user`` — a FastAPI dependency that extracts the Bearer token
    from the ``Authorization`` header and returns the validated claims.
  - ``require_role(*roles)`` — a FastAPI dependency factory that returns 403
    when the authenticated principal lacks any of the required roles.

Security model (Design → Security Architecture → GCKey OAuth 2.0 Flow):
  - Every API endpoint validates the Bearer token via the GCKey JWKS endpoint.
  - 401 is returned for a missing/invalid/expired token (Req 19.7).
  - 403 is returned for a valid token with insufficient scope/role (Req 19.7, 22.6).

The JWKS URL is configurable via the ``GCKEY_JWKS_URL`` environment variable and
the fetched keys are cached in-process (with a TTL) to avoid a network round-trip
on every request.

Requirements: 1.1, 19.7, 22.6
"""

from __future__ import annotations

import os
import threading
import time
from enum import Enum
from typing import Any, Optional

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import jwt
from jose.exceptions import ExpiredSignatureError, JWTError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Default GCKey JWKS endpoint; overridable via the GCKEY_JWKS_URL env var.
DEFAULT_GCKEY_JWKS_URL = "https://auth.id.gc.ca/.well-known/jwks.json"

# How long (seconds) a fetched JWKS document is cached before a refresh.
_JWKS_CACHE_TTL_SECONDS = int(os.environ.get("GCKEY_JWKS_CACHE_TTL", "3600"))

# Expected audience / issuer (optional — only enforced when configured).
_GCKEY_AUDIENCE = os.environ.get("GCKEY_CLIENT_ID") or os.environ.get("GCKEY_AUDIENCE")
_GCKEY_ISSUER = os.environ.get("GCKEY_ISSUER")


def _jwks_url() -> str:
    """Return the configured GCKey JWKS URL (read fresh so tests can patch env)."""
    return os.environ.get("GCKEY_JWKS_URL", DEFAULT_GCKEY_JWKS_URL)


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


class Role(str, Enum):
    """EI-ADS RBAC roles mapped from GCKey OAuth 2.0 scopes/claims.

    See Design → Security Architecture → RBAC Role Model.
    """

    CLAIMANT = "CLAIMANT"
    REVIEWER = "REVIEWER"
    INVESTIGATOR = "INVESTIGATOR"
    ADMINISTRATOR = "ADMINISTRATOR"


# ---------------------------------------------------------------------------
# JWKS cache
# ---------------------------------------------------------------------------


class _JWKSCache:
    """Thread-safe, TTL-based in-process cache of the GCKey JWKS document."""

    def __init__(self, ttl_seconds: int = _JWKS_CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._keys: Optional[dict[str, Any]] = None
        self._fetched_at: float = 0.0
        self._lock = threading.Lock()

    def clear(self) -> None:
        """Discard any cached keys (primarily used by tests)."""
        with self._lock:
            self._keys = None
            self._fetched_at = 0.0

    def _is_fresh(self) -> bool:
        return (
            self._keys is not None
            and (time.monotonic() - self._fetched_at) < self._ttl
        )

    def get_keys(self, *, force_refresh: bool = False) -> dict[str, Any]:
        """Return the JWKS document, fetching it from the GCKey endpoint if stale.

        Raises
        ------
        HTTPException
            With status 401 when the JWKS document cannot be retrieved.
        """
        with self._lock:
            if not force_refresh and self._is_fresh():
                return self._keys  # type: ignore[return-value]

            url = _jwks_url()
            try:
                response = httpx.get(url, timeout=10.0)
                response.raise_for_status()
                keys = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                # If we have *any* previously cached keys, fall back to them
                # rather than failing hard on a transient network blip.
                if self._keys is not None:
                    return self._keys
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Unable to retrieve GCKey signing keys.",
                ) from exc

            self._keys = keys
            self._fetched_at = time.monotonic()
            return keys


# Module-level singleton cache.
_jwks_cache = _JWKSCache()


def clear_jwks_cache() -> None:
    """Clear the module-level JWKS cache (test helper)."""
    _jwks_cache.clear()


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------


def _find_signing_key(token: str, jwks: dict[str, Any]) -> dict[str, Any]:
    """Return the JWK whose ``kid`` matches the token header.

    Raises
    ------
    HTTPException
        401 when the token header is unreadable or no matching key exists.
    """
    try:
        unverified_header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed authentication token.",
        ) from exc

    kid = unverified_header.get("kid")
    keys = jwks.get("keys", []) if isinstance(jwks, dict) else []

    for key in keys:
        # If the token carries a kid, match it; otherwise fall back to the
        # first available key (common for single-key JWKS documents).
        if kid is None or key.get("kid") == kid:
            return key

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="No matching GCKey signing key for token.",
    )


def validate_gckey_token(token: str) -> dict[str, Any]:
    """Validate a GCKey-issued JWT and return its decoded claims.

    The token signature is verified against the GCKey JWKS endpoint. Expiry,
    audience, and issuer are validated when the corresponding configuration is
    present.

    Parameters
    ----------
    token:
        The raw JWT string (without the ``Bearer`` prefix).

    Returns
    -------
    dict
        The decoded and verified JWT claims.

    Raises
    ------
    HTTPException
        401 for any missing, malformed, expired, or otherwise invalid token.
    """
    if not token or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token.",
        )

    jwks = _jwks_cache.get_keys()
    signing_key = _find_signing_key(token, jwks)

    # Build decode options. Only enforce audience when one is configured.
    options = {
        "verify_aud": _GCKEY_AUDIENCE is not None,
    }

    algorithm = jwt.get_unverified_header(token).get("alg", "RS256")

    try:
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=[algorithm],
            audience=_GCKEY_AUDIENCE,
            issuer=_GCKEY_ISSUER,
            options=options,
        )
    except ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication token has expired.",
        ) from exc
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token.",
        ) from exc

    return claims


# ---------------------------------------------------------------------------
# Role extraction
# ---------------------------------------------------------------------------


def extract_roles(claims: dict[str, Any]) -> set[Role]:
    """Extract the set of EI-ADS ``Role`` values encoded in the token claims.

    GCKey may express EI-ADS authorisation via any of several conventional
    claim shapes:
      - ``roles``  — a list (or space-delimited string) of role names.
      - ``scope``  — an OAuth 2.0 space-delimited scope string.
      - ``scopes`` — a list of scope strings.

    Role/scope names are matched case-insensitively against the ``Role`` enum;
    a scope such as ``ei-ads:investigator`` matches ``INVESTIGATOR`` by its
    trailing segment. Unknown values are ignored.
    """
    raw_values: list[str] = []

    roles_claim = claims.get("roles")
    if isinstance(roles_claim, str):
        raw_values.extend(roles_claim.split())
    elif isinstance(roles_claim, (list, tuple)):
        raw_values.extend(str(r) for r in roles_claim)

    scope_claim = claims.get("scope")
    if isinstance(scope_claim, str):
        raw_values.extend(scope_claim.split())

    scopes_claim = claims.get("scopes")
    if isinstance(scopes_claim, (list, tuple)):
        raw_values.extend(str(s) for s in scopes_claim)

    resolved: set[Role] = set()
    known = {r.value.upper(): r for r in Role}
    for value in raw_values:
        # Take the last segment after ':' or '/' so that scopes like
        # "ei-ads:investigator" resolve to the INVESTIGATOR role.
        candidate = value.replace("/", ":").split(":")[-1].strip().upper()
        role = known.get(candidate)
        if role is not None:
            resolved.add(role)

    return resolved


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

# ``auto_error=False`` so we can raise our own 401 with a consistent message.
_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> dict[str, Any]:
    """FastAPI dependency returning the validated claims for the current request.

    Raises
    ------
    HTTPException
        401 when the ``Authorization: Bearer <token>`` header is absent or the
        token fails validation.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return validate_gckey_token(credentials.credentials)


def require_role(*roles: Role):
    """Return a FastAPI dependency that enforces at least one of ``roles``.

    The returned dependency validates the caller's token (via
    ``get_current_user``) and then checks that the caller holds at least one of
    the required roles. ADMINISTRATOR is always permitted.

    Returns 403 when the caller is authenticated but lacks the required role.

    Example
    -------
    ::

        @app.post("/api/v1/claims")
        async def create_claim(user=Depends(require_role(Role.CLAIMANT))):
            ...
    """
    required = set(roles)

    async def _dependency(
        user: dict[str, Any] = Depends(get_current_user),
    ) -> dict[str, Any]:
        held = extract_roles(user)
        # ADMINISTRATOR is a superuser role permitted everywhere.
        if Role.ADMINISTRATOR in held:
            return user
        if required & held:
            return user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient role for this operation.",
        )

    return _dependency


__all__ = [
    "Role",
    "validate_gckey_token",
    "get_current_user",
    "require_role",
    "extract_roles",
    "clear_jwks_cache",
    "DEFAULT_GCKEY_JWKS_URL",
]
