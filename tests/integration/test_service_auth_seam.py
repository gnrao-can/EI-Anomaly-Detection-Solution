"""
tests/integration/test_service_auth_seam.py
============================================
Proves the Option A test-auth seam: a token minted by ``itest_service_auth``
passes the REAL ``shared.auth`` validation path (JWKS fetched over HTTP,
RS256 signature verified) and satisfies ``require_role(Role.INVESTIGATOR)`` —
the exact gate the engines and Ensemble Scorer put on their internal endpoints.

This is the unblocker for the Slice 1 real-transport integration test: it
confirms the orchestrator can present a Bearer token that the callee services
actually accept, and that the auth boundary still rejects missing tokens (401)
and insufficient roles (403).

The endpoints below are driven over a real ASGI HTTP transport
(``httpx.ASGITransport``) so the ``HTTPBearer`` extraction, JWKS fetch, and
signature verification all run for real — no monkeypatched auth.

Requirements exercised: 19.7 (Bearer validation on every endpoint),
22.6 (role enforcement).
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import Depends, FastAPI

from shared.auth import Role, require_role


def _build_app() -> FastAPI:
    """A minimal app whose endpoint is gated exactly like the engines/scorer."""
    app = FastAPI()

    @app.post("/engine/analyze")
    async def analyze(_user: dict = Depends(require_role(Role.INVESTIGATOR))):
        return {"ok": True, "sub": _user.get("sub")}

    return app


async def _post(app: FastAPI, headers: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://itest"
    ) as client:
        return await client.post("/engine/analyze", headers=headers or {}, json={})


@pytest.mark.asyncio
async def test_minted_investigator_token_is_accepted(itest_service_auth):
    """A minted INVESTIGATOR token passes real JWKS/RS256 validation -> 200."""
    token = itest_service_auth.mint_token(roles=["INVESTIGATOR"])
    resp = await _post(_build_app(), {"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True


@pytest.mark.asyncio
async def test_missing_bearer_is_rejected_401(itest_service_auth):
    """No Authorization header -> 401 (the auth boundary still holds)."""
    resp = await _post(_build_app())
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_wrong_role_is_rejected_403(itest_service_auth):
    """A validly-signed token lacking INVESTIGATOR -> 403 (role enforced)."""
    token = itest_service_auth.mint_token(roles=["CLAIMANT"])
    resp = await _post(_build_app(), {"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_expired_token_is_rejected_401(itest_service_auth):
    """An expired but correctly-signed token -> 401 (exp enforced)."""
    token = itest_service_auth.mint_token(roles=["INVESTIGATOR"], ttl_seconds=-10)
    resp = await _post(_build_app(), {"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_token_signed_by_untrusted_key_is_rejected_401(itest_service_auth):
    """A token signed by a DIFFERENT keypair (same kid) -> 401.

    Guards against the seam accidentally trusting any RS256 token: the JWKS
    served is the session key, so a token from a foreign key must fail signature
    verification even though its ``kid`` matches.
    """
    from tests.integration.service_auth import TestServiceAuth

    foreign = TestServiceAuth.generate(kid=itest_service_auth.kid)
    token = foreign.mint_token(roles=["INVESTIGATOR"])
    resp = await _post(_build_app(), {"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401, resp.text
