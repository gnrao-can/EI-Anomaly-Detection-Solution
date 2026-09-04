"""
services/graph-service/main.py
==============================
FastAPI application for the Graph Service.

Endpoints
---------
``POST /graph/construct``
    Upsert the Claimant and Employer nodes and the EMPLOYED_BY edge for the
    current claim into the Neo4j graph. Idempotent — safe to re-run for the
    same claim.

``GET /healthz``
    Liveness/readiness probe that reports Neo4j connectivity.

The service holds a single :class:`GraphRepository` for its lifetime, created
during the FastAPI lifespan startup from environment-sourced settings
(``NEO4J_URI`` / ``NEO4J_USER`` / ``NEO4J_PASSWORD``) and closed on shutdown.

Requirements: 5.1
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from shared.auth import Role, require_role

from community import louvain_partition, project_claimant_graph
from config import get_settings
from graph_repo import GraphRepository


# ===========================================================================
# Request / response models
# ===========================================================================


class ConstructRequest(BaseModel):
    """Payload for ``POST /graph/construct``.

    Captures the current claim's claimant, employer and the attributes of the
    EMPLOYED_BY relationship that binds them.
    """

    sin_hash: str = Field(..., description="SHA-256 hex digest of the claimant SIN.")
    employer_bn: str = Field(..., description="15-char (or 9-digit) employer Business Number.")

    # Optional node attributes
    postal_fsa: Optional[str] = Field(default=None, description="Claimant postal FSA (first 3).")
    surname_hash: Optional[str] = Field(default=None, description="SHA-256 of normalized surname.")
    province_code: Optional[str] = Field(default=None)
    employer_name: Optional[str] = Field(default=None)
    registered_address_hash: Optional[str] = Field(default=None)

    # EMPLOYED_BY edge attributes
    separation_date: Optional[str] = Field(default=None, description="ISO date of separation.")
    roe_submission_ts: Optional[str] = Field(default=None, description="ISO datetime of ROE submit.")
    insurable_hours: Optional[float] = Field(default=None)
    claim_id: Optional[str] = Field(default=None)

    # Arbitrary extra edge attributes (kept flexible for evolving schema)
    edge_attrs: dict[str, Any] = Field(default_factory=dict)


class ConstructResponse(BaseModel):
    status: str
    sin_hash: str
    employer_bn: str


class CommunityDetectRequest(BaseModel):
    """Payload for ``POST /graph/community-detect``.

    A caller may either:
      * supply ``edges`` directly as ``[claimant_id, employer_id]`` pairs
        (used by tests and the Fraud Ring Engine when it already holds the
        cohort in memory), or
      * supply ``employer_bns`` (or nothing) so the service reads the
        EMPLOYED_BY edges for that employer cohort from the graph via the
        repository.

    When both are provided, ``edges`` takes precedence.
    """

    edges: Optional[list[tuple[str, str]]] = Field(
        default=None,
        description="Explicit bipartite (claimant_id, employer_id) edge list.",
    )
    employer_bns: Optional[list[str]] = Field(
        default=None,
        description="Employer BNs whose claimant cohort should be partitioned.",
    )
    seed: int = Field(default=42, description="Deterministic Louvain seed.")


class CommunityDetectResponse(BaseModel):
    partition: dict[str, int]
    num_communities: int


# ===========================================================================
# Application factory
# ===========================================================================


def create_app(repository: GraphRepository | None = None) -> FastAPI:
    """Create the Graph Service FastAPI app.

    Parameters
    ----------
    repository:
        Optional pre-built :class:`GraphRepository` (used by tests to inject a
        fake-driver repository). When omitted, the repository is built from
        settings during lifespan startup.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if repository is not None:
            app.state.repo = repository
        else:
            app.state.repo = GraphRepository.from_settings(get_settings())
        try:
            yield
        finally:
            await app.state.repo.close()

    app = FastAPI(title="EI-ADS Graph Service", version="0.1.0", lifespan=lifespan)

    # --- Prometheus metrics + /metrics endpoint (task 31.1) ---
    from shared.metrics import instrument_app  # noqa: E402

    instrument_app(app, kind="service")
    # --- end metrics wiring ---

    def _repo(request: Request) -> GraphRepository:
        return request.app.state.repo

    @app.post("/graph/construct", response_model=ConstructResponse)
    async def construct(
        payload: ConstructRequest,
        request: Request,
        _user: dict[str, Any] = Depends(require_role(Role.INVESTIGATOR)),
    ) -> ConstructResponse:
        repo = _repo(request)

        claimant_attrs = {
            "postal_fsa": payload.postal_fsa,
            "surname_hash": payload.surname_hash,
            "province_code": payload.province_code,
        }
        employer_attrs = {
            "name": payload.employer_name,
            "registered_address_hash": payload.registered_address_hash,
        }
        edge_attrs: dict[str, Any] = {
            "separation_date": payload.separation_date,
            "roe_submission_ts": payload.roe_submission_ts,
            "insurable_hours": payload.insurable_hours,
            "claim_id": payload.claim_id,
            "postal_fsa": payload.postal_fsa,
        }
        # Merge in any caller-supplied extra edge attributes.
        edge_attrs.update(payload.edge_attrs)

        await repo.upsert_claimant(payload.sin_hash, claimant_attrs)
        await repo.upsert_employer(payload.employer_bn, employer_attrs)
        await repo.add_employed_by_edge(payload.sin_hash, payload.employer_bn, edge_attrs)

        return ConstructResponse(
            status="ok",
            sin_hash=payload.sin_hash,
            employer_bn=payload.employer_bn,
        )

    @app.post("/graph/community-detect", response_model=CommunityDetectResponse)
    async def community_detect(
        payload: CommunityDetectRequest,
        request: Request,
        _user: dict[str, Any] = Depends(require_role(Role.INVESTIGATOR)),
    ) -> CommunityDetectResponse:
        """Partition the claimant cohort into communities.

        Louvain (on the claimant-only projection where a shared employer forms
        an edge) is the primary partitioner and guarantees every claimant is
        placed in exactly one community (Property 3 completeness precondition).
        """
        if payload.edges is not None:
            # Caller supplied the bipartite edges directly.
            edges = [(c, e) for c, e in payload.edges]
        else:
            # Read the employer cohort's EMPLOYED_BY edges from the graph.
            repo = _repo(request)
            edges = await repo.fetch_shared_employer_edges(payload.employer_bns)

        graph = project_claimant_graph(edges)
        partition = louvain_partition(graph, seed=payload.seed)
        num_communities = len(set(partition.values()))

        return CommunityDetectResponse(
            partition={str(k): v for k, v in partition.items()},
            num_communities=num_communities,
        )

    @app.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        repo = _repo(request)
        healthy = await repo.verify_connectivity()
        status_code = 200 if healthy else 503
        return JSONResponse(
            status_code=status_code,
            content={
                "status": "ok" if healthy else "unavailable",
                "service": "graph-service",
                "neo4j": "up" if healthy else "down",
            },
        )

    return app


app = create_app()

# ── OpenTelemetry distributed tracing (shared.telemetry, task 32.1 / Req 21.5) ──
# Single, self-contained hook: all tracer setup and span helpers live in
# shared/telemetry.py. No-op when the OpenTelemetry packages are absent.
from shared.telemetry import instrument_tracing  # noqa: E402

instrument_tracing(app, service_name="graph-service")
# ── end OpenTelemetry tracing ──
