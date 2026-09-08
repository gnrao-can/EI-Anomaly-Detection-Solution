"""
tests/integration/service_auth.py
=================================
Test-auth seam for EI-ADS integration tests (Option A: test JWKS + signed JWT).

The internal EI-ADS services (engines, Ensemble Scorer) protect every endpoint
with ``shared.auth.require_role(...)``, which validates a GCKey-issued Bearer
JWT against a JWKS endpoint (``shared.auth.validate_gckey_token``). To exercise
those endpoints over a REAL transport (real HTTP, real ``shared.auth`` code
path) an integration test needs a Bearer token the shared validator actually
trusts — not a bypass.

This module provides exactly that, entirely in test code, changing NO production
behaviour:

  * :class:`TestServiceAuth` — generates an ephemeral RSA keypair once, exposes
    a JWKS document built from the public key (``.jwks``), and mints short-lived
    RS256 JWTs carrying an EI-ADS role (``.mint_token(...)``).

The tokens are faithful to what ``shared.auth`` enforces:

  * signed RS256 with a ``kid`` that matches the JWKS entry, so
    ``_find_signing_key`` resolves it;
  * a valid ``exp`` (validator rejects expired tokens);
  * an ``INVESTIGATOR`` role by default (``roles`` claim), which
    ``extract_roles`` resolves and ``require_role(Role.INVESTIGATOR)`` accepts.

Neither the engines nor the scorer configure ``GCKEY_AUDIENCE`` /
``GCKEY_CLIENT_ID`` / ``GCKEY_ISSUER``, so ``validate_gckey_token`` does NOT
enforce audience/issuer; the token therefore only needs a trusted signature,
a matching ``kid``, a live ``exp``, and the role claim. (If a future service
does set those, ``mint_token`` accepts ``audience`` / ``issuer`` overrides.)

Two ways to point a service at the test JWKS:

  * **In-process** (same interpreter as the service app, e.g. ASGI transport):
    use :func:`installed_service_auth` / the ``itest_service_auth`` fixture,
    which sets ``GCKEY_JWKS_URL`` to a ``file://`` URL of the written JWKS and
    clears the shared JWKS cache. ``shared.auth._jwks_url()`` reads the env
    fresh per call and ``httpx.get`` supports ``file://`` via no extra deps only
    when a transport is provided — so we also expose ``.write_jwks(path)`` and
    let callers set ``GCKEY_JWKS_URL`` to an ``http(s)://`` URL served by their
    own fixture (e.g. a container or a local HTTP stub) when a real network
    fetch is required.

  * **Out-of-process** (service started in a container / subprocess): write the
    JWKS to a path or serve it, pass its URL to the service via
    ``GCKEY_JWKS_URL`` in the service's environment, and inject
    ``mint_token()`` as the orchestrator's ``ORCHESTRATOR_SERVICE_TOKEN``.

Requirements exercised: 19.7 (every endpoint validates a GCKey Bearer token),
22.6 (role enforcement).
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

# ``shared.auth`` exposes the JWKS-cache clear helper and the default URL const.
from shared.auth import DEFAULT_GCKEY_JWKS_URL, clear_jwks_cache  # noqa: F401

_TEST_KID = "ei-ads-itest-key"


def _b64url_uint(value: int) -> str:
    """Encode an unsigned integer as a base64url string (no padding), per JWK."""
    import base64

    length = (value.bit_length() + 7) // 8
    data = value.to_bytes(length, "big")
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@dataclass
class TestServiceAuth:
    """Ephemeral RSA signer + JWKS provider for integration tests.

    A single instance owns one keypair for the whole test session. Build it via
    :meth:`generate` (the fixtures below do this for you).
    """

    kid: str
    _private_key: rsa.RSAPrivateKey
    jwks: dict[str, Any] = field(default_factory=dict)

    # -- construction -------------------------------------------------------

    @classmethod
    def generate(cls, *, kid: str = _TEST_KID) -> "TestServiceAuth":
        """Generate a fresh 2048-bit RSA keypair and its single-entry JWKS."""
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        instance = cls(kid=kid, _private_key=private_key)
        instance.jwks = instance._build_jwks()
        return instance

    def _build_jwks(self) -> dict[str, Any]:
        """Return a JWKS document (one RSA public key) matching ``self.kid``."""
        public_numbers = self._private_key.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": "RS256",
                    "kid": self.kid,
                    "n": _b64url_uint(public_numbers.n),
                    "e": _b64url_uint(public_numbers.e),
                }
            ]
        }

    # -- token minting ------------------------------------------------------

    def _private_pem(self) -> bytes:
        return self._private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def mint_token(
        self,
        *,
        roles: Optional[list[str]] = None,
        subject: Optional[str] = None,
        ttl_seconds: int = 900,
        audience: Optional[str] = None,
        issuer: Optional[str] = None,
        extra_claims: Optional[dict[str, Any]] = None,
    ) -> str:
        """Mint a signed RS256 JWT the shared validator will accept.

        Parameters
        ----------
        roles:
            EI-ADS role names placed in the ``roles`` claim. Defaults to
            ``["INVESTIGATOR"]`` — the role the engines and scorer require for
            their internal endpoints.
        subject:
            Optional ``sub`` claim (defaults to a random service principal id).
        ttl_seconds:
            Token lifetime; the validator rejects expired tokens.
        audience / issuer:
            Optional ``aud`` / ``iss`` claims for services that enforce them
            (the current engines/scorer do not).
        extra_claims:
            Additional claims merged into the payload (e.g. ``sin_hash``).
        """
        now = int(time.time())
        claims: dict[str, Any] = {
            "sub": subject or f"svc-{uuid.uuid4()}",
            "iat": now,
            "nbf": now,
            "exp": now + int(ttl_seconds),
            "roles": roles if roles is not None else ["INVESTIGATOR"],
        }
        if audience is not None:
            claims["aud"] = audience
        if issuer is not None:
            claims["iss"] = issuer
        if extra_claims:
            claims.update(extra_claims)

        return jwt.encode(
            claims,
            self._private_pem().decode("ascii"),
            algorithm="RS256",
            headers={"kid": self.kid},
        )

    # -- JWKS publication ---------------------------------------------------

    def write_jwks(self, path: str) -> str:
        """Write the JWKS document to ``path`` and return that path.

        Useful for pointing an out-of-process service at the JWKS via a
        ``file://`` or served ``http://`` URL.
        """
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.jwks, handle)
        return path


@contextlib.contextmanager
def installed_service_auth(
    auth: TestServiceAuth, jwks_path: str
) -> Iterator[TestServiceAuth]:
    """Point ``shared.auth`` at the test JWKS for the duration of the block.

    Writes the JWKS to ``jwks_path``, sets ``GCKEY_JWKS_URL`` to its ``file://``
    URL, clears the shared JWKS cache, and restores the previous environment on
    exit. Suitable for in-process services (ASGI transport) that share this
    interpreter's ``shared.auth`` module.

    Note: ``shared.auth`` fetches the JWKS with ``httpx.get(url)``. ``httpx``
    does not serve ``file://`` URLs by default, so in-process tests that rely on
    a network fetch should instead run a tiny local HTTP file server and set
    ``GCKEY_JWKS_URL`` to its ``http://`` address (see the ``itest_service_auth``
    fixture, which does this). ``jwks_path`` is still written so out-of-process
    services can consume it directly.
    """
    auth.write_jwks(jwks_path)
    previous = os.environ.get("GCKEY_JWKS_URL")
    os.environ["GCKEY_JWKS_URL"] = f"file://{os.path.abspath(jwks_path)}"
    clear_jwks_cache()
    try:
        yield auth
    finally:
        if previous is None:
            os.environ.pop("GCKEY_JWKS_URL", None)
        else:
            os.environ["GCKEY_JWKS_URL"] = previous
        clear_jwks_cache()


__all__ = [
    "TestServiceAuth",
    "installed_service_auth",
    "clear_jwks_cache",
]
