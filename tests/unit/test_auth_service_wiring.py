"""
tests/unit/test_auth_service_wiring.py
======================================
Task 29.1 — verify that the shared GCKey token-validation / RBAC dependencies
from ``shared/auth.py`` are applied across *all* EI-ADS FastAPI services.

For each protected service endpoint we assert the auth *boundary* behaviour
(Requirement 19.7, 22.6):

  * No ``Authorization`` header                → HTTP 401.
  * A valid token whose role is insufficient   → HTTP 403.
  * A valid token whose role is sufficient     → the request passes the auth
    gate (i.e. it is NOT rejected with 401/403; the downstream status depends
    on backend availability, which is irrelevant to the auth check).

The role gate is exercised through FastAPI's ``dependency_overrides`` on the
shared ``get_current_user`` dependency, so the tests never touch the network:
the GCKey JWKS fetch is bypassed entirely. The 401 path is exercised with no
override at all — the real ``HTTPBearer`` scheme rejects the missing header
before any JWKS lookup happens.

Services are loaded via the shared ``load_service_module`` helper so the whole
unit suite stays collision-free when run together. Because that helper *caches*
each service ``app`` (other test files load the same cached instance and set
their own ``dependency_overrides`` at import time), this file NEVER clears an
app's overrides outright — it snapshots and restores them around each request
via :func:`_temp_override`, leaving any pre-existing override untouched.

Requirements: 19.7, 22.6
"""

from __future__ import annotations

import contextlib

import pytest

from tests.conftest import load_service_module

# ``get_current_user`` is the single shared dependency every ``require_role``
# gate composes over; overriding it lets us inject a synthetic principal.
from shared.auth import get_current_user

try:
    from fastapi.testclient import TestClient

    _HAS_FASTAPI = True
except Exception:  # pragma: no cover - FastAPI always present in this project
    _HAS_FASTAPI = False


pytestmark = pytest.mark.skipif(
    not _HAS_FASTAPI, reason="fastapi/starlette TestClient unavailable"
)


def _load_app(service_dir: str, siblings: list[str]):
    module = load_service_module(service_dir, "main", siblings=[*siblings, "main"])
    return module.app


@contextlib.contextmanager
def _temp_override(app, principal_roles):
    """Temporarily set (or clear) the ``get_current_user`` override on ``app``.

    The prior override — which other test modules may have installed on this
    *shared, cached* app — is snapshotted and restored on exit so this file
    never destroys another test's authentication setup.

    ``principal_roles=None`` runs the body with NO override (missing-token path).
    """
    overrides = app.dependency_overrides
    had_prev = get_current_user in overrides
    prev = overrides.get(get_current_user)
    try:
        if principal_roles is None:
            overrides.pop(get_current_user, None)
        else:
            overrides[get_current_user] = lambda: {"roles": list(principal_roles)}
        yield
    finally:
        if had_prev:
            overrides[get_current_user] = prev
        else:
            overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Adapter GET endpoints (path params, no request body) requiring INVESTIGATOR.
#   (service_dir, siblings, http_method, url)
# ---------------------------------------------------------------------------

_ADAPTER_CASES = [
    ("business-registry-adapter", ["config", "client"], "GET",
     "/registry/employer/123456789RT0001"),
    ("cra-adapter", ["config", "client", "audit"], "GET",
     "/cra/t4/abc123/2023/123456789RT0001"),
    ("cbsa-adapter", ["config", "client", "absence"], "GET",
     "/cbsa/travel/abc123?province=ON"),
    ("job-bank-adapter", ["config", "client"], "GET",
     "/jobbank/postings/12345/ON/2023-06"),
    ("mvr-adapter", ["config", "client"], "GET", "/mvr/abc123?province=ON"),
    ("stats-canada-adapter", ["config", "client"], "GET",
     "/statscan/lfs/ON/2023-06"),
    ("geonames-adapter", ["config", "client"], "GET",
     "/geonames/resolve/K1A0A9"),
]


@pytest.mark.parametrize(
    "service_dir,siblings,method,url",
    _ADAPTER_CASES,
    ids=[c[0] for c in _ADAPTER_CASES],
)
def test_missing_token_returns_401(service_dir, siblings, method, url):
    app = _load_app(service_dir, siblings)
    with _temp_override(app, None):
        resp = TestClient(app).request(method, url)
    assert resp.status_code == 401, (
        f"{service_dir} {method} {url} should reject a missing token with 401, "
        f"got {resp.status_code}"
    )


@pytest.mark.parametrize(
    "service_dir,siblings,method,url",
    _ADAPTER_CASES,
    ids=[c[0] for c in _ADAPTER_CASES],
)
def test_insufficient_role_returns_403(service_dir, siblings, method, url):
    app = _load_app(service_dir, siblings)
    with _temp_override(app, ["CLAIMANT"]):
        resp = TestClient(app).request(method, url)
    assert resp.status_code == 403, (
        f"{service_dir} {method} {url} should reject a CLAIMANT with 403, "
        f"got {resp.status_code}"
    )


@pytest.mark.parametrize(
    "service_dir,siblings,method,url",
    _ADAPTER_CASES,
    ids=[c[0] for c in _ADAPTER_CASES],
)
def test_sufficient_role_passes_auth_gate(service_dir, siblings, method, url):
    app = _load_app(service_dir, siblings)
    with _temp_override(app, ["INVESTIGATOR"]):
        resp = TestClient(app).request(method, url)
    assert resp.status_code not in (401, 403), (
        f"{service_dir} {method} {url} should let an INVESTIGATOR through the "
        f"auth gate, got {resp.status_code}"
    )


@pytest.mark.parametrize(
    "service_dir,siblings,method,url",
    _ADAPTER_CASES,
    ids=[c[0] for c in _ADAPTER_CASES],
)
def test_administrator_passes_auth_gate(service_dir, siblings, method, url):
    app = _load_app(service_dir, siblings)
    with _temp_override(app, ["ADMINISTRATOR"]):
        resp = TestClient(app).request(method, url)
    assert resp.status_code not in (401, 403), (
        f"{service_dir} {method} {url} should let an ADMINISTRATOR through the "
        f"auth gate, got {resp.status_code}"
    )


@pytest.mark.parametrize(
    "service_dir,siblings",
    [(c[0], c[1]) for c in _ADAPTER_CASES],
    ids=[c[0] for c in _ADAPTER_CASES],
)
def test_healthz_is_public(service_dir, siblings):
    app = _load_app(service_dir, siblings)
    with _temp_override(app, None):
        resp = TestClient(app).get("/healthz")
    # Healthz never requires auth; it returns 200 or 503 depending on
    # dependency health, but never 401/403 (Requirement 21.4).
    assert resp.status_code not in (401, 403), (
        f"{service_dir} /healthz must remain public, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Detection engines / scorer / llm — POST endpoints requiring INVESTIGATOR.
#   (service_dir, siblings, url, json_body)
# ---------------------------------------------------------------------------

_POST_CASES = [
    ("document-forgery-engine",
     ["config", "ocr", "consistency", "metadata_forensics", "font_fingerprint",
      "signature_verification", "payroll_cross_reference", "style_analysis",
      "kafka_publisher"],
     "/engine/analyze", {}),
    ("fraud-ring-engine", ["config"], "/engine/analyze",
     {"employer_bn": "123456789RT0001", "separation_date": "2024-03-01"}),
    ("geographic-anomaly-engine",
     ["cbsa_client", "client", "config", "geo", "indicators", "kafka_producer",
      "mvr_client", "transport"],
     "/engine/analyze", {}),
    ("history-inconsistency-engine",
     ["config", "client", "discrepancy", "analysis", "narrative",
      "kafka_publisher", "noc"],
     "/engine/analyze", {}),
    ("regional-analysis-engine",
     ["analysis", "client", "config", "forecast", "kafka_producer", "providers",
      "seasonality"],
     "/engine/analyze", {}),
    ("repetitive-pattern-engine",
     ["config", "hours_clustering", "kafka_producer", "llm_client", "patterns",
      "repository"],
     "/engine/analyze", {}),
    ("ensemble-scorer", ["models", "scorer", "repository"], "/score",
     {"indicators": [], "claim_id": "abc"}),
    ("llm-service", ["config", "similarity"], "/llm/similarity",
     {"narrative_a": "a", "narrative_b": "b"}),
]


@pytest.mark.parametrize(
    "service_dir,siblings,url,body",
    _POST_CASES,
    ids=[c[0] for c in _POST_CASES],
)
def test_post_endpoint_missing_token_returns_401(service_dir, siblings, url, body):
    app = _load_app(service_dir, siblings)
    with _temp_override(app, None):
        resp = TestClient(app).post(url, json=body)
    assert resp.status_code == 401, (
        f"{service_dir} POST {url} should reject a missing token with 401, "
        f"got {resp.status_code}"
    )


@pytest.mark.parametrize(
    "service_dir,siblings,url,body",
    _POST_CASES,
    ids=[c[0] for c in _POST_CASES],
)
def test_post_endpoint_insufficient_role_returns_403(service_dir, siblings, url, body):
    app = _load_app(service_dir, siblings)
    with _temp_override(app, ["CLAIMANT"]):
        resp = TestClient(app).post(url, json=body)
    assert resp.status_code == 403, (
        f"{service_dir} POST {url} should reject a CLAIMANT with 403, "
        f"got {resp.status_code}"
    )
