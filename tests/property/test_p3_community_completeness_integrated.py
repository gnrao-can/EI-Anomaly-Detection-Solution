"""
tests/property/test_p3_community_completeness_integrated.py
===========================================================
Property 3: Community partition completeness (INTEGRATED). Validates: Requirements 5.10

This is the *integrated* counterpart to
``tests/property/test_p3_community_completeness.py`` (task 9.3, Graph Service
unit level). Task 13.3 wires the property **through the Fraud Ring Engine's
:class:`GraphServiceClient`** (``services/fraud-ring-engine/graph_client.py``) to
the Graph Service ``POST /graph/community-detect`` contract, exercising the same
completeness invariant end-to-end across the service boundary.

Statement
---------
*For any* random employer graph ``G`` (a bipartite ``(claimant_id, employer_id)``
edge list), when the Fraud Ring Engine asks the Graph Service to detect
communities via ``GraphServiceClient.community_detect(edges=...)``, the returned
partition SHALL be **complete and disjoint** over the claimant node set ``V``:

  * every claimant node appears in the partition exactly once — no node omitted,
    no extra nodes (``set(partition) == set(V)``);
  * each node maps to exactly one integer community id; and
  * the community sizes sum to ``N = |V|``
    (``sum(Counter(partition.values()).values()) == N``).

Wiring (no live network, no torch/torch_geometric)
---------------------------------------------------
The sandbox venv lacks ``torch`` / ``torch_geometric`` (the GNN stack), so the
Graph Service's documented **Louvain / networkx** path is the authoritative
partitioner (see ``services/graph-service/community.py`` — it degrades to Louvain
whenever the deep-learning stack is unavailable). A live HTTP endpoint is not
reachable in-sandbox, so we route the ``GraphServiceClient``'s injected transport
to the **real Graph Service partition logic in-process**:

    GraphServiceClient.community_detect(edges=...)      [fraud-ring-engine]
        -> fake httpx transport .post("/graph/community-detect", json=...)
            -> replicates the Graph Service endpoint by calling the REAL
               services/graph-service/community.py:
                   project_claimant_graph(edges) -> louvain_partition(graph)
            -> returns {"partition": {...}, "num_communities": k}
        -> CommunityResult.partition parsed back by the client

This validates the completeness invariant *through the client contract* (payload
shape, partition parsing, num_communities) while the partition itself is produced
by the Graph Service's genuine Louvain/networkx implementation.

Module loading
--------------
Both service directories are hyphenated (not importable as packages). The Fraud
Ring Engine's ``graph_client`` is loaded via the shared, collision-free
``tests.conftest.load_service_module`` helper. The Graph Service ``community``
module is loaded the same way (namespaced ``svc__graph-service__community`` key)
so it cannot collide with the fraud-ring modules on a full pytest run.

Property 3: Community partition completeness (INTEGRATED). Validates: Requirements 5.10
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.conftest import load_service_module

# ---------------------------------------------------------------------------
# Load the hyphenated service modules via the shared helper (namespaced
# sys.modules keys -> collision-free on full pytest runs).
# ---------------------------------------------------------------------------

# Fraud Ring Engine client (the integration point under test). Its siblings
# import each other by bare name, so register the full sibling set.
_FRE_SIBLINGS = [
    "config",
    "detection",
    "client",
    "community_analysis",
    "graph_client",
    "publisher",
    "main",
]
_graph_client = load_service_module(
    "fraud-ring-engine", "graph_client", siblings=_FRE_SIBLINGS
)
GraphServiceClient = _graph_client.GraphServiceClient
GraphCallStatus = _graph_client.GraphCallStatus

# Graph Service community-detection logic (the REAL partitioner the endpoint
# runs). ``community.py`` only imports networkx, so it has no bare siblings.
_gs_community = load_service_module("graph-service", "community", siblings=["community"])
project_claimant_graph = _gs_community.project_claimant_graph
louvain_partition = _gs_community.louvain_partition


# ---------------------------------------------------------------------------
# Hypothesis profile: CI runs 200 examples (design.md — Testing Strategy;
# tasks.md Notes: settings.register_profile("ci", max_examples=200, ...)).
# Guarded so a duplicate registration (from a sibling property module already
# loaded in the same session) does not raise; we always (re)load "ci".
# ---------------------------------------------------------------------------
try:
    settings.register_profile(
        "ci", max_examples=200, suppress_health_check=[HealthCheck.too_slow]
    )
except Exception:  # noqa: BLE001 - already registered elsewhere
    pass
settings.load_profile("ci")


# ===========================================================================
# In-process Graph Service transport
# ===========================================================================


class _GraphServiceResponse:
    """Minimal httpx-like response returned by the fake transport."""

    def __init__(self, body: dict[str, Any], status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._body


class _InProcessGraphServiceTransport:
    """Fake httpx client routing ``POST /graph/community-detect`` to the real
    Graph Service partition logic (Louvain / networkx), in-process.

    This mirrors ``services/graph-service/main.py::community_detect``: when the
    request carries an explicit ``edges`` list, it projects the bipartite edges
    to the claimant-only graph and runs Louvain, returning the same
    ``{"partition": {...}, "num_communities": k}`` body shape the Graph Service
    emits over HTTP.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, *args: Any, **kwargs: Any) -> _GraphServiceResponse:
        json_payload = kwargs.get("json", {})
        self.calls.append({"url": url, "json": json_payload})

        if not url.endswith("/graph/community-detect"):
            # Only the community-detect route is exercised by this property.
            return _GraphServiceResponse({}, status_code=404)

        raw_edges = json_payload.get("edges") or []
        seed = int(json_payload.get("seed", 42))
        # Normalise [claimant, employer] pairs (JSON lists) to tuples, exactly
        # as the Graph Service endpoint does before projecting.
        edges = [(c, e) for c, e in raw_edges]

        graph = project_claimant_graph(edges)
        partition = louvain_partition(graph, seed=seed)
        num_communities = len(set(partition.values()))

        body = {
            "partition": {str(k): int(v) for k, v in partition.items()},
            "num_communities": num_communities,
        }
        return _GraphServiceResponse(body, status_code=200)


def _run(coro):
    import asyncio

    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# The P3 completeness assertion (integrated flavour).
# ---------------------------------------------------------------------------


def _assert_complete_partition(partition: dict, nodes) -> None:
    """Assert the P3 completeness invariant for ``partition`` over ``nodes``.

    * keys cover exactly the claimant node set (no missing / extra);
    * every value is a single ``int`` community id;
    * community sizes sum to ``|V|``.
    """
    node_set = set(nodes)

    assert set(partition.keys()) == node_set, (
        f"partition keys {set(partition.keys())} != node set {node_set}"
    )
    assert all(isinstance(cid, int) for cid in partition.values()), (
        "community ids must be ints"
    )
    sizes = Counter(partition.values())
    assert sum(sizes.values()) == len(node_set), (
        f"community sizes {dict(sizes)} sum to {sum(sizes.values())} != {len(node_set)}"
    )


# ===========================================================================
# Strategy — random employer graphs (bipartite claimant/employer edge lists).
# ===========================================================================

# Small, bounded id pools keep the projected graph dense enough to be
# interesting while remaining fast at 200 examples.
_CLAIMANT_IDS = [f"c{i}" for i in range(8)]   # c0..c7
_EMPLOYER_IDS = [f"e{i}" for i in range(5)]   # e0..e4

employer_graph_strategy = st.lists(
    st.tuples(st.sampled_from(_CLAIMANT_IDS), st.sampled_from(_EMPLOYER_IDS)),
    min_size=0,
    max_size=40,
)


@given(edges=employer_graph_strategy)
def test_P3_integrated_partition_complete_via_client(edges):
    """Property 3: Community partition completeness (INTEGRATED). Validates: Requirements 5.10

    Generate a random employer graph, call the Graph Service community-detect
    contract through the Fraud Ring Engine's ``GraphServiceClient`` (routed
    in-process to the real Louvain/networkx partitioner), and assert every
    claimant node lands in exactly one community with sizes summing to N.
    """
    transport = _InProcessGraphServiceTransport()
    client = GraphServiceClient("http://graph-service", transport)

    result = _run(client.community_detect(edges=edges))

    # The Graph Service is reachable in-process, so the call must succeed.
    assert result.status is GraphCallStatus.OK
    # The client actually hit the community-detect route.
    assert transport.calls and transport.calls[0]["url"].endswith(
        "/graph/community-detect"
    )

    expected_claimants = {claimant for claimant, _employer in edges}

    # Completeness: every claimant in exactly one community; sizes sum to N.
    _assert_complete_partition(result.partition, expected_claimants)

    # num_communities reported by the contract equals the distinct community
    # count in the parsed partition (and 0 for the empty graph).
    assert result.num_communities == len(set(result.partition.values()))
    if not expected_claimants:
        assert result.partition == {}
        assert result.num_communities == 0


@given(edges=employer_graph_strategy)
def test_P3_integrated_sizes_sum_to_N(edges):
    """Property 3: Community partition completeness (INTEGRATED). Validates: Requirements 5.10

    Focused restatement of the "sum of community sizes = N" clause, checked on
    the partition returned through the client contract.
    """
    transport = _InProcessGraphServiceTransport()
    client = GraphServiceClient("http://graph-service", transport)

    result = _run(client.community_detect(edges=edges))
    assert result.status is GraphCallStatus.OK

    n = len({claimant for claimant, _employer in edges})
    sizes = Counter(result.partition.values())
    assert sum(sizes.values()) == n


# ===========================================================================
# Edge cases — enumerated explicitly (not property-generated).
# ===========================================================================


def test_P3_integrated_empty_employer_graph():
    """Property 3 (INTEGRATED). Validates: Requirements 5.10

    An empty employer graph yields an empty partition and 0 communities via the
    client contract.
    """
    transport = _InProcessGraphServiceTransport()
    client = GraphServiceClient("http://graph-service", transport)
    result = _run(client.community_detect(edges=[]))
    assert result.status is GraphCallStatus.OK
    assert result.partition == {}
    assert result.num_communities == 0


def test_P3_integrated_single_claimant_one_community():
    """Property 3 (INTEGRATED). Validates: Requirements 5.10

    A single claimant employed by one employer forms exactly one community
    covering that claimant.
    """
    transport = _InProcessGraphServiceTransport()
    client = GraphServiceClient("http://graph-service", transport)
    result = _run(client.community_detect(edges=[("only", "BN1")]))
    assert result.status is GraphCallStatus.OK
    _assert_complete_partition(result.partition, ["only"])
    assert result.num_communities == 1


def test_P3_integrated_shared_employer_cohort_covered():
    """Property 3 (INTEGRATED). Validates: Requirements 5.10

    A cohort of claimants all sharing one employer partitions completely — every
    claimant covered exactly once, sizes summing to the cohort size.
    """
    edges = [(f"c{i}", "BN1") for i in range(5)]
    transport = _InProcessGraphServiceTransport()
    client = GraphServiceClient("http://graph-service", transport)
    result = _run(client.community_detect(edges=edges))
    assert result.status is GraphCallStatus.OK
    _assert_complete_partition(result.partition, [f"c{i}" for i in range(5)])
