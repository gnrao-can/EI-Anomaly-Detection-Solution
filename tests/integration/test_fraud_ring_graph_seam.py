"""
tests/integration/test_fraud_ring_graph_seam.py
===============================================
The investigator fraud-ring graph ↔ Graph Service seam over REAL transports.

The portal-backend composes the investigator dashboard's fraud-ring network
graph (Req 22.2 surface) from a LIVE Graph Service read — there is no persisted
node/edge store. This test exercises that seam the way the regeneration rule
demands ("no seam is done until an integration test exercises it over REAL
transports"):

  * a REAL Graph Service (services/graph-service) runs on a loopback HTTP socket
    (LoopbackService), backed by the in-memory fake Neo4j driver (no container);
  * the portal-backend's ``PortalGraphServiceClient`` reaches it over a REAL
    ``httpx`` call across that socket, presenting a REAL GCKey service Bearer
    that the Graph Service validates via ``shared.auth`` against a REAL test
    JWKS served over HTTP (the session ``itest_service_auth`` fixture);
  * the cohort is seeded through the Graph Service's own ``POST /graph/construct``
    endpoint (also over real HTTP with the Bearer), so the graph the portal
    reads back is genuinely the one the service built.

Proves:
  * the seam composes an employer hub + claimant cohort with EMPLOYED_BY edges
    and community-detected clusters (Req 5.1 / 5.5);
  * PII never leaks (node ids are pseudonymous sin_hashes);
  * the seam degrades HONESTLY (``available: false``, empty graph, no 500) when
    the Graph Service is unreachable (grounding rule 1).

Requirements exercised: 5.1, 5.5, 19.7, 22.2.
"""

from __future__ import annotations

import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

from tests.conftest import load_service_module  # noqa: E402
from tests.integration.loopback_service import LoopbackService  # noqa: E402

_PORTAL_SIBLINGS = [
    "config",
    "auth",
    "repository",
    "sse",
    "consumer",
    "graph_client",
    "gckey",
    "main",
]

_EMPLOYER_BN = "123456789RT0001"
_SESSION_SECRET = "itest_portal_graph_secret"

# Set the portal session secret BEFORE any portal module (and its cached
# ``get_settings``) loads, so the signed cookie below matches what the auth
# layer validates with.
os.environ.setdefault("SESSION_SECRET", _SESSION_SECRET)


def _load_graph_service_app():
    """Build the real Graph Service app with the in-memory fake Neo4j driver."""
    graph_repo = load_service_module("graph-service", "graph_repo")
    graph_main = load_service_module(
        "graph-service",
        "main",
        siblings=["graph_repo", "community", "config", "main"],
    )
    repo = graph_repo.GraphRepository(test_mode=True)
    return graph_main.create_app(repository=repo)


def _load_portal():
    """Load the portal-backend modules (namespaced, collision-free)."""
    for name in _PORTAL_SIBLINGS[:-1]:
        load_service_module("portal-backend", name, siblings=_PORTAL_SIBLINGS)
    main = load_service_module("portal-backend", "main", siblings=_PORTAL_SIBLINGS)
    config = load_service_module("portal-backend", "config", siblings=_PORTAL_SIBLINGS)
    auth = load_service_module("portal-backend", "auth", siblings=_PORTAL_SIBLINGS)
    repository = load_service_module(
        "portal-backend", "repository", siblings=_PORTAL_SIBLINGS
    )
    sse = load_service_module("portal-backend", "sse", siblings=_PORTAL_SIBLINGS)
    return main, config, auth, repository, sse


def _seed_cohort(base_url: str, token: str) -> None:
    """Seed a 3-claimant cohort via the Graph Service's own construct endpoint.

    Two claimants share a surname hash + postal FSA (a cluster); the third is
    unrelated. All are EMPLOYED_BY the same employer, so community-detect places
    the two sharers in one community.
    """
    headers = {"Authorization": f"Bearer {token}"}
    members = [
        {"sin_hash": "h-aaa", "surname_hash": "SMITH", "postal_fsa": "K1A"},
        {"sin_hash": "h-bbb", "surname_hash": "SMITH", "postal_fsa": "K1A"},
        {"sin_hash": "h-ccc", "surname_hash": "JONES", "postal_fsa": "M5V"},
    ]
    with httpx.Client(base_url=base_url, timeout=5.0) as client:
        for m in members:
            resp = client.post(
                "/graph/construct",
                headers=headers,
                json={
                    "sin_hash": m["sin_hash"],
                    "employer_bn": _EMPLOYER_BN,
                    "surname_hash": m["surname_hash"],
                    "postal_fsa": m["postal_fsa"],
                    "insurable_hours": 421.0,
                    "separation_date": "2024-03-01",
                },
            )
            assert resp.status_code == 200, resp.text


@pytest.fixture
def portal_client(itest_service_auth):
    """A TestClient for portal-backend wired to a REAL loopback Graph Service.

    ``itest_service_auth`` (session-scoped) serves a REAL JWKS over HTTP and
    points ``shared.auth`` at it, so the Graph Service validates the minted
    service Bearer for real.
    """
    from fastapi.testclient import TestClient

    main, config, auth, repository, sse = _load_portal()
    token = itest_service_auth.mint_token(roles=["INVESTIGATOR"])

    graph_app = _load_graph_service_app()
    graph_svc = LoopbackService(graph_app, health_path="/healthz").start()
    _seed_cohort(graph_svc.base_url, token)

    app = main.app
    # A flagged claim whose employer matches the seeded cohort.
    repo = repository.FakePortalRepository()
    repo.add_claim(
        "c-graph",
        risk_score=88,
        risk_decision="INVESTIGATE",
        employer_bn=_EMPLOYER_BN,
    )

    # Point the portal's Graph Service client at the loopback service + real
    # token. ``auth.get_settings`` is the SAME cached instance the auth layer
    # reads, so mutating it here also drives cookie validation.
    settings = auth.get_settings()
    settings.graph_service_url = graph_svc.base_url
    settings.service_token = token
    session_secret = settings.session_secret

    with TestClient(app) as client:
        app.state.repository = repo
        app.state.settings = settings
        app.state.kafka_producer = None
        app.state.graph_client = None  # use the real httpx-backed client
        cookie_token = auth.sign_session(
            {"role": "INVESTIGATOR", "sin_hash": None},
            session_secret,
            max_age=3600,
        )
        client.cookies.set("ei_ads_session", cookie_token)
        yield client, graph_svc

    graph_svc.stop()


def test_graph_seam_composes_real_network(portal_client):
    client, _graph_svc = portal_client
    resp = client.get("/portal/investigator/claims/c-graph/graph")
    assert resp.status_code == 200
    body = resp.json()

    assert body["available"] is True
    assert body["employer_bn"] == _EMPLOYER_BN
    assert body["cohort_size"] == 3
    # h-aaa/h-bbb share surname + FSA -> one detected cluster.
    assert body["cluster_count"] >= 1

    node_ids = {n["id"] for n in body["nodes"]}
    assert _EMPLOYER_BN in node_ids  # employer hub present
    assert {"h-aaa", "h-bbb", "h-ccc"}.issubset(node_ids)

    # Every claimant is EMPLOYED_BY the hub over a real edge.
    employed_by = [e for e in body["edges"] if e["kind"] == "EMPLOYED_BY"]
    assert len(employed_by) == 3
    assert all(e["target"] == _EMPLOYER_BN for e in employed_by)

    # The two sharers are linked by shared-attribute edges (Req 5.5).
    shared = {
        frozenset((e["source"], e["target"]))
        for e in body["edges"]
        if e["kind"] in {"SHARED_SURNAME", "SHARED_ADDRESS"}
    }
    assert frozenset(("h-aaa", "h-bbb")) in shared

    # PII never leaks: node labels are pseudonymous, ids are hashes.
    for node in body["nodes"]:
        if node["type"] == "CLAIMANT":
            assert node["label"].startswith("Claimant ")


def test_graph_seam_degrades_when_service_down(portal_client):
    client, graph_svc = portal_client
    # Stop the Graph Service, then request the graph: the portal must NOT 500 —
    # it returns an honest unavailable payload (grounding rule 1).
    graph_svc.stop()

    resp = client.get("/portal/investigator/claims/c-graph/graph")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["reason"] == "GRAPH_SERVICE_UNAVAILABLE"
    assert body["nodes"] == []
