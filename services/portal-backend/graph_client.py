"""
services/portal-backend/graph_client.py
========================================
Graph Service client + fraud-ring network composition for the investigator
dashboard (Req 22.2 surface, fed by the Req 5.1 bipartite claimant/employer
graph and the Req 5.5 community clustering).

The investigator claim-detail view renders a fraud-ring *network graph* — an
employer hub surrounded by the claimant cohort, with claimants in the same
detected community coloured as a cluster. There is no persisted node/edge table
in PostgreSQL, so this data is fetched LIVE from the Graph Service:

  * ``GET  /graph/cohort/{employer_bn}``  — the employer's 24-month claimant
    cohort WITH attributes (postal FSA, surname hash, insurable hours, dates).
    These become the claimant NODES; the shared employer, FSA and surname are
    the EDGE bases (Req 5.1).
  * ``POST /graph/community-detect`` with ``employer_bns=[bn]`` — partitions the
    cohort into communities (Req 5.5 / Property P3). The partition colours the
    nodes so an investigator can see the connected cluster.

Design decisions (grounded, honest degradation):

* **No fabricated data.** When the Graph Service is unavailable, returns no
  cohort, or the claim has no employer BN, the composed graph reports
  ``available=False`` with a ``reason`` and an empty node/edge set. The UI shows
  a "no network data" state rather than inventing a ring (grounding rule 1).
* **The client never raises** on a Graph Service failure — every call degrades
  to an ``UNAVAILABLE`` result, mirroring the fraud-ring engine's client.
* **PII masking is applied by the caller** (the endpoint) per role (Req 22.6);
  the pure ``build_graph`` helper works on already-pseudonymous identifiers
  (sin_hash / surname_hash) and never emits raw PII.

Requirements: 5.1, 5.5, 19.7, 22.2, 22.6
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ===========================================================================
# Result types
# ===========================================================================


class GraphCallStatus(str, Enum):
    """Outcome of a Graph Service call from the BFF's view."""

    OK = "OK"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass
class CohortFetchResult:
    """Result of ``GET /graph/cohort/{employer_bn}``.

    ``members`` is the list of raw cohort-member dicts as returned by the Graph
    Service (each carrying ``sin_hash, employer_bn, postal_fsa, surname_hash,
    insurable_hours, separation_date, roe_submission_ts, claim_id``). On any
    failure ``status`` is ``UNAVAILABLE`` and ``members`` is empty.
    """

    status: GraphCallStatus
    members: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CommunityResult:
    """Result of ``POST /graph/community-detect``.

    ``partition`` maps claimant id (sin_hash) -> integer community id.
    """

    status: GraphCallStatus
    partition: dict[str, int] = field(default_factory=dict)
    num_communities: int = 0


# ===========================================================================
# Graph Service client
# ===========================================================================


class PortalGraphServiceClient:
    """Async client for the two Graph Service reads the dashboard needs.

    The HTTP transport is injected (an ``httpx.AsyncClient`` in production, a
    fake in tests). A per-call timeout bounds latency. No method raises on a
    Graph Service failure — each returns an ``UNAVAILABLE`` result instead, so
    the investigator view degrades to a "no network data" state.
    """

    def __init__(
        self,
        base_url: str,
        http_client: Any,
        *,
        timeout_seconds: float = 5.0,
        bearer_token: Optional[str] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = http_client
        self._timeout = timeout_seconds
        # Internal GCKey Bearer presented to the auth-gated Graph Service
        # (Req 19.7). ``None`` (unit tests) => no Authorization header.
        self._headers = (
            {"Authorization": f"Bearer {bearer_token}"} if bearer_token else None
        )

    async def fetch_cohort(self, employer_bn: str) -> CohortFetchResult:
        """Fetch the employer's 24-month cohort WITH attributes (Req 5.1)."""
        url = f"{self._base_url}/graph/cohort/{employer_bn}"
        try:
            resp = await self._http.get(
                url, timeout=self._timeout, headers=self._headers
            )
        except Exception:  # noqa: BLE001 - transport failure -> UNAVAILABLE
            return CohortFetchResult(status=GraphCallStatus.UNAVAILABLE)

        if not _ok(resp):
            return CohortFetchResult(status=GraphCallStatus.UNAVAILABLE)
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            return CohortFetchResult(status=GraphCallStatus.UNAVAILABLE)

        if not isinstance(body, dict):
            return CohortFetchResult(status=GraphCallStatus.UNAVAILABLE)

        raw_members = body.get("members")
        members = (
            [m for m in raw_members if isinstance(m, dict)]
            if isinstance(raw_members, list)
            else []
        )
        return CohortFetchResult(status=GraphCallStatus.OK, members=members)

    async def community_detect(self, employer_bn: str, *, seed: int = 42) -> CommunityResult:
        """Partition the employer cohort into communities (Req 5.5 / P3)."""
        url = f"{self._base_url}/graph/community-detect"
        payload = {"seed": seed, "employer_bns": [employer_bn]}
        try:
            resp = await self._http.post(
                url, json=payload, timeout=self._timeout, headers=self._headers
            )
        except Exception:  # noqa: BLE001 - transport failure -> UNAVAILABLE
            return CommunityResult(status=GraphCallStatus.UNAVAILABLE)

        if not _ok(resp):
            return CommunityResult(status=GraphCallStatus.UNAVAILABLE)
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            return CommunityResult(status=GraphCallStatus.UNAVAILABLE)

        if not isinstance(body, dict):
            return CommunityResult(status=GraphCallStatus.UNAVAILABLE)

        raw_partition = body.get("partition") or {}
        partition: dict[str, int] = {}
        if isinstance(raw_partition, dict):
            for key, value in raw_partition.items():
                try:
                    partition[str(key)] = int(value)
                except (ValueError, TypeError):
                    continue
        num = body.get("num_communities")
        num_communities = (
            int(num)
            if isinstance(num, (int, float))
            else len(set(partition.values()))
        )
        return CommunityResult(
            status=GraphCallStatus.OK,
            partition=partition,
            num_communities=num_communities,
        )


def _ok(resp: Any) -> bool:
    """True when the response status code is 2xx (defaults to 200 if absent)."""
    status_code = getattr(resp, "status_code", 200)
    return 200 <= int(status_code) < 300


# ===========================================================================
# Pure graph composition (unit-testable, no I/O)
# ===========================================================================

#: A stable short label for a claimant node derived from its sin_hash. Real
#: deployments join an identity store; here we keep the contract pseudonymous
#: (never raw PII, Req 22.6) while giving investigators a stable handle.
def _claimant_label(sin_hash: str) -> str:
    digest = hashlib.sha256(sin_hash.encode("utf-8")).hexdigest()
    return f"Claimant {digest[:6].upper()}"


def _shared_attr_edges(
    members: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Derive claimant<->claimant edges from shared surname / postal FSA (Req 5.5).

    Two claimants are linked when they share a non-empty ``surname_hash`` (kind
    ``SHARED_SURNAME``) or the first-3 postal characters ``postal_fsa`` (kind
    ``SHARED_ADDRESS``). These are the family/address ties the fraud-ring
    community check keys on. Edges are undirected and de-duplicated.
    """
    edges: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def _group(attr: str, kind: str) -> None:
        buckets: dict[str, list[str]] = {}
        for m in members:
            value = m.get(attr)
            sin = m.get("sin_hash")
            if not value or not sin:
                continue
            buckets.setdefault(str(value), []).append(str(sin))
        for members_with_value in buckets.values():
            uniq = sorted(set(members_with_value))
            for i in range(len(uniq)):
                for j in range(i + 1, len(uniq)):
                    a, b = uniq[i], uniq[j]
                    key = (a, b, kind)
                    if key in seen:
                        continue
                    seen.add(key)
                    edges.append({"source": a, "target": b, "kind": kind})

    _group("surname_hash", "SHARED_SURNAME")
    _group("postal_fsa", "SHARED_ADDRESS")
    return edges


def build_graph(
    *,
    employer_bn: Optional[str],
    cohort_members: list[dict[str, Any]],
    partition: dict[str, int],
    cohort_status: GraphCallStatus,
) -> dict[str, Any]:
    """Compose a fraud-ring network graph from a Graph Service cohort.

    Nodes:
      * one EMPLOYER hub node (id = employer_bn), plus
      * one CLAIMANT node per cohort member (id = sin_hash), carrying its
        community id (from ``partition``) and shared attributes.

    Edges:
      * an ``EMPLOYED_BY`` edge from each claimant to the employer hub, plus
      * ``SHARED_SURNAME`` / ``SHARED_ADDRESS`` edges between claimants who
        share a surname hash or postal FSA (Req 5.5).

    A claimant is marked ``flagged=True`` when it belongs to a community shared
    by two or more members (a connected cluster, not a singleton). ``available``
    is ``False`` — with a ``reason`` and empty nodes/edges — when there is no
    employer BN or the Graph Service returned no usable cohort, so the UI never
    renders a fabricated network (grounding rule 1).
    """
    if not employer_bn:
        return _unavailable("NO_EMPLOYER")
    if cohort_status is not GraphCallStatus.OK:
        return _unavailable("GRAPH_SERVICE_UNAVAILABLE", employer_bn=employer_bn)
    if not cohort_members:
        return _unavailable("EMPTY_COHORT", employer_bn=employer_bn)

    # Community sizes: a cluster is a community with >= 2 members.
    community_sizes: dict[int, int] = {}
    for m in cohort_members:
        sin = m.get("sin_hash")
        if not sin:
            continue
        community = partition.get(str(sin))
        if community is None:
            continue
        community_sizes[community] = community_sizes.get(community, 0) + 1

    nodes: list[dict[str, Any]] = [
        {
            "id": employer_bn,
            "type": "EMPLOYER",
            "label": f"Employer {employer_bn}",
            "community": None,
            "flagged": False,
        }
    ]
    edges: list[dict[str, str]] = []

    for m in cohort_members:
        sin = m.get("sin_hash")
        if not sin:
            continue
        sin = str(sin)
        community = partition.get(sin)
        in_cluster = community is not None and community_sizes.get(community, 0) >= 2
        nodes.append(
            {
                "id": sin,
                "type": "CLAIMANT",
                "label": _claimant_label(sin),
                "community": community,
                "flagged": bool(in_cluster),
                "postal_fsa": m.get("postal_fsa"),
                "insurable_hours": m.get("insurable_hours"),
                "separation_date": m.get("separation_date"),
                "claim_id": m.get("claim_id"),
            }
        )
        edges.append({"source": sin, "target": employer_bn, "kind": "EMPLOYED_BY"})

    edges.extend(_shared_attr_edges(cohort_members))

    num_communities = len({c for c in partition.values()}) if partition else 0
    cluster_count = sum(1 for size in community_sizes.values() if size >= 2)

    return {
        "available": True,
        "employer_bn": employer_bn,
        "cohort_size": len(cohort_members),
        "num_communities": num_communities,
        "cluster_count": cluster_count,
        "nodes": nodes,
        "edges": edges,
    }


def _unavailable(reason: str, *, employer_bn: Optional[str] = None) -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "employer_bn": employer_bn,
        "cohort_size": 0,
        "num_communities": 0,
        "cluster_count": 0,
        "nodes": [],
        "edges": [],
    }


__all__ = [
    "GraphCallStatus",
    "CohortFetchResult",
    "CommunityResult",
    "PortalGraphServiceClient",
    "build_graph",
]
