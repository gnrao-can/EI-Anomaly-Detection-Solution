"""
tests/unit/test_auth.py
=======================
Unit tests for shared/auth.py — GCKey JWKS token validation and RBAC.

These tests generate an in-memory RSA keypair, publish the public half as a
JWKS document, and mock the JWKS network fetch so no external calls are made.
They then mint JWTs signed with the private key and assert that:
  - Valid tokens decode successfully.
  - Missing / malformed / expired / wrong-signature tokens raise 401.
  - Each RBAC role is correctly allowed or denied by ``require_role``.

Requirements: 1.1, 19.7, 22.6
"""

from __future__ import annotations

import time

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jose import jwk, jwt
from jose.utils import long_to_base64

import shared.auth as auth
from shared.auth import (
    Role,
    extract_roles,
    require_role,
    validate_gckey_token,
)

KID = "test-key-1"
ALG = "RS256"


# ---------------------------------------------------------------------------
# Key / JWKS fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rsa_keypair():
    """Generate an RSA keypair and expose PEM private key + public numbers."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    from cryptography.hazmat.primitives import serialization

    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    return private_key, pem


@pytest.fixture(scope="module")
def jwks_document(rsa_keypair):
    """Build a JWKS document containing the public key."""
    private_key, _pem = rsa_keypair
    public_numbers = private_key.public_key().public_numbers()
    n = long_to_base64(public_numbers.n).decode("ascii")
    e = long_to_base64(public_numbers.e).decode("ascii")
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": KID,
                "use": "sig",
                "alg": ALG,
                "n": n,
                "e": e,
            }
        ]
    }


@pytest.fixture(autouse=True)
def _mock_jwks(monkeypatch, jwks_document):
    """Mock the JWKS fetch so validate_gckey_token never hits the network."""
    auth.clear_jwks_cache()

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return jwks_document

    def _fake_get(url, timeout=10.0):  # noqa: ANN001
        return _Resp()

    monkeypatch.setattr(auth.httpx, "get", _fake_get)
    # No configured audience/issuer for these tests.
    monkeypatch.setattr(auth, "_GCKEY_AUDIENCE", None)
    monkeypatch.setattr(auth, "_GCKEY_ISSUER", None)
    yield
    auth.clear_jwks_cache()


def _make_token(rsa_keypair, claims: dict, *, kid: str = KID) -> str:
    """Sign a JWT with the test private key."""
    _private_key, pem = rsa_keypair
    return jwt.encode(claims, pem, algorithm=ALG, headers={"kid": kid})


def _base_claims(**extra) -> dict:
    now = int(time.time())
    claims = {
        "sub": "user-123",
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(extra)
    return claims


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------


class TestValidateGckeyToken:
    def test_valid_token_returns_claims(self, rsa_keypair):
        token = _make_token(rsa_keypair, _base_claims(roles=["CLAIMANT"]))
        claims = validate_gckey_token(token)
        assert claims["sub"] == "user-123"
        assert claims["roles"] == ["CLAIMANT"]

    def test_empty_token_raises_401(self):
        with pytest.raises(HTTPException) as exc:
            validate_gckey_token("")
        assert exc.value.status_code == 401

    def test_whitespace_token_raises_401(self):
        with pytest.raises(HTTPException) as exc:
            validate_gckey_token("   ")
        assert exc.value.status_code == 401

    def test_malformed_token_raises_401(self):
        with pytest.raises(HTTPException) as exc:
            validate_gckey_token("not-a-jwt")
        assert exc.value.status_code == 401

    def test_expired_token_raises_401(self, rsa_keypair):
        now = int(time.time())
        token = _make_token(
            rsa_keypair,
            {"sub": "user-123", "iat": now - 7200, "exp": now - 3600, "roles": ["CLAIMANT"]},
        )
        with pytest.raises(HTTPException) as exc:
            validate_gckey_token(token)
        assert exc.value.status_code == 401

    def test_unknown_kid_raises_401(self, rsa_keypair):
        token = _make_token(rsa_keypair, _base_claims(roles=["CLAIMANT"]), kid="other-kid")
        with pytest.raises(HTTPException) as exc:
            validate_gckey_token(token)
        assert exc.value.status_code == 401

    def test_wrong_signature_raises_401(self, jwks_document):
        # Sign with a *different* private key but reuse the advertised kid.
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        from cryptography.hazmat.primitives import serialization

        other_pem = other.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        now = int(time.time())
        token = jwt.encode(
            {"sub": "x", "exp": now + 3600},
            other_pem,
            algorithm=ALG,
            headers={"kid": KID},
        )
        with pytest.raises(HTTPException) as exc:
            validate_gckey_token(token)
        assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# Role extraction
# ---------------------------------------------------------------------------


class TestExtractRoles:
    def test_roles_list(self):
        assert extract_roles({"roles": ["INVESTIGATOR"]}) == {Role.INVESTIGATOR}

    def test_roles_space_string(self):
        assert extract_roles({"roles": "CLAIMANT REVIEWER"}) == {
            Role.CLAIMANT,
            Role.REVIEWER,
        }

    def test_scope_string(self):
        assert extract_roles({"scope": "openid ei-ads:investigator"}) == {
            Role.INVESTIGATOR
        }

    def test_scopes_list_prefixed(self):
        assert extract_roles({"scopes": ["ei-ads/administrator"]}) == {
            Role.ADMINISTRATOR
        }

    def test_unknown_values_ignored(self):
        assert extract_roles({"roles": ["SOMETHING_ELSE"]}) == set()

    def test_case_insensitive(self):
        assert extract_roles({"roles": ["claimant"]}) == {Role.CLAIMANT}


# ---------------------------------------------------------------------------
# require_role dependency
# ---------------------------------------------------------------------------


async def _invoke(dependency, user):
    # The dependency signature is _dependency(user=Depends(get_current_user)).
    # We bypass Depends by passing the resolved user directly.
    return await dependency(user=user)


class TestRequireRole:
    @pytest.mark.parametrize("role", list(Role))
    async def test_matching_role_allowed(self, role):
        user = await _invoke(require_role(role), {"roles": [role.value]})
        assert user["roles"] == [role.value]

    async def test_missing_role_denied(self):
        with pytest.raises(HTTPException) as exc:
            await _invoke(require_role(Role.INVESTIGATOR), {"roles": ["CLAIMANT"]})
        assert exc.value.status_code == 403

    async def test_reviewer_denied_investigator_route(self):
        with pytest.raises(HTTPException) as exc:
            await _invoke(require_role(Role.INVESTIGATOR), {"roles": ["REVIEWER"]})
        assert exc.value.status_code == 403

    async def test_administrator_allowed_everywhere(self):
        # ADMINISTRATOR is a superuser: allowed even on a CLAIMANT-only route.
        user = await _invoke(require_role(Role.CLAIMANT), {"roles": ["ADMINISTRATOR"]})
        assert user["roles"] == ["ADMINISTRATOR"]

    async def test_any_of_multiple_roles_allowed(self):
        user = await _invoke(
            require_role(Role.REVIEWER, Role.INVESTIGATOR),
            {"roles": ["REVIEWER"]},
        )
        assert user["roles"] == ["REVIEWER"]

    async def test_no_roles_denied(self):
        with pytest.raises(HTTPException) as exc:
            await _invoke(require_role(Role.CLAIMANT), {"roles": []})
        assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Claim Intake app wiring (HTTP-level) — verifies require_role integration and
# the /healthz endpoint. Skipped if the service module cannot be imported.
# ---------------------------------------------------------------------------

import importlib.util
import os
import sys

import pytest

_SERVICE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "services", "claim-intake")
)


def _load_claim_intake_main():
    """Load the claim-intake ``main`` module under a unique name.

    The service modules import their sibling ``config`` by bare name, so the
    service directory must be importable while the module is loaded. We register
    it under a distinct module name (``claim_intake_main``) to avoid clobbering
    the generic ``main`` module name that other service test files also use.
    """
    added = False
    if os.path.isdir(_SERVICE_DIR) and _SERVICE_DIR not in sys.path:
        sys.path.insert(0, _SERVICE_DIR)
        added = True
    try:
        spec = importlib.util.spec_from_file_location(
            "claim_intake_main", os.path.join(_SERVICE_DIR, "main.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module
    finally:
        # Drop transient bare module names created during exec so sibling
        # service test files resolve their own ``main`` / ``config`` modules.
        sys.modules.pop("main", None)
        sys.modules.pop("config", None)
        if added and _SERVICE_DIR in sys.path:
            sys.path.remove(_SERVICE_DIR)


try:
    from fastapi.testclient import TestClient

    claim_intake_main = _load_claim_intake_main()
    _HAS_CLAIM_INTAKE = True
except Exception:  # noqa: BLE001 - service deps may be unavailable
    _HAS_CLAIM_INTAKE = False


_VALID_PAYLOAD = {
    "sin": "130692544",
    "employer_bn": "123456789RT0001",
    "insurable_hours": 800,
    "insurable_earnings": 25000,
    "separation_date": "2024-01-01",
    "separation_reason": "Shortage of work",
    "postal_code": "K1A 0A9",
}


@pytest.mark.skipif(not _HAS_CLAIM_INTAKE, reason="claim-intake service not importable")
class TestClaimIntakeAppWiring:
    def _client(self):
        return TestClient(claim_intake_main.app)

    def teardown_method(self):
        if _HAS_CLAIM_INTAKE:
            claim_intake_main.app.dependency_overrides.clear()

    def test_post_claims_without_token_returns_401(self):
        resp = self._client().post("/api/v1/claims", json=_VALID_PAYLOAD)
        assert resp.status_code == 401

    def test_post_claims_as_claimant_returns_202(self):
        from shared.auth import get_current_user

        claim_intake_main.app.dependency_overrides[get_current_user] = (
            lambda: {"roles": ["CLAIMANT"]}
        )
        resp = self._client().post("/api/v1/claims", json=_VALID_PAYLOAD)
        assert resp.status_code == 202
        assert "claim_id" in resp.json()

    def test_post_claims_as_reviewer_returns_403(self):
        from shared.auth import get_current_user

        claim_intake_main.app.dependency_overrides[get_current_user] = (
            lambda: {"roles": ["REVIEWER"]}
        )
        resp = self._client().post("/api/v1/claims", json=_VALID_PAYLOAD)
        assert resp.status_code == 403

    def test_healthz_returns_503_when_dependencies_absent(self):
        # No DB/Kafka/Redis connected in the unit-test context.
        resp = self._client().get("/healthz")
        assert resp.status_code == 503
        body = resp.json()
        assert body["checks"] == {"database": False, "kafka": False, "redis": False}
