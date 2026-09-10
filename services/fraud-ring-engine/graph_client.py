"""
services/fraud-ring-engine/graph_client.py
============================================
Graph Service client for the Fraud Ring Engine (task 13.2).

The Fraud Ring Engine calls the Graph Service to (a) upsert the current claim's
claimant/employer edge and (b) partition the employer's 24-month claimant cohort
into communities. This module wraps those two HTTP calls behind a small,
dependency-injected client so tests can supply a fake transport (no live
network) and so failures degrade gracefully.

Endpoints (see ``services/graph-service/main.py``):
  * ``POST /graph/construct``          — upsert claimant/employer/edge.
  * ``POST /graph/community-detect``   — partition the cohort. May be called
    with an explicit ``edges`` list (when the engine already holds the cohort in
    memory) or with ``employer_bns`` (server reads edges from Neo4j).

Cohort membership (the ``CohortClaimant`` records consumed by
``community_analysis``) is supplied to the engine alongside the graph edges. The
Graph Service community-detect response returns a ``partition`` mapping; the
engine uses that to confirm the cohort is a connected community and attaches the
community id count to evidence. The per-claimant attributes (postal FSA, surname
hash, insurable hours) travel with the cohort payload.

On any Graph Service failure (transport error, non-2xx, malformed body) the
client returns a ``GraphUnavailable`` result rather than raising, so the engine
can skip the community/hours-band checks and still publish partial results
(Requirement 5.9).

Requirements: 5.1, 5.5, 5.9
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ===========================================================================
# Result types
# ===========================================================================


class GraphCallStatus(str, Enum):
    """Outcome of a Graph Service call from the engine's view."""

    OK = "OK"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass
class ConstructResult:
    """Result of a ``POST /graph/construct`` call."""

    status: GraphCallStatus
    body: Optional[dict[str, Any]] = None


@dataclass
class CommunityResult:
    """Result of a ``POST /graph/community-detect`` call.

    ``partition`` maps claimant id -> integer community id (as returned by the
    Graph Service). ``num_communities`` is the distinct community count.
    """

    status: GraphCallStatus
    partition: dict[str, int] = field(default_factory=dict)
    num_communities: int = 0


# ===========================================================================
# Graph Service client
# ===========================================================================


class GraphServiceClient:
    """Async client for the Graph Service construct / community-detect endpoints.

    The HTTP transport is injected (an ``httpx.AsyncClient`` in production, a
    fake in tests). A per-call timeout bounds latency so the engine can stay
    within its 800 ms budget. No method raises on a Graph Service failure — each
    returns an ``UNAVAILABLE`` result instead.
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

    async def construct(self, payload: dict[str, Any]) -> ConstructResult:
        """Upsert the current claim's claimant/employer/edge into the graph."""
        url = f"{self._base_url}/graph/construct"
        try:
            resp = await self._http.post(
                url, json=payload, timeout=self._timeout, headers=self._headers
            )
        except Exception:  # noqa: BLE001 - transport failure -> UNAVAILABLE
            return ConstructResult(status=GraphCallStatus.UNAVAILABLE)

        if not _ok(resp):
            return ConstructResult(status=GraphCallStatus.UNAVAILABLE)
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = None
        return ConstructResult(status=GraphCallStatus.OK, body=body)

    async def community_detect(
        self,
        *,
        edges: Optional[list[tuple[str, str]]] = None,
        employer_bns: Optional[list[str]] = None,
        seed: int = 42,
    ) -> CommunityResult:
        """Partition the employer cohort into communities.

        Provide either ``edges`` (explicit bipartite claimant/employer pairs) or
        ``employer_bns`` (server reads the cohort from the graph). ``edges``
        takes precedence when both are given, matching the Graph Service.
        """
        url = f"{self._base_url}/graph/community-detect"
        payload: dict[str, Any] = {"seed": seed}
        if edges is not None:
            payload["edges"] = [list(e) for e in edges]
        if employer_bns is not None:
            payload["employer_bns"] = employer_bns

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
            int(num) if isinstance(num, (int, float)) else len(set(partition.values()))
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


__all__ = [
    "GraphCallStatus",
    "ConstructResult",
    "CommunityResult",
    "GraphServiceClient",
]
