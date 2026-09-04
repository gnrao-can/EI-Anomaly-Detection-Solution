"""
tests/unit/test_community.py
============================
Unit tests for the Graph Service community detection (task 9.2):

  - project_claimant_graph: claimants sharing an employer are connected, with an
    edge weight equal to the number of shared employers; isolated claimants are
    preserved as nodes.
  - louvain_partition: partitions ALL nodes with no overlap, community sizes sum
    to N (the Property 3 completeness precondition), disconnected components land
    in separate communities, and results are deterministic given a seed.
  - load_graphsage: returns a working embedder (deterministic fallback when the
    torch / torch_geometric stack or the pre-trained weights are unavailable).
  - fetch_shared_employer_edges + /graph/community-detect wiring via the fake
    driver.

The graph-service directory contains a hyphen, so it is not importable as a
package. We add it to ``sys.path`` here.

Requirements: 5.5, 5.10
"""

from __future__ import annotations

import os
import sys

import pytest

# --- Make the graph-service module importable --------------------------------
_SERVICE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services",
    "graph-service",
)
if _SERVICE_DIR not in sys.path:
    sys.path.insert(0, _SERVICE_DIR)

from community import (  # noqa: E402
    GraphSAGEEmbedder,
    detect_communities,
    load_graphsage,
    louvain_partition,
    project_claimant_graph,
)
from graph_repo import FakeAsyncDriver, GraphRepository  # noqa: E402


# ===========================================================================
# project_claimant_graph
# ===========================================================================


class TestProjection:
    def test_shared_employer_connects_claimants(self):
        edges = [("c1", "E1"), ("c2", "E1")]
        g = project_claimant_graph(edges)
        assert g.has_edge("c1", "c2")
        assert g["c1"]["c2"]["weight"] == 1

    def test_no_shared_employer_no_edge(self):
        edges = [("c1", "E1"), ("c2", "E2")]
        g = project_claimant_graph(edges)
        assert set(g.nodes()) == {"c1", "c2"}
        assert not g.has_edge("c1", "c2")

    def test_weight_counts_shared_employers(self):
        # c1 and c2 share two employers -> weight 2.
        edges = [("c1", "E1"), ("c2", "E1"), ("c1", "E2"), ("c2", "E2")]
        g = project_claimant_graph(edges)
        assert g["c1"]["c2"]["weight"] == 2

    def test_isolated_claimant_preserved_as_node(self):
        # c3 works at an employer with no one else -> isolated node, still present.
        edges = [("c1", "E1"), ("c2", "E1"), ("c3", "E9")]
        g = project_claimant_graph(edges)
        assert "c3" in g.nodes()
        assert g.degree("c3") == 0

    def test_triangle_from_three_coworkers(self):
        edges = [("c1", "E1"), ("c2", "E1"), ("c3", "E1")]
        g = project_claimant_graph(edges)
        assert g.number_of_nodes() == 3
        assert g.number_of_edges() == 3  # fully connected triangle

    def test_no_self_edges(self):
        # Same claimant listed twice under the same employer must not self-loop.
        edges = [("c1", "E1"), ("c1", "E1")]
        g = project_claimant_graph(edges)
        assert not g.has_edge("c1", "c1")
        assert list(g.nodes()) == ["c1"]

    def test_empty_edges_empty_graph(self):
        g = project_claimant_graph([])
        assert g.number_of_nodes() == 0


# ===========================================================================
# louvain_partition — P3 completeness precondition
# ===========================================================================


def _assert_complete_partition(partition, expected_nodes):
    """Every node appears exactly once; sizes sum to N; no overlaps."""
    assert set(partition.keys()) == set(expected_nodes)
    assert len(partition) == len(expected_nodes)
    sizes = {}
    for cid in partition.values():
        sizes[cid] = sizes.get(cid, 0) + 1
    assert sum(sizes.values()) == len(expected_nodes)


class TestLouvainPartition:
    def test_empty_graph_empty_partition(self):
        g = project_claimant_graph([])
        assert louvain_partition(g) == {}

    def test_partition_covers_all_nodes(self):
        edges = [("c1", "E1"), ("c2", "E1"), ("c3", "E2"), ("c4", "E2"), ("c5", "E5")]
        g = project_claimant_graph(edges)
        partition = louvain_partition(g)
        _assert_complete_partition(partition, ["c1", "c2", "c3", "c4", "c5"])

    def test_community_sizes_sum_to_n(self):
        edges = [("c1", "E1"), ("c2", "E1"), ("c3", "E1"), ("c4", "E2"), ("c5", "E2")]
        g = project_claimant_graph(edges)
        partition = louvain_partition(g)
        sizes = {}
        for cid in partition.values():
            sizes[cid] = sizes.get(cid, 0) + 1
        assert sum(sizes.values()) == g.number_of_nodes()

    def test_disconnected_components_separate_communities(self):
        # Two cliques that share no employer must be in different communities.
        edges = [
            ("a1", "EA"), ("a2", "EA"), ("a3", "EA"),  # component A
            ("b1", "EB"), ("b2", "EB"), ("b3", "EB"),  # component B
        ]
        g = project_claimant_graph(edges)
        partition = louvain_partition(g)
        a_comms = {partition["a1"], partition["a2"], partition["a3"]}
        b_comms = {partition["b1"], partition["b2"], partition["b3"]}
        # Each clique is internally cohesive and disjoint from the other.
        assert a_comms.isdisjoint(b_comms)

    def test_isolated_node_gets_its_own_community(self):
        edges = [("c1", "E1"), ("c2", "E1"), ("lonely", "E9")]
        g = project_claimant_graph(edges)
        partition = louvain_partition(g)
        assert partition["lonely"] not in {partition["c1"], partition["c2"]}

    def test_deterministic_given_seed(self):
        edges = [
            ("c1", "E1"), ("c2", "E1"), ("c3", "E1"),
            ("c4", "E2"), ("c5", "E2"),
            ("c3", "E2"),  # bridge node
        ]
        g = project_claimant_graph(edges)
        p1 = louvain_partition(g, seed=7)
        p2 = louvain_partition(g, seed=7)
        assert p1 == p2

    def test_community_ids_are_contiguous(self):
        edges = [("c1", "E1"), ("c2", "E2"), ("c3", "E3")]
        g = project_claimant_graph(edges)
        partition = louvain_partition(g)
        ids = sorted(set(partition.values()))
        assert ids == list(range(len(ids)))


# ===========================================================================
# detect_communities (convenience) — same guarantees end-to-end
# ===========================================================================


class TestDetectCommunities:
    def test_partition_complete_from_edges(self):
        edges = [("c1", "E1"), ("c2", "E1"), ("c3", "E2")]
        partition = detect_communities(edges)
        _assert_complete_partition(partition, ["c1", "c2", "c3"])

    def test_empty_edges(self):
        assert detect_communities([]) == {}


# ===========================================================================
# GraphSAGE loading + deterministic fallback
# ===========================================================================


class TestGraphSAGE:
    def test_load_returns_embedder(self):
        embedder = load_graphsage("models/does_not_exist.pt")
        assert isinstance(embedder, GraphSAGEEmbedder)

    def test_missing_weights_uses_fallback(self):
        embedder = load_graphsage("models/definitely_missing.pt")
        assert embedder.using_fallback is True

    def test_fallback_embeddings_are_deterministic(self):
        embedder = load_graphsage("models/definitely_missing.pt")
        g = project_claimant_graph([("c1", "E1"), ("c2", "E1")])
        e1 = embedder.embed_nodes(g)
        e2 = embedder.embed_nodes(g)
        assert e1 == e2
        # One embedding vector per node, all of equal length.
        assert set(e1.keys()) == {"c1", "c2"}
        lengths = {len(v) for v in e1.values()}
        assert len(lengths) == 1

    def test_embeddings_differ_per_node(self):
        embedder = load_graphsage("models/definitely_missing.pt")
        g = project_claimant_graph([("c1", "E1"), ("c2", "E1")])
        emb = embedder.embed_nodes(g)
        assert emb["c1"] != emb["c2"]


# ===========================================================================
# Repository fetch_shared_employer_edges (fake driver) + endpoint wiring
# ===========================================================================


@pytest.fixture()
def driver() -> FakeAsyncDriver:
    return FakeAsyncDriver()


@pytest.fixture()
def repo(driver: FakeAsyncDriver) -> GraphRepository:
    return GraphRepository(driver=driver)


async def test_fetch_shared_employer_edges_all(repo):
    await repo.add_employed_by_edge("c1", "E1", {"claim_id": "1"})
    await repo.add_employed_by_edge("c2", "E1", {"claim_id": "2"})
    edges = await repo.fetch_shared_employer_edges()
    assert set(edges) == {("c1", "E1"), ("c2", "E1")}


async def test_fetch_shared_employer_edges_filtered_by_bn(repo):
    await repo.add_employed_by_edge("c1", "E1", {"claim_id": "1"})
    await repo.add_employed_by_edge("c2", "E2", {"claim_id": "2"})
    edges = await repo.fetch_shared_employer_edges(["E1"])
    assert edges == [("c1", "E1")]


async def test_fetch_then_partition_is_complete(repo):
    await repo.add_employed_by_edge("c1", "E1", {})
    await repo.add_employed_by_edge("c2", "E1", {})
    await repo.add_employed_by_edge("c3", "E2", {})
    edges = await repo.fetch_shared_employer_edges()
    partition = detect_communities(edges)
    _assert_complete_partition(partition, ["c1", "c2", "c3"])
