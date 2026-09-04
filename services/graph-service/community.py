"""
services/graph-service/community.py
===================================
Community detection for the Fraud Ring analytics of the Graph Service.

The design (see design.md, "Fraud Ring Engine" and "Graph Service") specifies
community detection over the **projected claimant-only graph** built from the
bipartite claimant-employer graph:

    Two claimants are connected by an edge when they share at least one
    employer; the edge weight is the number of shared employers.

The primary partitioner is the **Louvain** algorithm
(``networkx.algorithms.community.louvain_communities``), which is guaranteed to
return a partition covering *every* node in exactly one community — the
precondition for Property 3 (community partition completeness).

A **GraphSAGE** (2-layer, hidden dim 64) GNN, implemented in PyTorch Geometric,
*refines / embeds* the graph on top of the Louvain partition. Because torch /
torch_geometric (and the pre-trained ``models/graphsage_v1.pt`` weights) are
frequently unavailable in constrained/sandbox environments — and are pinned to
CPU-heavy versions — this module degrades gracefully: when the deep-learning
stack cannot be imported or the weights are missing, a **deterministic
fallback** produces stable node embeddings and, if ever needed, communities.
The Louvain path remains the authoritative partitioner in all cases.

Requirements: 5.5, 5.10
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Iterable, Optional, Sequence

import networkx as nx
from networkx.algorithms.community import louvain_communities

# ---------------------------------------------------------------------------
# Optional deep-learning stack (torch / torch_geometric).
# These are pinned to CPU builds that may be unavailable in the sandbox, so all
# imports are guarded. When unavailable we fall back to a deterministic path.
# ---------------------------------------------------------------------------
try:  # pragma: no cover - availability depends on the runtime environment
    import torch  # type: ignore[import-not-found]
    from torch import nn  # type: ignore[import-not-found]

    _HAS_TORCH = True
except Exception:  # noqa: BLE001 - any import failure means "unavailable"
    torch = None  # type: ignore[assignment]
    nn = object  # type: ignore[assignment,misc]
    _HAS_TORCH = False

try:  # pragma: no cover - availability depends on the runtime environment
    from torch_geometric.nn import SAGEConv  # type: ignore[import-not-found]

    _HAS_PYG = True
except Exception:  # noqa: BLE001
    SAGEConv = None  # type: ignore[assignment,misc]
    _HAS_PYG = False


#: Default location of the pre-trained GraphSAGE weights.
DEFAULT_GRAPHSAGE_WEIGHTS = "models/graphsage_v1.pt"

#: GraphSAGE architecture constants (from the design: 2-layer, hidden dim 64).
GRAPHSAGE_HIDDEN_DIM = 64
GRAPHSAGE_NUM_LAYERS = 2
#: Node input feature dimension used by the fallback embedder and the model.
GRAPHSAGE_IN_DIM = 8
#: Output embedding dimension.
GRAPHSAGE_OUT_DIM = 32

#: Fixed seed so Louvain (and the fallback) are deterministic for a given graph.
DEFAULT_SEED = 42


# ===========================================================================
# Claimant-only graph projection
# ===========================================================================


def project_claimant_graph(edges: Iterable[Sequence[Any]]) -> nx.Graph:
    """Project the bipartite claimant-employer edges to a claimant-only graph.

    Parameters
    ----------
    edges:
        An iterable of ``(claimant_id, employer_id)`` pairs (extra tuple
        elements, if present, are ignored). These are the ``EMPLOYED_BY``
        edges of the bipartite graph.

    Returns
    -------
    networkx.Graph
        An undirected graph whose nodes are *claimant* ids. Two claimants are
        joined by an edge iff they share at least one employer; the edge
        ``weight`` attribute is the number of employers they share.

        Every claimant appearing in ``edges`` becomes a node — including
        claimants that share no employer with anyone else (they remain
        isolated nodes). This guarantees the downstream Louvain partition can
        cover *all* claimants (P3 completeness precondition).

    Notes
    -----
    A self-pairing (a claimant with itself) is never created. Claimant ids are
    treated as opaque, hashable values (typically ``sin_hash`` strings).
    """
    # employer -> set of claimants employed there
    employer_to_claimants: dict[Any, set[Any]] = {}
    all_claimants: list[Any] = []
    seen: set[Any] = set()

    for edge in edges:
        if edge is None or len(edge) < 2:
            continue
        claimant, employer = edge[0], edge[1]
        if claimant not in seen:
            seen.add(claimant)
            all_claimants.append(claimant)
        employer_to_claimants.setdefault(employer, set()).add(claimant)

    graph = nx.Graph()
    # Add every claimant as a node first so isolated claimants are preserved.
    graph.add_nodes_from(all_claimants)

    # For each employer, connect every pair of its claimants, accumulating the
    # shared-employer count as the edge weight.
    for claimants in employer_to_claimants.values():
        members = sorted(claimants, key=_stable_key)
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                if graph.has_edge(a, b):
                    graph[a][b]["weight"] += 1
                else:
                    graph.add_edge(a, b, weight=1)

    return graph


# ===========================================================================
# Louvain partitioning
# ===========================================================================


def louvain_partition(
    graph: nx.Graph,
    *,
    seed: int = DEFAULT_SEED,
    resolution: float = 1.0,
) -> dict[Any, int]:
    """Run Louvain community detection over ``graph``.

    Parameters
    ----------
    graph:
        A claimant-only projection (typically from :func:`project_claimant_graph`).
    seed:
        Random seed for deterministic results across runs on the same graph.
    resolution:
        Louvain resolution parameter (higher -> more, smaller communities).

    Returns
    -------
    dict
        A mapping ``{claimant_id: community_id}`` where ``community_id`` is a
        contiguous integer in ``[0, num_communities)``.

    Guarantees (P3 completeness precondition)
    -----------------------------------------
    * **Every** node of ``graph`` appears in the result exactly once.
    * Community ids partition the node set — the sum of community sizes equals
      the number of nodes and no node belongs to more than one community.
    * An empty graph yields an empty mapping.
    * Disconnected components are never merged: nodes in different connected
      components always receive different community ids.
    """
    if graph.number_of_nodes() == 0:
        return {}

    communities = louvain_communities(
        graph,
        weight="weight",
        resolution=resolution,
        seed=seed,
    )

    # Order communities deterministically by their smallest member so the
    # integer community ids are stable for a given graph.
    ordered = sorted(
        (sorted(c, key=_stable_key) for c in communities),
        key=lambda members: _stable_key(members[0]),
    )

    partition: dict[Any, int] = {}
    for community_id, members in enumerate(ordered):
        for node in members:
            partition[node] = community_id

    # Defensive: Louvain must cover every node, but guard against any node that
    # (through backend quirks) was omitted — assign it a fresh singleton id.
    if len(partition) != graph.number_of_nodes():
        next_id = len(ordered)
        for node in graph.nodes():
            if node not in partition:
                partition[node] = next_id
                next_id += 1

    return partition


# ===========================================================================
# GraphSAGE (2-layer, hidden dim 64) — with deterministic fallback
# ===========================================================================


if _HAS_TORCH and _HAS_PYG:  # pragma: no cover - exercised only where PyG exists

    class GraphSAGEModel(nn.Module):  # type: ignore[misc,valid-type]
        """2-layer GraphSAGE encoder (hidden dim 64).

        Node features -> SAGEConv -> ReLU -> SAGEConv -> node embeddings. Used
        to refine/embed the claimant graph on top of the Louvain partition.
        """

        def __init__(
            self,
            in_dim: int = GRAPHSAGE_IN_DIM,
            hidden_dim: int = GRAPHSAGE_HIDDEN_DIM,
            out_dim: int = GRAPHSAGE_OUT_DIM,
        ) -> None:
            super().__init__()
            self.conv1 = SAGEConv(in_dim, hidden_dim)
            self.conv2 = SAGEConv(hidden_dim, out_dim)

        def forward(self, x, edge_index):  # noqa: D401 - torch forward
            h = self.conv1(x, edge_index)
            h = torch.relu(h)
            h = self.conv2(h, edge_index)
            return h

else:

    class GraphSAGEModel:  # type: ignore[no-redef]
        """Deterministic stand-in for the GraphSAGE encoder.

        Used when torch / torch_geometric are unavailable (e.g. the CPU-only
        sandbox). It exposes the same ``forward``-style call surface but returns
        stable, hash-derived embeddings so the endpoint keeps working.
        """

        def __init__(
            self,
            in_dim: int = GRAPHSAGE_IN_DIM,
            hidden_dim: int = GRAPHSAGE_HIDDEN_DIM,
            out_dim: int = GRAPHSAGE_OUT_DIM,
        ) -> None:
            self.in_dim = in_dim
            self.hidden_dim = hidden_dim
            self.out_dim = out_dim
            self.is_fallback = True

        def forward(self, x, edge_index):  # noqa: D401 - parity with torch model
            raise NotImplementedError(
                "GraphSAGEModel fallback does not implement tensor forward; "
                "use embed_nodes()/load_graphsage() for deterministic embeddings."
            )


class GraphSAGEEmbedder:
    """Wraps a GraphSAGE model (real or fallback) to produce node embeddings.

    ``load_graphsage`` returns one of these. When the deep-learning stack and
    the pre-trained weights are present, embeddings come from the trained
    network; otherwise a deterministic hash-based embedding is produced so that
    community detection and downstream fraud-ring analytics remain functional.
    """

    def __init__(self, model: Any, using_fallback: bool, weights_path: Optional[str]) -> None:
        self.model = model
        self.using_fallback = using_fallback
        self.weights_path = weights_path

    def embed_nodes(self, graph: nx.Graph) -> dict[Any, list[float]]:
        """Return an ``{node: embedding_vector}`` mapping for every node.

        The embedding is deterministic for a given node id (and, in the trained
        path, for a given graph), so repeated calls yield identical results.
        """
        if not self.using_fallback and _HAS_TORCH and _HAS_PYG:  # pragma: no cover
            return self._embed_with_model(graph)
        return {node: _deterministic_embedding(node, self.model.out_dim) for node in graph.nodes()}

    def _embed_with_model(self, graph: nx.Graph) -> dict[Any, list[float]]:  # pragma: no cover
        nodes = list(graph.nodes())
        index = {node: i for i, node in enumerate(nodes)}
        # Deterministic input features derived from each node id.
        feats = [
            _deterministic_embedding(node, GRAPHSAGE_IN_DIM) for node in nodes
        ]
        x = torch.tensor(feats, dtype=torch.float32)
        src: list[int] = []
        dst: list[int] = []
        for a, b in graph.edges():
            src.extend([index[a], index[b]])
            dst.extend([index[b], index[a]])
        edge_index = torch.tensor([src, dst], dtype=torch.long) if src else torch.empty(
            (2, 0), dtype=torch.long
        )
        self.model.eval()
        with torch.no_grad():
            out = self.model(x, edge_index)
        return {node: out[index[node]].tolist() for node in nodes}


def load_graphsage(path: str = DEFAULT_GRAPHSAGE_WEIGHTS) -> GraphSAGEEmbedder:
    """Load the pre-trained GraphSAGE model, with deterministic fallback.

    Parameters
    ----------
    path:
        Filesystem path to the pre-trained weights
        (default ``models/graphsage_v1.pt``).

    Returns
    -------
    GraphSAGEEmbedder
        Wraps either the trained network (when torch / torch_geometric and the
        weights are all available) or a deterministic fallback embedder.

    The function never raises on a missing dependency or missing weights file;
    it degrades to the fallback so the ``/graph/community-detect`` endpoint
    always works. Louvain remains the primary partitioner regardless.
    """
    if not (_HAS_TORCH and _HAS_PYG):
        return GraphSAGEEmbedder(GraphSAGEModel(), using_fallback=True, weights_path=None)

    import os  # local import; only reached when torch is present

    model = GraphSAGEModel()
    if path and os.path.exists(path):  # pragma: no cover - needs weights + torch
        try:
            state = torch.load(path, map_location="cpu")
            if isinstance(state, dict) and "state_dict" in state:
                state = state["state_dict"]
            model.load_state_dict(state)
            model.eval()
            return GraphSAGEEmbedder(model, using_fallback=False, weights_path=path)
        except Exception:  # noqa: BLE001 - corrupt/incompatible weights -> fallback
            return GraphSAGEEmbedder(GraphSAGEModel(), using_fallback=True, weights_path=None)

    # torch available but no weights on disk -> deterministic fallback.
    return GraphSAGEEmbedder(model, using_fallback=True, weights_path=None)


# ===========================================================================
# High-level convenience
# ===========================================================================


def detect_communities(
    edges: Iterable[Sequence[Any]],
    *,
    seed: int = DEFAULT_SEED,
) -> dict[Any, int]:
    """Project the bipartite ``edges`` and return the Louvain partition.

    This is the primary entry point used by the ``/graph/community-detect``
    endpoint: it builds the claimant-only projection and partitions it with
    Louvain, guaranteeing every claimant is placed in exactly one community.
    """
    graph = project_claimant_graph(edges)
    return louvain_partition(graph, seed=seed)


# ===========================================================================
# Internals
# ===========================================================================


def _stable_key(value: Any) -> tuple[int, str]:
    """A total, type-tolerant ordering key for heterogeneous node ids."""
    return (0, value) if isinstance(value, str) else (1, str(value))


def _deterministic_embedding(node: Any, dim: int) -> list[float]:
    """Produce a stable pseudo-embedding for ``node`` in ``[-1, 1]^dim``.

    Uses SHA-256 over the node id so the vector is reproducible across runs and
    processes without requiring any random state.
    """
    digest = hashlib.sha256(str(node).encode("utf-8")).digest()
    vec: list[float] = []
    i = 0
    while len(vec) < dim:
        # Take 2 bytes at a time, map to [-1, 1]; re-hash when we run out.
        if i + 1 >= len(digest):
            digest = hashlib.sha256(digest).digest()
            i = 0
        raw = (digest[i] << 8) | digest[i + 1]
        vec.append((raw / 65535.0) * 2.0 - 1.0)
        i += 2
    # L2-normalise for stability (avoid division by zero on the zero vector).
    norm = math.sqrt(sum(component * component for component in vec)) or 1.0
    return [component / norm for component in vec]


__all__ = [
    "project_claimant_graph",
    "louvain_partition",
    "detect_communities",
    "GraphSAGEModel",
    "GraphSAGEEmbedder",
    "load_graphsage",
    "DEFAULT_GRAPHSAGE_WEIGHTS",
    "GRAPHSAGE_HIDDEN_DIM",
    "GRAPHSAGE_NUM_LAYERS",
]
