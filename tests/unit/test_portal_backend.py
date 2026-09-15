"""
tests/unit/test_portal_backend.py
=================================
Unit tests for the Portal Backend-for-Frontend (BFF) at
``services/portal-backend``.

The service lives in a hyphenated directory, so it is loaded via the shared
``load_service_module`` conftest helper (namespaced ``sys.modules`` keys keep the
whole unit suite collision-free).

Auth model: GCKey OAuth 2.0 is the ONLY auth mode (the dev-auth stub was
removed). Tests establish a session two ways:
  * directly minting the signed session cookie via ``sign_session`` (the same
    cookie the GCKey callback issues) for the many tests that only need an
    authenticated principal; and
  * driving ``POST /auth/callback`` with the GCKey token-exchange functions
    faked (``gckey.exchange_code_for_tokens`` + ``principal_fields_from_id_token``)
    to exercise the real callback wiring — including persisting the GCKey access
    token into the session for downstream Bearer-forward.

Coverage:
  * GCKey callback: exchanges the code, sets a session cookie, and the session
    carries the access token; ``/auth/session`` then reports authenticated.
  * Bearer-forward: ``POST /api/v1/claims`` and the Privacy Act proxies forward
    ``Authorization: Bearer <gckey access token>`` to Claim Intake (NOT X-Dev-*).
  * role gates: 403 for the wrong role on investigator/disposition endpoints.
  * PII masking: REVIEWER sees masked name/SIN/address; INVESTIGATOR does not.
  * claimant own-data-only enforcement (Req 19.7).
  * 404 paths for absent claims.
  * disposition -> outcome.feedback publish hook fires ONLY for
    CONFIRMED_FRAUD / FALSE_POSITIVE.
  * SSE emits a pushed flagged claim.
  * JSON response keys EXACTLY match the snake_case keys the SPA parsers read.
  * reverse-proxy endpoints forward to CLAIM_INTAKE_URL (httpx MockTransport).
"""

from __future__ import annotations

import json
import os

import pytest

from tests.conftest import load_service_module

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

# Force local-degrade defaults before the modules read settings.
os.environ.setdefault("SESSION_SECRET", "test_portal_secret")
os.environ.setdefault("CLAIM_INTAKE_URL", "http://claim-intake.test")

_SIBLINGS = [
    "config",
    "auth",
    "repository",
    "sse",
    "consumer",
    "graph_client",
    "gckey",
    "main",
]

_config = load_service_module("portal-backend", "config", siblings=_SIBLINGS)
_auth = load_service_module("portal-backend", "auth", siblings=_SIBLINGS)
_repository = load_service_module("portal-backend", "repository", siblings=_SIBLINGS)
_sse = load_service_module("portal-backend", "sse", siblings=_SIBLINGS)
_consumer = load_service_module("portal-backend", "consumer", siblings=_SIBLINGS)
_graph_client = load_service_module(
    "portal-backend", "graph_client", siblings=_SIBLINGS
)
_gckey = load_service_module("portal-backend", "gckey", siblings=_SIBLINGS)
_main = load_service_module("portal-backend", "main", siblings=_SIBLINGS)

app = _main.app
FakePortalRepository = _repository.FakePortalRepository
sign_session = _auth.sign_session
Role = _auth.Role

SESSION_SECRET = "test_portal_secret"
COOKIE_NAME = "ei_ads_session"
# A representative GCKey access token value the BFF forwards downstream.
TEST_ACCESS_TOKEN = "gckey-access-token-xyz"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo() -> "FakePortalRepository":
    """A fresh in-memory repository wired into the app for each test."""
    repository = FakePortalRepository()
    app.state.repository = repository
    # Ensure a broadcaster exists and settings resolve without a live backend.
    app.state.broadcaster = _sse.ClaimEventBroadcaster()
    app.state.kafka_producer = None
    app.state.settings = _config.get_settings()
    return repository


@pytest.fixture
def client(repo):
    with TestClient(app) as c:
        # TestClient runs the lifespan, which may reset app.state.repository to
        # a fresh fake (DB absent). Re-inject our seeded repository afterwards.
        app.state.repository = repo
        app.state.kafka_producer = None
        yield c


def _session_cookie(role: str, sin_hash=None, access_token=TEST_ACCESS_TOKEN) -> dict:
    """Mint the signed session cookie the GCKey callback would issue.

    Carries the GCKey ``access_token`` so downstream Bearer-forward paths have a
    token to attach; pass ``access_token=None`` to simulate a token-less session.
    """
    payload = {"role": role, "sin_hash": sin_hash}
    if access_token:
        payload["access_token"] = access_token
    token = sign_session(payload, SESSION_SECRET, max_age=3600)
    return {COOKIE_NAME: token}


def _seed_flagged(repo, claim_id="c-1", **kw):
    defaults = dict(
        risk_score=88,
        risk_decision="INVESTIGATE",
        explanation_en="High risk.",
        explanation_fr="Risque élevé.",
        shap_base_value=50.0,
        shap_attributions=[
            {"indicator_id": "i-1", "feature_name": "hours_spike", "contribution": 12.0}
        ],
        indicators=[
            {
                "indicator_id": "i-1",
                "engine": "REPETITIVE_PATTERN",
                "severity": "HIGH",
                "title": "Repeated pattern",
                "explanation_en": "en",
                "explanation_fr": "fr",
                "shap_contribution": 12.0,
            }
        ],
    )
    defaults.update(kw)
    repo.add_claim(claim_id, **defaults)


def _fake_gckey(monkeypatch, role="CLAIMANT", sin_hash=None, access_token=TEST_ACCESS_TOKEN):
    """Fake the GCKey token exchange + ID-token validation on the loaded module.

    Lets ``POST /auth/callback`` run the REAL callback wiring (code -> tokens ->
    principal -> session cookie -> access token persisted) without a live IdP or
    JWKS. Returns nothing; patches the ``gckey`` module the app imported.
    """
    async def _fake_exchange(**kwargs):
        return {"access_token": access_token, "id_token": "fake.id.token"}

    def _fake_principal_fields(id_token):
        # Mirrors gckey.principal_fields_from_id_token -> (role, sin_hash, subject).
        subject = f"gckey-sub-{role.lower()}"
        return (Role(role.upper()), sin_hash, subject)

    monkeypatch.setattr(_gckey, "exchange_code_for_tokens", _fake_exchange)
    monkeypatch.setattr(_gckey, "principal_fields_from_id_token", _fake_principal_fields)


# ---------------------------------------------------------------------------
# GCKey auth callback + session
# ---------------------------------------------------------------------------


class TestGCKeyAuth:
    def test_callback_exchanges_code_and_sets_session(self, client, monkeypatch):
        _fake_gckey(monkeypatch, role="INVESTIGATOR")
        client.cookies.clear()
        resp = client.post(
            "/auth/callback",
            json={"code": "abc", "code_verifier": "v", "redirect_uri": "r"},
        )
        assert resp.status_code == 200
        assert COOKIE_NAME in resp.cookies
        # The SPA then reads the session; it must be authenticated.
        session = client.get("/auth/session")
        assert session.status_code == 200
        assert session.json()["roles"] == ["INVESTIGATOR"]

    def test_callback_persists_access_token_for_bearer_forward(self, client, monkeypatch):
        # The session established by the callback must carry the GCKey access
        # token so downstream calls can forward it as a Bearer.
        _fake_gckey(monkeypatch, role="CLAIMANT", sin_hash="sinhash-abc",
                    access_token="tok-from-idp")
        client.cookies.clear()
        client.post(
            "/auth/callback",
            json={"code": "abc", "code_verifier": "v", "redirect_uri": "r"},
        )
        # Forward a claim submission and assert the Bearer is the IdP token.
        capture: dict = {}
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            capture["authorization"] = request.headers.get("authorization")
            return httpx.Response(202, json={"claim_id": "x", "status": "RECEIVED"})

        app.state.proxy_transport = httpx.MockTransport(handler)
        resp = client.post("/api/v1/claims", data={
            "sin": "046454286", "employer_bn": "123456789RT0001",
            "insurable_hours": "1200", "insurable_earnings": "45000",
            "separation_date": "2024-01-15", "separation_reason": "Shortage of work",
        })
        assert resp.status_code == 202
        assert capture["authorization"] == "Bearer tok-from-idp"

    def test_protected_route_requires_cookie(self, client):
        # No cookie -> 401.
        client.cookies.clear()
        resp = client.get("/portal/investigator/claims")
        assert resp.status_code == 401

    def test_protected_route_works_with_cookie(self, client, repo):
        _seed_flagged(repo)
        resp = client.get(
            "/portal/investigator/claims", cookies=_session_cookie("INVESTIGATOR")
        )
        assert resp.status_code == 200

    def test_logout_clears_cookie(self, client):
        resp = client.get("/auth/logout")
        assert resp.status_code == 200
        assert resp.json()["status"] == "logged_out"

    def test_me_reflects_session(self, client):
        resp = client.get("/auth/me", cookies=_session_cookie("REVIEWER", "abc"))
        assert resp.status_code == 200
        assert resp.json() == {"role": "REVIEWER", "sin_hash": "abc"}


# ---------------------------------------------------------------------------
# SPA session contract — GET /auth/session, POST /auth/logout, POST /auth/callback
# (services/claimant-portal/src/auth/gckey.ts + roles.ts)
# ---------------------------------------------------------------------------


class TestSessionContract:
    # Exact JSON keys the SPA's parseSession reads.
    SESSION_KEYS = {"subject", "roles", "preferredLanguage", "displayName"}
    # The role strings the SPA's roles.ts isRole() accepts.
    SPA_ROLES = {"CLAIMANT", "REVIEWER", "INVESTIGATOR", "ADMINISTRATOR"}

    def test_session_with_valid_cookie_returns_contract(self, client):
        resp = client.get(
            "/auth/session", cookies=_session_cookie("INVESTIGATOR")
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == self.SESSION_KEYS
        # subject must be a non-empty string so the SPA accepts the session.
        assert isinstance(body["subject"], str) and body["subject"]
        assert body["roles"] == ["INVESTIGATOR"]
        # roles strings must be exactly what the SPA's isRole() accepts.
        assert set(body["roles"]).issubset(self.SPA_ROLES)
        assert body["preferredLanguage"] == "en"

    def test_session_subject_uses_sin_hash_when_present(self, client):
        resp = client.get(
            "/auth/session", cookies=_session_cookie("CLAIMANT", "sinhash123")
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["subject"] == "sinhash123"
        assert body["roles"] == ["CLAIMANT"]

    def test_session_without_cookie_returns_401(self, client):
        client.cookies.clear()
        resp = client.get("/auth/session")
        assert resp.status_code == 401

    def test_session_with_invalid_cookie_returns_401(self, client):
        client.cookies.clear()
        resp = client.get("/auth/session", cookies={COOKIE_NAME: "not.a.valid.cookie"})
        assert resp.status_code == 401

    def test_gckey_callback_then_session_is_authenticated(self, client, monkeypatch):
        _fake_gckey(monkeypatch, role="CLAIMANT", sin_hash="abc")
        client.cookies.clear()
        cb = client.post(
            "/auth/callback",
            json={"code": "c", "code_verifier": "v", "redirect_uri": "r"},
        )
        assert cb.status_code == 200
        # The same client carries the minted cookie forward.
        resp = client.get("/auth/session")
        assert resp.status_code == 200
        body = resp.json()
        assert body["roles"] == ["CLAIMANT"]
        assert isinstance(body["subject"], str) and body["subject"]
        assert body["subject"] == "abc"

    def test_post_logout_clears_cookie(self, client, monkeypatch):
        _fake_gckey(monkeypatch, role="CLAIMANT", sin_hash="abc")
        client.cookies.clear()
        client.post(
            "/auth/callback",
            json={"code": "c", "code_verifier": "v", "redirect_uri": "r"},
        )
        assert client.get("/auth/session").status_code == 200
        out = client.post("/auth/logout")
        assert out.status_code == 200
        assert out.json()["status"] == "logged_out"
        # Cookie cleared -> subsequent session read is unauthenticated.
        client.cookies.clear()
        assert client.get("/auth/session").status_code == 401

    def test_authorize_redirects_to_gckey(self, client):
        # GCKey is the only auth mode: /auth/authorize 302s to the GCKey
        # authorize URL (not a dev-login stub) and sets the PKCE state cookie.
        resp = client.get("/auth/authorize", follow_redirects=False)
        assert resp.status_code == 302
        loc = resp.headers["location"]
        assert "/auth/dev/login" not in loc
        settings = _config.get_settings()
        assert loc.startswith(settings.gckey_authorize_url)


# ---------------------------------------------------------------------------
# Claimant status view + own-data-only (Req 19.7)
# ---------------------------------------------------------------------------


class TestClaimStatus:
    SPA_KEYS = {
        "claim_id",
        "status",
        "risk_decision",
        "risk_score",
        "explanation_en",
        "explanation_fr",
        "submitted_at",
        "decided_at",
    }

    def test_own_claim_returns_contract_keys(self, client, repo):
        repo.add_claim(
            "c-own",
            sin_hash="mysin",
            db_status="SCORED",
            risk_score=20,
            risk_decision="AUTO_APPROVE",
            explanation_en="ok",
            explanation_fr="ok-fr",
        )
        resp = client.get(
            "/portal/claims/c-own", cookies=_session_cookie("CLAIMANT", "mysin")
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == self.SPA_KEYS
        assert body["claim_id"] == "c-own"
        assert body["status"] == "DECIDED"  # SCORED -> DECIDED mapping
        assert body["risk_decision"] == "AUTO_APPROVE"

    def test_claimant_cannot_read_other_claim(self, client, repo):
        repo.add_claim("c-other", sin_hash="otherhash", risk_decision="AUTO_APPROVE")
        resp = client.get(
            "/portal/claims/c-other", cookies=_session_cookie("CLAIMANT", "mine")
        )
        # Own-data-only: masked as 404 to avoid leaking existence.
        assert resp.status_code == 404

    def test_absent_claim_is_404(self, client):
        resp = client.get(
            "/portal/claims/missing", cookies=_session_cookie("CLAIMANT", "x")
        )
        assert resp.status_code == 404

    def test_requires_auth(self, client):
        client.cookies.clear()
        resp = client.get("/portal/claims/anything")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Investigator queue (Req 22.1) + role gates + masking (Req 22.6)
# ---------------------------------------------------------------------------


class TestInvestigatorQueue:
    ROW_KEYS = {
        "claim_id",
        "risk_score",
        "risk_decision",
        "received_at",
        "employer_bn",
        "claimant_name",
        "claimant_sin",
        "address",
    }

    def test_only_flagged_decisions_sorted_desc(self, client, repo):
        _seed_flagged(repo, "low", risk_score=71, risk_decision="INVESTIGATE")
        _seed_flagged(repo, "high", risk_score=95, risk_decision="FLAG_FOR_REVIEW")
        repo.add_claim("approved", risk_score=10, risk_decision="AUTO_APPROVE")

        resp = client.get(
            "/portal/investigator/claims", cookies=_session_cookie("INVESTIGATOR")
        )
        assert resp.status_code == 200
        claims = resp.json()["claims"]
        assert [c["claim_id"] for c in claims] == ["high", "low"]
        assert set(claims[0].keys()) == self.ROW_KEYS

    def test_reviewer_pii_masked(self, client, repo):
        _seed_flagged(repo, "c-1", claimant_name="Jane Doe", claimant_sin="123-456-789")
        resp = client.get(
            "/portal/investigator/claims", cookies=_session_cookie("REVIEWER")
        )
        row = resp.json()["claims"][0]
        assert row["claimant_name"] is None
        assert row["address"] is None
        assert row["claimant_sin"] == "***-**-789"

    def test_investigator_pii_unmasked(self, client, repo):
        _seed_flagged(repo, "c-1", claimant_name="Jane Doe", claimant_sin="123-456-789")
        resp = client.get(
            "/portal/investigator/claims", cookies=_session_cookie("INVESTIGATOR")
        )
        row = resp.json()["claims"][0]
        assert row["claimant_name"] == "Jane Doe"
        assert row["claimant_sin"] == "123-456-789"

    def test_claimant_role_denied(self, client, repo):
        resp = client.get(
            "/portal/investigator/claims", cookies=_session_cookie("CLAIMANT", "x")
        )
        assert resp.status_code == 403

    def test_no_cookie_401(self, client):
        client.cookies.clear()
        resp = client.get("/portal/investigator/claims")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Investigator detail (Req 22.2) + masking + 404
# ---------------------------------------------------------------------------


class TestInvestigatorDetail:
    DETAIL_KEYS = {
        "claim_id",
        "risk_score",
        "risk_decision",
        "shap_base_value",
        "shap_attributions",
        "indicators",
        "explanation_en",
        "explanation_fr",
        "claimant_name",
        "claimant_sin",
        "employer_bn",
        "disposition",
        "case_notes",
    }
    SHAP_KEYS = {"indicator_id", "feature_name", "contribution"}
    INDICATOR_KEYS = {
        "indicator_id",
        "engine",
        "severity",
        "title",
        "explanation_en",
        "explanation_fr",
        "shap_contribution",
    }

    def test_detail_contract_keys(self, client, repo):
        _seed_flagged(repo, "c-1")
        resp = client.get(
            "/portal/investigator/claims/c-1",
            cookies=_session_cookie("INVESTIGATOR"),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == self.DETAIL_KEYS
        assert set(body["shap_attributions"][0].keys()) == self.SHAP_KEYS
        assert set(body["indicators"][0].keys()) == self.INDICATOR_KEYS

    def test_detail_reviewer_denied_403(self, client, repo):
        # Req 22.6: only INVESTIGATOR/ADMINISTRATOR may access flagged claim
        # DETAILS. REVIEWER is limited to the masked queue + SSE and must be
        # denied at the detail endpoint (not merely served masked data).
        _seed_flagged(repo, "c-1", claimant_name="Jane Doe", claimant_sin="123-456-789")
        resp = client.get(
            "/portal/investigator/claims/c-1", cookies=_session_cookie("REVIEWER")
        )
        assert resp.status_code == 403

    def test_detail_administrator_allowed_unmasked(self, client, repo):
        # ADMINISTRATOR is a superuser: full unmasked detail access (Req 22.6).
        _seed_flagged(repo, "c-1", claimant_name="Jane Doe", claimant_sin="123-456-789")
        resp = client.get(
            "/portal/investigator/claims/c-1", cookies=_session_cookie("ADMINISTRATOR")
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["claimant_name"] == "Jane Doe"
        assert body["risk_score"] == 88

    def test_detail_absent_404(self, client):
        resp = client.get(
            "/portal/investigator/claims/nope", cookies=_session_cookie("INVESTIGATOR")
        )
        assert resp.status_code == 404

    def test_detail_claimant_denied(self, client, repo):
        _seed_flagged(repo, "c-1")
        resp = client.get(
            "/portal/investigator/claims/c-1", cookies=_session_cookie("CLAIMANT", "x")
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Disposition (Req 22.3, 22.4)
# ---------------------------------------------------------------------------


class _RecordingProducer:
    """Captures outcome.feedback publishes without a real broker."""

    def __init__(self):
        self.sent = []

    async def send_and_wait(self, topic, value=None, key=None):
        self.sent.append((topic, json.loads(value.decode("utf-8")), key))


class TestDisposition:
    def _publish_producer(self):
        producer = _RecordingProducer()
        app.state.kafka_producer = producer
        return producer

    def test_confirmed_fraud_publishes_feedback(self, client, repo):
        _seed_flagged(repo, "c-1")
        producer = self._publish_producer()
        resp = client.put(
            "/api/v1/claims/c-1/disposition",
            json={"disposition": "CONFIRMED_FRAUD", "case_notes": "notes"},
            cookies=_session_cookie("INVESTIGATOR"),
        )
        assert resp.status_code == 200
        assert resp.json()["disposition"] == "CONFIRMED_FRAUD"
        assert len(producer.sent) == 1
        topic, event, _ = producer.sent[0]
        assert topic == "outcome.feedback"
        assert event["claim_id"] == "c-1"

    def test_false_positive_publishes_feedback(self, client, repo):
        _seed_flagged(repo, "c-1")
        producer = self._publish_producer()
        resp = client.put(
            "/api/v1/claims/c-1/disposition",
            json={"disposition": "FALSE_POSITIVE", "case_notes": ""},
            cookies=_session_cookie("INVESTIGATOR"),
        )
        assert resp.status_code == 200
        assert len(producer.sent) == 1

    def test_needs_review_does_not_publish(self, client, repo):
        _seed_flagged(repo, "c-1")
        producer = self._publish_producer()
        resp = client.put(
            "/api/v1/claims/c-1/disposition",
            json={"disposition": "NEEDS_FURTHER_REVIEW", "case_notes": ""},
            cookies=_session_cookie("INVESTIGATOR"),
        )
        assert resp.status_code == 200
        assert producer.sent == []

    def test_disposition_persisted(self, client, repo):
        _seed_flagged(repo, "c-1")
        self._publish_producer()
        client.put(
            "/api/v1/claims/c-1/disposition",
            json={"disposition": "NEEDS_FURTHER_REVIEW", "case_notes": "look again"},
            cookies=_session_cookie("INVESTIGATOR"),
        )
        detail = client.get(
            "/portal/investigator/claims/c-1", cookies=_session_cookie("INVESTIGATOR")
        ).json()
        assert detail["disposition"] == "NEEDS_FURTHER_REVIEW"
        assert detail["case_notes"] == "look again"

    def test_reviewer_denied_disposition(self, client, repo):
        _seed_flagged(repo, "c-1")
        resp = client.put(
            "/api/v1/claims/c-1/disposition",
            json={"disposition": "CONFIRMED_FRAUD", "case_notes": ""},
            cookies=_session_cookie("REVIEWER"),
        )
        assert resp.status_code == 403

    def test_invalid_disposition_422(self, client, repo):
        _seed_flagged(repo, "c-1")
        self._publish_producer()
        resp = client.put(
            "/api/v1/claims/c-1/disposition",
            json={"disposition": "BOGUS", "case_notes": ""},
            cookies=_session_cookie("INVESTIGATOR"),
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# SSE stream (Req 22.1)
# ---------------------------------------------------------------------------


class TestSSE:
    def test_stream_emits_pushed_claim(self, repo):
        import asyncio

        broadcaster = _sse.ClaimEventBroadcaster()

        async def _run():
            queue = await broadcaster.subscribe()
            claim = {"claim_id": "c-9", "risk_score": 90, "risk_decision": "INVESTIGATE"}
            await broadcaster.publish(claim)
            gen = broadcaster.event_stream(queue, heartbeat_seconds=5.0)
            frame = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
            await gen.aclose()
            return frame

        frame = asyncio.run(_run())
        assert frame.startswith("data: ")
        payload = json.loads(frame[len("data: ") :].strip())
        assert payload["claim_id"] == "c-9"

    def test_stream_requires_investigator_role(self, client, repo):
        resp = client.get(
            "/portal/events/claims", cookies=_session_cookie("CLAIMANT", "x")
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Claim submission (Option A: BFF owns/translates the SPA's multipart contract)
# ---------------------------------------------------------------------------


class TestClaimSubmission:
    """POST /api/v1/claims — the BFF is a THIN multipart forwarder to Claim Intake.

    Design-faithful (design.md GCKey flow): the portal forwards the claimant's
    multipart submission to the Claim Intake Service — attaching the caller's
    GCKey access token as ``Authorization: Bearer`` — and Claim Intake owns
    validation + persistence and independently validates the Bearer (Req 19.7).
    These tests stand claim-intake up as an httpx MockTransport and assert the
    BFF (a) requires a CLAIMANT session, (b) forwards the multipart body to
    CLAIM_INTAKE_URL with the GCKey Bearer header (NOT X-Dev-*), and (c) returns
    intake's response (202 {claim_id} or 400 {errors}) unchanged.
    """

    VALID_FIELDS = {
        "sin": "046454286",
        "employer_bn": "123456789RT0001",
        "insurable_hours": "1200",
        "insurable_earnings": "45000",
        "separation_date": "2024-01-15",
        "separation_reason": "Shortage of work",
    }

    def _mock_intake(self, capture, *, status_code=202, body=None):
        """Install an httpx MockTransport that impersonates claim-intake.

        Disables the DEV-ONLY auto-ROE side effect by default so these tests
        exercise the pure thin-forwarder contract (a dedicated test below
        re-enables it). The auto-ROE uses its own httpx client — not this
        proxy_transport — so leaving it on would otherwise fire a real
        (swallowed) network call to the roe-adapter on every submit.
        """
        import httpx

        # Focus these tests on the intake forward; opt out of the dev auto-ROE.
        if getattr(app.state, "settings", None) is not None:
            app.state.settings.dev_auto_roe = False

        def handler(request: httpx.Request) -> httpx.Response:
            capture["method"] = request.method
            capture["url"] = str(request.url)
            capture["content_type"] = request.headers.get("content-type", "")
            capture["authorization"] = request.headers.get("authorization")
            capture["x_dev_role"] = request.headers.get("x-dev-role")
            capture["x_dev_sin_hash"] = request.headers.get("x-dev-sin-hash")
            capture["body"] = request.content
            payload = body if body is not None else {"claim_id": "intake-123", "status": "RECEIVED"}
            return httpx.Response(status_code, json=payload)

        app.state.proxy_transport = httpx.MockTransport(handler)

    def test_forwards_multipart_to_intake_and_returns_202(self, client):
        capture: dict = {}
        self._mock_intake(capture)
        resp = client.post(
            "/api/v1/claims",
            data=dict(self.VALID_FIELDS),
            cookies=_session_cookie("CLAIMANT", "sinhash-abc"),
        )
        # BFF returns intake's response unchanged.
        assert resp.status_code == 202
        assert resp.json()["claim_id"] == "intake-123"
        # Forwarded to claim-intake's /api/v1/claims as a form/multipart body.
        assert capture["method"] == "POST"
        assert capture["url"].endswith("/api/v1/claims")
        assert "form" in capture["content_type"] or "multipart" in capture["content_type"]

    def test_gckey_bearer_is_forwarded_from_session(self, client):
        capture: dict = {}
        self._mock_intake(capture)
        client.post(
            "/api/v1/claims",
            data=dict(self.VALID_FIELDS),
            cookies=_session_cookie("CLAIMANT", "sinhash-abc"),
        )
        # The BFF forwards the GCKey access token (from the session) as a
        # Bearer, and does NOT fabricate X-Dev-* headers (Req 19.7).
        assert capture["authorization"] == f"Bearer {TEST_ACCESS_TOKEN}"
        assert capture["x_dev_role"] is None
        assert capture["x_dev_sin_hash"] is None

    def test_session_without_access_token_returns_401(self, client):
        capture: dict = {}
        self._mock_intake(capture)
        # A CLAIMANT session lacking a GCKey access token cannot authorise a
        # downstream call -> 401 (re-authenticate), not an unauthenticated call.
        resp = client.post(
            "/api/v1/claims",
            data=dict(self.VALID_FIELDS),
            cookies=_session_cookie("CLAIMANT", "sinhash-abc", access_token=None),
        )
        assert resp.status_code == 401

    def test_intake_400_errors_are_passed_through(self, client):
        capture: dict = {}
        self._mock_intake(
            capture,
            status_code=400,
            body={"errors": [{"field": "insurable_hours", "code": "MISSING", "hint": "x"}]},
        )
        resp = client.post(
            "/api/v1/claims",
            data=dict(self.VALID_FIELDS),
            cookies=_session_cookie("CLAIMANT", "x"),
        )
        assert resp.status_code == 400
        assert resp.json()["errors"][0]["field"] == "insurable_hours"

    def test_optional_roe_document_is_forwarded(self, client):
        capture: dict = {}
        self._mock_intake(capture)
        resp = client.post(
            "/api/v1/claims",
            data=dict(self.VALID_FIELDS),
            files={"roe_document": ("roe.pdf", b"%PDF-1.4 fake", "application/pdf")},
            cookies=_session_cookie("CLAIMANT", "x"),
        )
        assert resp.status_code == 202
        # A multipart body carrying the file was forwarded.
        assert "multipart" in capture["content_type"]
        assert b"roe.pdf" in capture["body"]

    def test_dev_auto_roe_posts_matching_roe_when_enabled(self, client, monkeypatch):
        """DEV-ONLY: with dev_auto_roe on, a 202 submit also posts a matching ROE
        to the roe-adapter (same claim_id) over its own client, standing in for
        ROE Web so the pipeline can pair + score from a browser submit."""
        capture: dict = {}
        self._mock_intake(capture)  # returns claim_id=intake-123
        # Re-enable the dev auto-ROE that _mock_intake disabled.
        app.state.settings.dev_auto_roe = True
        app.state.settings.app_env = "development"
        app.state.settings.roe_adapter_url = "http://roe-adapter:8002"

        roe_calls: list = []

        # The auto-ROE uses a plain httpx.AsyncClient; capture its POST.
        import httpx as _httpx

        async def fake_post(self, url, **kwargs):  # noqa: ANN001
            roe_calls.append({"url": str(url), "json": kwargs.get("json"),
                              "headers": kwargs.get("headers", {})})
            return _httpx.Response(202, json={"claim_id": "intake-123",
                                              "status": "RECEIVED", "published": True})

        monkeypatch.setattr(_httpx.AsyncClient, "post", fake_post)

        resp = client.post(
            "/api/v1/claims",
            data=dict(self.VALID_FIELDS),
            cookies=_session_cookie("CLAIMANT", "sinhash-abc"),
        )
        assert resp.status_code == 202
        # Exactly one auto-ROE was posted to the roe-adapter, same claim_id,
        # carrying the submitted employer/earnings fields + the GCKey Bearer.
        roe = [c for c in roe_calls if c["url"].endswith("/roe/rest")]
        assert len(roe) == 1, roe_calls
        body = roe[0]["json"]
        assert body["claimId"] == "intake-123"
        assert body["employerBn"] == self.VALID_FIELDS["employer_bn"]
        assert body["insurableEarnings"] == self.VALID_FIELDS["insurable_earnings"]
        assert roe[0]["headers"].get("Authorization") == f"Bearer {TEST_ACCESS_TOKEN}"

    def test_dev_auto_roe_disabled_posts_no_roe(self, client, monkeypatch):
        """With dev_auto_roe off, a submit forwards ONLY to intake (no ROE)."""
        capture: dict = {}
        self._mock_intake(capture)  # this also sets dev_auto_roe = False
        roe_calls: list = []
        import httpx as _httpx

        async def fake_post(self, url, **kwargs):  # noqa: ANN001
            roe_calls.append(str(url))
            return _httpx.Response(202, json={})

        monkeypatch.setattr(_httpx.AsyncClient, "post", fake_post)
        resp = client.post(
            "/api/v1/claims",
            data=dict(self.VALID_FIELDS),
            cookies=_session_cookie("CLAIMANT", "x"),
        )
        assert resp.status_code == 202
        assert not any(u.endswith("/roe/rest") for u in roe_calls), roe_calls

    def test_intake_unreachable_returns_502(self, client):
        import httpx

        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("intake down")

        app.state.proxy_transport = httpx.MockTransport(boom)
        resp = client.post(
            "/api/v1/claims",
            data=dict(self.VALID_FIELDS),
            cookies=_session_cookie("CLAIMANT", "x"),
        )
        assert resp.status_code == 502

    def test_no_session_cookie_returns_401(self, client):
        client.cookies.clear()
        resp = client.post("/api/v1/claims", data=dict(self.VALID_FIELDS))
        assert resp.status_code == 401

    def test_non_claimant_role_returns_403(self, client):
        resp = client.post(
            "/api/v1/claims",
            data=dict(self.VALID_FIELDS),
            cookies=_session_cookie("REVIEWER"),
        )
        assert resp.status_code == 403


class TestReverseProxy:
    def _install_mock_transport(self, capture):
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            capture["method"] = request.method
            capture["url"] = str(request.url)
            capture["authorization"] = request.headers.get("authorization")
            capture["body"] = request.content
            return httpx.Response(202, json={"claim_id": "proxied-123"})

        app.state.proxy_transport = httpx.MockTransport(handler)

    # The Privacy Act paths are reverse-proxied to Claim Intake with the GCKey
    # Bearer from the session; they require an authenticated CLAIMANT.

    def test_privacy_access_forwards_with_bearer(self, client):
        capture: dict = {}
        self._install_mock_transport(capture)
        client.get(
            "/api/v1/privacy/access", cookies=_session_cookie("CLAIMANT", "sin-1")
        )
        assert capture["method"] == "GET"
        assert "/api/v1/privacy/access" in capture["url"]
        assert capture["authorization"] == f"Bearer {TEST_ACCESS_TOKEN}"

    def test_privacy_delete_forwards_with_bearer(self, client):
        capture: dict = {}
        self._install_mock_transport(capture)
        client.request(
            "DELETE", "/api/v1/privacy/delete", cookies=_session_cookie("CLAIMANT", "sin-1")
        )
        assert capture["method"] == "DELETE"
        assert "/api/v1/privacy/delete" in capture["url"]
        assert capture["authorization"] == f"Bearer {TEST_ACCESS_TOKEN}"

    def test_privacy_access_requires_auth(self, client):
        client.cookies.clear()
        resp = client.get("/api/v1/privacy/access")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


class TestHealth:
    def test_healthz_degraded_without_backends(self, client):
        app.state.db_pool = None
        app.state.kafka_producer = None
        resp = client.get("/healthz")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "degraded"
        assert set(body["checks"].keys()) == {"database", "kafka"}


# ---------------------------------------------------------------------------
# Session signing unit tests
# ---------------------------------------------------------------------------


class TestSessionSigning:
    def test_roundtrip(self):
        token = sign_session({"role": "INVESTIGATOR", "sin_hash": None}, "s", max_age=60)
        payload = _auth.verify_session(token, "s", max_age=60)
        assert payload["role"] == "INVESTIGATOR"

    def test_tampered_rejected(self):
        token = sign_session({"role": "ADMINISTRATOR"}, "s")
        tampered = token[:-2] + ("aa" if token[-2:] != "aa" else "bb")
        with pytest.raises(_auth.SessionError):
            _auth.verify_session(tampered, "s")

    def test_wrong_secret_rejected(self):
        token = sign_session({"role": "CLAIMANT"}, "secret-a")
        with pytest.raises(_auth.SessionError):
            _auth.verify_session(token, "secret-b")


# ---------------------------------------------------------------------------
# DEV-ONLY one-click role login — GET /auth/dev/enabled + /auth/dev/login
# ---------------------------------------------------------------------------


class TestDevLogin:
    """The dev role-login lets a demonstrator pick a role with one click (no
    GCKey IdP locally). It reuses the SAME signed session cookie the real GCKey
    callback issues, and is gated by is_development + dev_login_enabled."""

    _REPO_ROOT = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..")
    )
    _SIGNING_KEY = os.path.join(
        _REPO_ROOT, "scripts", "dev", "jwks", "signing-key.pem"
    )

    def _decode_session(self, cookie_value: str) -> dict:
        """Verify + decode the session cookie the endpoint set."""
        return _auth.verify_session(cookie_value, SESSION_SECRET, max_age=3600)

    def test_enabled_probe_true_in_development(self, client):
        app.state.settings.app_env = "development"
        app.state.settings.dev_login_enabled = True
        resp = client.get("/auth/dev/enabled")
        assert resp.status_code == 200
        assert resp.json() == {"enabled": True}

    def test_enabled_probe_false_when_not_development(self, client):
        app.state.settings.app_env = "production"
        resp = client.get("/auth/dev/enabled")
        assert resp.status_code == 200
        assert resp.json() == {"enabled": False}

    def test_dev_login_claimant_sets_session_with_sin_hash(self, client):
        app.state.settings.app_env = "development"
        app.state.settings.dev_login_enabled = True
        client.cookies.clear()
        # The endpoint honours an in-app `next` (the SPA passes /submit for a
        # claimant); default is "/".
        resp = client.get(
            "/auth/dev/login",
            params={"role": "CLAIMANT", "next": "/submit"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/submit"
        assert COOKIE_NAME in resp.cookies
        payload = self._decode_session(resp.cookies[COOKIE_NAME])
        assert payload["role"] == "CLAIMANT"
        # A CLAIMANT session is keyed by sin_hash (own-data).
        assert isinstance(payload.get("sin_hash"), str) and payload["sin_hash"]

    def test_dev_login_investigator_sets_session_no_sin_hash(self, client):
        app.state.settings.app_env = "development"
        app.state.settings.dev_login_enabled = True
        client.cookies.clear()
        resp = client.get(
            "/auth/dev/login", params={"role": "INVESTIGATOR"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/"  # default next when not given
        payload = self._decode_session(resp.cookies[COOKIE_NAME])
        assert payload["role"] == "INVESTIGATOR"
        assert not payload.get("sin_hash")

    def test_dev_login_next_must_be_in_app(self, client):
        # An external redirect target is ignored (no open redirect even in dev).
        app.state.settings.app_env = "development"
        app.state.settings.dev_login_enabled = True
        resp = client.get(
            "/auth/dev/login",
            params={"role": "INVESTIGATOR", "next": "https://evil.example/x"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/"

    def test_dev_login_mints_access_token_when_key_present(self, client):
        # With the real dev signing key configured, the session carries a
        # JWKS-valid GCKey Bearer (roles claim = the chosen role) for the
        # downstream Bearer-forward on a claim submit.
        if not os.path.exists(self._SIGNING_KEY):
            pytest.skip("dev signing key not present (run gen-service-token.py)")
        app.state.settings.app_env = "development"
        app.state.settings.dev_login_enabled = True
        app.state.settings.dev_signing_key_path = self._SIGNING_KEY
        resp = client.get(
            "/auth/dev/login", params={"role": "CLAIMANT"}, follow_redirects=False
        )
        payload = self._decode_session(resp.cookies[COOKIE_NAME])
        token = payload.get("access_token")
        assert isinstance(token, str) and token.count(".") == 2  # a JWT
        # The token's roles claim matches the chosen role.
        import base64 as _b64
        import json as _json

        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        claims = _json.loads(_b64.urlsafe_b64decode(body))
        assert claims["roles"] == ["CLAIMANT"]

    def test_dev_login_404_when_disabled(self, client):
        app.state.settings.app_env = "development"
        app.state.settings.dev_login_enabled = False
        resp = client.get(
            "/auth/dev/login", params={"role": "CLAIMANT"}, follow_redirects=False
        )
        assert resp.status_code == 404

    def test_dev_login_404_when_not_development(self, client):
        app.state.settings.app_env = "production"
        app.state.settings.dev_login_enabled = True
        resp = client.get(
            "/auth/dev/login", params={"role": "CLAIMANT"}, follow_redirects=False
        )
        assert resp.status_code == 404

    def test_dev_login_rejects_unknown_role(self, client):
        app.state.settings.app_env = "development"
        app.state.settings.dev_login_enabled = True
        resp = client.get(
            "/auth/dev/login", params={"role": "WIZARD"}, follow_redirects=False
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# claim.scored -> SSE broadcaster consumer (Req 22.1 live dashboard updates)
# ---------------------------------------------------------------------------


class TestScoredConsumer:
    """The PortalScoredConsumer pushes newly-scored FLAGGED claims to the SSE
    broadcaster so open dashboards update live (no manual reload). AUTO_APPROVE
    and INDETERMINATE decisions are NOT surfaced in the investigator queue."""

    import asyncio as _asyncio

    PortalScoredConsumer = _consumer.PortalScoredConsumer
    ClaimEventBroadcaster = _sse.ClaimEventBroadcaster

    def _consumer_for(self, repo, broadcaster):
        return self.PortalScoredConsumer(
            broadcaster,
            lambda: repo,
            # Row-read is instant against the fake; the consumer never opens a
            # real Kafka connection in these tests (we call _handle directly).
        )

    def _scored_event(self, claim_id, decision):
        return {
            "event_type": "claim.scored",
            "claim_id": claim_id,
            "risk_score": 88,
            "risk_decision": decision,
            "assessment": {},
            "scored_at": "2024-01-02T00:00:00+00:00",
        }

    def test_flagged_claim_is_published_to_subscribers(self):
        async def run():
            repo = FakePortalRepository()
            _seed_flagged(repo, "c-live", risk_decision="INVESTIGATE", risk_score=88)
            broadcaster = self.ClaimEventBroadcaster()
            queue = await broadcaster.subscribe()
            consumer = self._consumer_for(repo, broadcaster)

            # A raw JSON bytes frame, as aiokafka would deliver it.
            await consumer._handle(
                json.dumps(self._scored_event("c-live", "INVESTIGATE")).encode()
            )

            # The subscriber received the UNMASKED flagged row for that claim.
            claim = await self._asyncio.wait_for(queue.get(), timeout=1.0)
            assert claim["claim_id"] == "c-live"
            assert claim["risk_decision"] == "INVESTIGATE"
            assert claim["risk_score"] == 88
            assert claim["employer_bn"]  # unmasked row carries employer_bn

        self._asyncio.run(run())

    def test_flag_for_review_is_published(self):
        async def run():
            repo = FakePortalRepository()
            _seed_flagged(repo, "c-flag", risk_decision="FLAG_FOR_REVIEW", risk_score=44)
            broadcaster = self.ClaimEventBroadcaster()
            queue = await broadcaster.subscribe()
            consumer = self._consumer_for(repo, broadcaster)
            await consumer._handle(self._scored_event("c-flag", "FLAG_FOR_REVIEW"))
            claim = await self._asyncio.wait_for(queue.get(), timeout=1.0)
            assert claim["claim_id"] == "c-flag"

        self._asyncio.run(run())

    def test_auto_approve_is_not_published(self):
        async def run():
            repo = FakePortalRepository()
            repo.add_claim("c-ok", risk_decision="AUTO_APPROVE", risk_score=10)
            broadcaster = self.ClaimEventBroadcaster()
            queue = await broadcaster.subscribe()
            consumer = self._consumer_for(repo, broadcaster)
            await consumer._handle(self._scored_event("c-ok", "AUTO_APPROVE"))
            # Nothing is pushed for a non-flagged decision.
            with pytest.raises(self._asyncio.TimeoutError):
                await self._asyncio.wait_for(queue.get(), timeout=0.2)

        self._asyncio.run(run())

    def test_indeterminate_is_not_published(self):
        async def run():
            repo = FakePortalRepository()
            repo.add_claim("c-ind", risk_decision="INDETERMINATE", risk_score=0)
            broadcaster = self.ClaimEventBroadcaster()
            queue = await broadcaster.subscribe()
            consumer = self._consumer_for(repo, broadcaster)
            await consumer._handle(self._scored_event("c-ind", "INDETERMINATE"))
            with pytest.raises(self._asyncio.TimeoutError):
                await self._asyncio.wait_for(queue.get(), timeout=0.2)

        self._asyncio.run(run())

    def test_malformed_frame_is_ignored(self):
        async def run():
            repo = FakePortalRepository()
            broadcaster = self.ClaimEventBroadcaster()
            queue = await broadcaster.subscribe()
            consumer = self._consumer_for(repo, broadcaster)
            await consumer._handle(b"not json")  # must not raise
            with pytest.raises(self._asyncio.TimeoutError):
                await self._asyncio.wait_for(queue.get(), timeout=0.2)

        self._asyncio.run(run())

    def test_extract_scored_event_parses_bytes_str_dict(self):
        ev = {"event_type": "claim.scored", "claim_id": "x", "risk_decision": "INVESTIGATE"}
        assert _consumer.extract_scored_event(json.dumps(ev).encode()) == ev
        assert _consumer.extract_scored_event(json.dumps(ev)) == ev
        assert _consumer.extract_scored_event(ev) == ev
        assert _consumer.extract_scored_event(b"garbage") == {}


class _FakeGraphClient:
    """Fake PortalGraphServiceClient for the graph-endpoint tests.

    Returns preset cohort members + a community partition, or an UNAVAILABLE
    status to exercise the honest-degrade path — no live Graph Service.
    """

    GraphCallStatus = _graph_client.GraphCallStatus
    CohortFetchResult = _graph_client.CohortFetchResult
    CommunityResult = _graph_client.CommunityResult

    def __init__(self, *, members=None, partition=None, available=True):
        self._members = members or []
        self._partition = partition or {}
        self._available = available

    async def fetch_cohort(self, employer_bn):
        status = (
            self.GraphCallStatus.OK if self._available else self.GraphCallStatus.UNAVAILABLE
        )
        return self.CohortFetchResult(status=status, members=self._members)

    async def community_detect(self, employer_bn, *, seed=42):
        status = (
            self.GraphCallStatus.OK if self._available else self.GraphCallStatus.UNAVAILABLE
        )
        return self.CommunityResult(
            status=status,
            partition=self._partition,
            num_communities=len(set(self._partition.values())),
        )


class TestBuildGraph:
    """Pure fraud-ring graph composition (no I/O). Grounded on Req 5.1/5.5."""

    build_graph = staticmethod(_graph_client.build_graph)
    GraphCallStatus = _graph_client.GraphCallStatus

    def _members(self):
        # Two claimants share a surname hash AND postal FSA (a cluster); a third
        # is unrelated. All employed by the same employer.
        return [
            {"sin_hash": "h1", "surname_hash": "SMITH", "postal_fsa": "K1A",
             "insurable_hours": 421, "separation_date": "2024-03-01", "claim_id": "cl1"},
            {"sin_hash": "h2", "surname_hash": "SMITH", "postal_fsa": "K1A",
             "insurable_hours": 423, "separation_date": "2024-03-01", "claim_id": "cl2"},
            {"sin_hash": "h3", "surname_hash": "JONES", "postal_fsa": "M5V",
             "insurable_hours": 900, "separation_date": "2024-02-10", "claim_id": "cl3"},
        ]

    def test_composes_hub_and_claimant_nodes_with_employed_by_edges(self):
        graph = self.build_graph(
            employer_bn="123RT0001",
            cohort_members=self._members(),
            partition={"h1": 0, "h2": 0, "h3": 1},
            cohort_status=self.GraphCallStatus.OK,
        )
        assert graph["available"] is True
        assert graph["cohort_size"] == 3
        # 1 employer hub + 3 claimants.
        assert len(graph["nodes"]) == 4
        hub = [n for n in graph["nodes"] if n["type"] == "EMPLOYER"]
        assert len(hub) == 1 and hub[0]["id"] == "123RT0001"
        # Every claimant has an EMPLOYED_BY edge to the hub.
        employed_by = [e for e in graph["edges"] if e["kind"] == "EMPLOYED_BY"]
        assert len(employed_by) == 3
        assert all(e["target"] == "123RT0001" for e in employed_by)

    def test_cluster_members_flagged_singleton_not(self):
        graph = self.build_graph(
            employer_bn="123RT0001",
            cohort_members=self._members(),
            partition={"h1": 0, "h2": 0, "h3": 1},
            cohort_status=self.GraphCallStatus.OK,
        )
        by_id = {n["id"]: n for n in graph["nodes"] if n["type"] == "CLAIMANT"}
        # h1/h2 share community 0 (size 2) -> flagged; h3 is a singleton -> not.
        assert by_id["h1"]["flagged"] is True
        assert by_id["h2"]["flagged"] is True
        assert by_id["h3"]["flagged"] is False
        assert graph["cluster_count"] == 1

    def test_shared_attribute_edges_derived(self):
        graph = self.build_graph(
            employer_bn="123RT0001",
            cohort_members=self._members(),
            partition={"h1": 0, "h2": 0, "h3": 1},
            cohort_status=self.GraphCallStatus.OK,
        )
        surname = {(e["source"], e["target"]) for e in graph["edges"]
                   if e["kind"] == "SHARED_SURNAME"}
        address = {(e["source"], e["target"]) for e in graph["edges"]
                   if e["kind"] == "SHARED_ADDRESS"}
        # h1<->h2 share both surname and FSA; h3 shares neither.
        assert surname == {("h1", "h2")}
        assert address == {("h1", "h2")}

    def test_no_employer_is_unavailable(self):
        graph = self.build_graph(
            employer_bn=None,
            cohort_members=[],
            partition={},
            cohort_status=self.GraphCallStatus.UNAVAILABLE,
        )
        assert graph["available"] is False
        assert graph["reason"] == "NO_EMPLOYER"
        assert graph["nodes"] == [] and graph["edges"] == []

    def test_graph_service_unavailable_is_honest(self):
        graph = self.build_graph(
            employer_bn="123RT0001",
            cohort_members=[],
            partition={},
            cohort_status=self.GraphCallStatus.UNAVAILABLE,
        )
        assert graph["available"] is False
        assert graph["reason"] == "GRAPH_SERVICE_UNAVAILABLE"
        assert graph["nodes"] == []

    def test_empty_cohort_is_honest(self):
        graph = self.build_graph(
            employer_bn="123RT0001",
            cohort_members=[],
            partition={},
            cohort_status=self.GraphCallStatus.OK,
        )
        assert graph["available"] is False
        assert graph["reason"] == "EMPTY_COHORT"

    def test_claimant_labels_are_pseudonymous(self):
        graph = self.build_graph(
            employer_bn="123RT0001",
            cohort_members=self._members(),
            partition={},
            cohort_status=self.GraphCallStatus.OK,
        )
        # Labels never leak the sin_hash; they are a stable derived handle.
        for n in graph["nodes"]:
            if n["type"] == "CLAIMANT":
                assert n["label"].startswith("Claimant ")
                assert n["id"] not in n["label"]


class TestInvestigatorGraphEndpoint:
    """GET /portal/investigator/claims/{claim_id}/graph (Req 22.2, 22.6)."""

    def _seed(self, repo, claim_id="c-1", employer_bn="123RT0001"):
        _seed_flagged(repo, claim_id, employer_bn=employer_bn)

    def test_composes_graph_from_graph_service(self, client, repo):
        self._seed(repo)
        app.state.graph_client = _FakeGraphClient(
            members=[
                {"sin_hash": "h1", "surname_hash": "S", "postal_fsa": "K1A"},
                {"sin_hash": "h2", "surname_hash": "S", "postal_fsa": "K1A"},
            ],
            partition={"h1": 0, "h2": 0},
        )
        try:
            resp = client.get(
                "/portal/investigator/claims/c-1/graph",
                cookies=_session_cookie("INVESTIGATOR"),
            )
        finally:
            app.state.graph_client = None
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is True
        assert body["employer_bn"] == "123RT0001"
        assert body["cohort_size"] == 2
        assert body["cluster_count"] == 1
        assert len(body["nodes"]) == 3  # hub + 2 claimants

    def test_administrator_allowed(self, client, repo):
        self._seed(repo)
        app.state.graph_client = _FakeGraphClient(
            members=[{"sin_hash": "h1"}], partition={}
        )
        try:
            resp = client.get(
                "/portal/investigator/claims/c-1/graph",
                cookies=_session_cookie("ADMINISTRATOR"),
            )
        finally:
            app.state.graph_client = None
        assert resp.status_code == 200

    def test_reviewer_denied_403(self, client, repo):
        # Req 22.6: REVIEWER may not access flagged claim details (graph included).
        self._seed(repo)
        resp = client.get(
            "/portal/investigator/claims/c-1/graph",
            cookies=_session_cookie("REVIEWER", "x"),
        )
        assert resp.status_code == 403

    def test_claimant_denied_403(self, client, repo):
        self._seed(repo)
        resp = client.get(
            "/portal/investigator/claims/c-1/graph",
            cookies=_session_cookie("CLAIMANT", "x"),
        )
        assert resp.status_code == 403

    def test_absent_claim_404(self, client):
        resp = client.get(
            "/portal/investigator/claims/nope/graph",
            cookies=_session_cookie("INVESTIGATOR"),
        )
        assert resp.status_code == 404

    def test_graph_service_down_degrades_gracefully(self, client, repo):
        self._seed(repo)
        app.state.graph_client = _FakeGraphClient(available=False)
        try:
            resp = client.get(
                "/portal/investigator/claims/c-1/graph",
                cookies=_session_cookie("INVESTIGATOR"),
            )
        finally:
            app.state.graph_client = None
        # Never 500: an honest available:false payload instead.
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is False
        assert body["reason"] == "GRAPH_SERVICE_UNAVAILABLE"
        assert body["nodes"] == []
