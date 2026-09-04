"""
tests/property/test_p3_community_completeness.py
================================================
Property 3: Community partition completeness. Validates: Requirements 5.10

Statement
---------
*For any* graph ``G`` with node set ``V`` that the Graph Service partitions into
communities, the resulting partition SHALL be **complete and disjoint**:

  * every node of ``V`` appears in the partition exactly once
    (``set(partition.keys()) == set(V)`` — no missing, no extra nodes);
  * each node maps to exactly one community id (a ``dict`` guarantees this; the
    values are plain integers);
  * the community sizes sum to ``|V|`` (a ``Counter`` over the partition values
    totals ``len(V)``); and
  * the community ids are contiguous ``0..k-1`` (matches the implementation
    guarantee in :func:`louvain_partition`).

This is exercised at two complementary layers so completeness is validated both
*through the bipartite projection* and *independently of it*:

  1. Random bipartite ``(claimant_id, employer_id)`` edge lists are projected
     with :func:`project_claimant_graph` and partitioned with
     :func:`louvain_partition`; the same edge lists are also run end-to-end
     through :func:`detect_communities`.
  2. Random ``networkx`` graphs of ``N`` nodes are built directly (an explicit
     node set plus a random subset of possible edges) and partitioned with
     :func:`louvain_partition`, testing completeness with no projection in the
     way.

Edge cases covered explicitly: the empty graph (0 nodes -> empty partition, 0
communities), the single-node graph (one community covering it), the
fully-connected graph, and the fully-disconnected graph.

As an easy bonus (not required by P3 itself) we also assert that disconnected
components are never merged — nodes in different connected components always land
in different communities.

The graph-service directory contains a hyphen, so it is not importable as a
package; we add it to ``sys.path`` here (as ``tests/unit/test_community.py`` does).

Property 3: Community partition completeness. Validates: Requirements 5.10
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import networkx as nx
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# --- Make the graph-service module importable --------------------------------
_SERVICE_DIR = Path(__file__).resolve().parents[2] / "services" / "graph-service"
if str(_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVICE_DIR))

from community import (  # noqa: E402
    detect_communities,
    louvain_partition,
    project_claimant_graph,
)


# ---------------------------------------------------------------------------
# Hypothesis profile: CI runs 200 examples (design.md — Testing Strategy;
# tasks.md Notes: settings.register_profile("ci", max_examples=200, ...)).
#
# Registering is guarded so a duplicate registration (from a conftest or a
# sibling property-test module already loaded in the same session) does not
# raise. We always (re)load the "ci" profile so this file runs 200 examples.
# ---------------------------------------------------------------------------
try:
    settings.register_profile(
        "ci", max_examples=200, suppress_health_check=[HealthCheck.too_slow]
    )
except Exception:  # noqa: BLE001 - already registered elsewhere
    pass
settings.load_profile("ci")


# ---------------------------------------------------------------------------
# The P3 completeness assertion — reused by every property below.
# ---------------------------------------------------------------------------


def _assert_complete_partition(partition: dict, nodes) -> None:
    """Assert the P3 completeness invariant for ``partition`` over ``nodes``.

    * keys cover exactly the node set (no missing / extra);
    * every value is a single ``int`` community id;
    * community sizes sum to ``|V|``;
    * community ids are contiguous ``0..k-1``.
    """
    node_set = set(nodes)

    # Every node partitioned, no missing / extra.
    assert set(partition.keys()) == node_set, (
        f"partition keys {set(partition.keys())} != node set {node_set}"
    )

    # Each node maps to exactly one integer community id.
    assert all(isinstance(cid, int) for cid in partition.values()), (
        "community ids must be ints"
    )

    # Community sizes sum to |V|.
    sizes = Counter(partition.values())
    assert sum(sizes.values()) == len(node_set), (
        f"community sizes {dict(sizes)} sum to {sum(sizes.values())} != {len(node_set)}"
    )

    # Community ids are contiguous 0..k-1.
    if node_set:
        ids = sorted(sizes.keys())
        assert ids == list(range(len(ids))), (
            f"community ids {ids} are not contiguous 0..{len(ids) - 1}"
        )
    else:
        assert partition == {}


# ===========================================================================
# Strategy 1 — random bipartite (claimant_id, employer_id) edge lists.
# ===========================================================================

# Small, bounded id pools keep the projected graph dense enough to be
# interesting while remaining fast at 200 examples.
_CLAIMANT_IDS = [f"c{i}" for i in range(8)]   # c0..c7
_EMPLOYER_IDS = [f"e{i}" for i in range(5)]   # e0..e4


bipartite_edges_strategy = st.lists(
    st.tuples(st.sampled_from(_CLAIMANT_IDS), st.sampled_from(_EMPLOYER_IDS)),
    min_size=0,
    max_size=40,
)


@given(edges=bipartite_edges_strategy)
def test_P3_projection_partition_complete(edges):
    """Property 3: Community partition completeness. Validates: Requirements 5.10

    Project a random bipartite edge list to the claimant-only graph and assert
    the Louvain partition covers every claimant exactly once with contiguous
    community ids summing to |V|.
    """
    graph = project_claimant_graph(edges)
    partition = louvain_partition(graph)
    _assert_complete_partition(partition, graph.nodes())


@given(edges=bipartite_edges_strategy)
def test_P3_detect_communities_complete(edges):
    """Property 3: Community partition completeness. Validates: Requirements 5.10

    The end-to-end :func:`detect_communities` convenience (project + partition)
    is complete over the same generated bipartite edge lists. The expected node
    set is exactly the set of claimants that appear in the edges.
    """
    partition = detect_communities(edges)
    expected_claimants = {claimant for claimant, _employer in edges}
    _assert_complete_partition(partition, expected_claimants)


# ===========================================================================
# Strategy 2 — random networkx graphs of N nodes, built directly.
# ===========================================================================


@st.composite
def random_graph_strategy(draw, min_nodes: int = 0, max_nodes: int = 15):
    """Generate a random ``networkx.Graph`` on ``N`` nodes (``0..max_nodes``).

    Nodes are the integers ``0..N-1`` (explicitly added, so isolated nodes are
    preserved). A random subset of the possible undirected edges is added, so
    the graph ranges from fully-disconnected to fully-connected.
    """
    n = draw(st.integers(min_value=min_nodes, max_value=max_nodes))
    graph = nx.Graph()
    graph.add_nodes_from(range(n))

    # All possible undirected edges (i < j); pick a random subset.
    possible = [(i, j) for i in range(n) for j in range(i + 1, n)]
    if possible:
        chosen = draw(
            st.lists(
                st.sampled_from(possible),
                unique=True,
                max_size=len(possible),
            )
        )
        graph.add_edges_from(chosen)
    return graph


@given(graph=random_graph_strategy())
def test_P3_random_graph_partition_complete(graph):
    """Property 3: Community partition completeness. Validates: Requirements 5.10

    Independently of the bipartite projection, a random ``networkx`` graph of N
    nodes partitions completely: every node covered exactly once, sizes sum to
    N, contiguous ids.
    """
    partition = louvain_partition(graph)
    _assert_complete_partition(partition, graph.nodes())


@given(graph=random_graph_strategy(min_nodes=1, max_nodes=15))
def test_P3_disconnected_components_never_merged(graph):
    """Property 3 (bonus): disconnected components are never merged.

    Not required by P3 completeness itself, but easy to verify: every Louvain
    community lies entirely within a single connected component, so nodes in
    different components always receive different community ids.
    """
    partition = louvain_partition(graph)
    # Also complete, as a sanity check alongside the bonus property.
    _assert_complete_partition(partition, graph.nodes())

    # Map each node to its connected-component index.
    component_of: dict = {}
    for idx, component in enumerate(nx.connected_components(graph)):
        for node in component:
            component_of[node] = idx

    # For each community, all members must share one connected component.
    members_by_community: dict = {}
    for node, cid in partition.items():
        members_by_community.setdefault(cid, []).append(node)
    for cid, members in members_by_community.items():
        comps = {component_of[node] for node in members}
        assert len(comps) == 1, (
            f"community {cid} spans multiple connected components {comps}"
        )


# ===========================================================================
# Edge cases — enumerated explicitly (not property-generated).
# ===========================================================================


def test_P3_empty_graph_empty_partition():
    """Property 3: Community partition completeness. Validates: Requirements 5.10

    The empty graph (0 nodes) yields an empty partition (0 communities).
    """
    graph = project_claimant_graph([])
    partition = louvain_partition(graph)
    assert partition == {}
    _assert_complete_partition(partition, [])


def test_P3_single_node_one_community():
    """Property 3: Community partition completeness. Validates: Requirements 5.10

    A single node forms exactly one community (id 0) covering it.
    """
    graph = nx.Graph()
    graph.add_node("only")
    partition = louvain_partition(graph)
    _assert_complete_partition(partition, ["only"])
    assert partition == {"only": 0}


def test_P3_fully_connected_graph():
    """Property 3: Community partition completeness. Validates: Requirements 5.10

    A fully-connected graph partitions completely (every node covered once,
    contiguous ids summing to N).
    """
    graph = nx.complete_graph(6)
    partition = louvain_partition(graph)
    _assert_complete_partition(partition, graph.nodes())


def test_P3_fully_disconnected_graph():
    """Property 3: Community partition completeness. Validates: Requirements 5.10

    A fully-disconnected graph (N isolated nodes) partitions completely — each
    node in its own community, all ids contiguous and covering every node.
    """
    graph = nx.Graph()
    graph.add_nodes_from(range(6))
    partition = louvain_partition(graph)
    _assert_complete_partition(partition, graph.nodes())
    # Every node isolated => N distinct communities.
    assert len(set(partition.values())) == 6
