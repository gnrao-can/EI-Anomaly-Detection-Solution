"""
services/graph-service/graph_repo.py
====================================
Neo4j graph construction and upsert logic for the Graph Service.

Implements the bipartite claimant-employer graph described in the design:

    (:Claimant {sin_hash})-[:EMPLOYED_BY {separation_date, roe_submission_ts,
                                          insurable_hours, claim_id, postal_fsa, ...}]
        ->(:Employer {bn})

The :class:`GraphRepository` wraps the asynchronous ``neo4j`` driver
(``neo4j.AsyncGraphDatabase``) and exposes idempotent upsert operations built
on ``MERGE`` so that repeated calls for the same claim never create duplicate
nodes or relationships.

All Cypher statements use **parameterised** queries — user-supplied values are
passed via the ``parameters`` mapping and are never interpolated into the query
string — mitigating Cypher injection.

Connection settings are read from the environment (``NEO4J_URI``,
``NEO4J_USER``, ``NEO4J_PASSWORD``) via :mod:`config`.  For unit tests and local
development a lightweight in-memory :class:`FakeAsyncDriver` records the exact
Cypher and parameters issued, so no live Neo4j instance is required.

Requirements: 5.1
"""

from __future__ import annotations

from typing import Any, Optional

try:  # The real async driver; optional so tests can run without the package.
    from neo4j import AsyncGraphDatabase  # type: ignore[import-not-found]

    _HAS_NEO4J = True
except ImportError:  # pragma: no cover - exercised only in minimal environments
    AsyncGraphDatabase = None  # type: ignore[assignment]
    _HAS_NEO4J = False


# ===========================================================================
# Cypher statements (parameterised — never string-interpolate user data)
# ===========================================================================

#: MERGE a Claimant node keyed on ``sin_hash`` and set/refresh its attributes.
UPSERT_CLAIMANT_CYPHER = """
MERGE (c:Claimant {sin_hash: $sin_hash})
ON CREATE SET c.created_at = datetime()
SET c += $attrs
RETURN c.sin_hash AS sin_hash
""".strip()

#: MERGE an Employer node keyed on ``bn`` and set/refresh its attributes.
UPSERT_EMPLOYER_CYPHER = """
MERGE (e:Employer {bn: $bn})
ON CREATE SET e.created_at = datetime()
SET e += $attrs
RETURN e.bn AS bn
""".strip()

#: MERGE the EMPLOYED_BY relationship between an existing Claimant and Employer.
#: The claimant/employer are MERGEd first so the edge can always be attached,
#: then the edge itself is MERGEd (keyed on the node pair) to guarantee
#: idempotency, with its properties refreshed via ``+=``.
ADD_EMPLOYED_BY_EDGE_CYPHER = """
MERGE (c:Claimant {sin_hash: $sin_hash})
MERGE (e:Employer {bn: $bn})
MERGE (c)-[r:EMPLOYED_BY]->(e)
SET r += $attrs
RETURN type(r) AS rel_type
""".strip()

#: Fetch the (claimant, employer) EMPLOYED_BY edges for an employer cohort so
#: the claimant-only projection (community.project_claimant_graph) can be built.
#: When ``bns`` is empty/NULL the whole graph is returned (dev/analytics use).
FETCH_SHARED_EMPLOYER_EDGES_CYPHER = """
MATCH (c:Claimant)-[:EMPLOYED_BY]->(e:Employer)
WHERE $bns IS NULL OR size($bns) = 0 OR e.bn IN $bns
RETURN c.sin_hash AS claimant_id, e.bn AS employer_id
""".strip()

#: Trivial connectivity probe used by the health check.
HEALTHCHECK_CYPHER = "RETURN 1 AS ok"


# ===========================================================================
# In-memory fake driver (test mode)
# ===========================================================================


class _FakeResult:
    """Minimal async result object mimicking ``neo4j.AsyncResult``."""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    async def single(self) -> Optional[dict[str, Any]]:
        return self._records[0] if self._records else None

    async def data(self) -> list[dict[str, Any]]:
        return list(self._records)


class _FakeSession:
    """In-memory stand-in for ``neo4j.AsyncSession``.

    Records every ``run`` invocation on the owning driver and maintains a tiny
    node/edge store so idempotency (MERGE semantics) can be asserted in tests.
    """

    def __init__(self, driver: "FakeAsyncDriver") -> None:
        self._driver = driver

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def run(self, query: str, parameters: Optional[dict[str, Any]] = None, **kwargs: Any):
        params = dict(parameters or {})
        params.update(kwargs)
        self._driver.calls.append((query, params))

        if query == UPSERT_CLAIMANT_CYPHER:
            self._driver.claimants[params["sin_hash"]] = dict(params.get("attrs", {}))
            return _FakeResult([{"sin_hash": params["sin_hash"]}])

        if query == UPSERT_EMPLOYER_CYPHER:
            self._driver.employers[params["bn"]] = dict(params.get("attrs", {}))
            return _FakeResult([{"bn": params["bn"]}])

        if query == ADD_EMPLOYED_BY_EDGE_CYPHER:
            self._driver.claimants.setdefault(params["sin_hash"], {})
            self._driver.employers.setdefault(params["bn"], {})
            self._driver.edges[(params["sin_hash"], params["bn"])] = dict(params.get("attrs", {}))
            return _FakeResult([{"rel_type": "EMPLOYED_BY"}])

        if query == FETCH_SHARED_EMPLOYER_EDGES_CYPHER:
            bns = params.get("bns")
            records: list[dict[str, Any]] = []
            for (sin_hash, bn) in self._driver.edges:
                if bns and bn not in bns:
                    continue
                records.append({"claimant_id": sin_hash, "employer_id": bn})
            return _FakeResult(records)

        if query == HEALTHCHECK_CYPHER:
            return _FakeResult([{"ok": 1}])

        return _FakeResult([])


class FakeAsyncDriver:
    """A fake async Neo4j driver for unit tests and local/dev "test mode".

    Attributes
    ----------
    calls:
        Ordered list of ``(query, parameters)`` tuples recording every Cypher
        statement executed. Tests assert against this to verify the correct
        parameterised MERGE statements were issued.
    claimants / employers / edges:
        In-memory stores that mimic MERGE (upsert) semantics so idempotency
        can be verified without a real database.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.claimants: dict[str, dict[str, Any]] = {}
        self.employers: dict[str, dict[str, Any]] = {}
        self.edges: dict[tuple[str, str], dict[str, Any]] = {}
        self.closed = False

    def session(self, **kwargs: Any) -> _FakeSession:  # noqa: ARG002 - kwargs ignored
        return _FakeSession(self)

    async def verify_connectivity(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


# ===========================================================================
# GraphRepository
# ===========================================================================


class GraphRepository:
    """Async repository for constructing the EI-ADS claimant-employer graph.

    Parameters
    ----------
    uri, user, password, database:
        Neo4j connection settings. Ignored when ``driver`` is supplied or when
        ``test_mode`` is True.
    driver:
        An explicit driver instance (real or fake). Primarily used by tests to
        inject a :class:`FakeAsyncDriver`.
    test_mode:
        When True, a :class:`FakeAsyncDriver` is used instead of a live Bolt
        connection. Also used automatically when the ``neo4j`` package is not
        installed.
    """

    def __init__(
        self,
        uri: str = "bolt://localhost:7687",
        user: str = "neo4j",
        password: str = "neo4j",
        database: str = "neo4j",
        driver: Any | None = None,
        test_mode: bool = False,
    ) -> None:
        self._database = database
        self._test_mode = test_mode or not _HAS_NEO4J

        if driver is not None:
            self._driver = driver
            self._test_mode = isinstance(driver, FakeAsyncDriver)
        elif self._test_mode:
            self._driver = FakeAsyncDriver()
        else:  # pragma: no cover - requires a live driver / network
            self._driver = AsyncGraphDatabase.driver(uri, auth=(user, password))

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_settings(cls, settings: Any) -> "GraphRepository":
        """Build a :class:`GraphRepository` from a :class:`config.Settings`."""
        return cls(
            uri=settings.neo4j_uri,
            user=settings.neo4j_user,
            password=settings.neo4j_password,
            database=getattr(settings, "neo4j_database", "neo4j"),
            test_mode=getattr(settings, "neo4j_test_mode", False),
        )

    @property
    def driver(self) -> Any:
        """Expose the underlying driver (used by tests / diagnostics)."""
        return self._driver

    @property
    def test_mode(self) -> bool:
        return self._test_mode

    # ------------------------------------------------------------------
    # Upsert operations
    # ------------------------------------------------------------------

    async def upsert_claimant(self, sin_hash: str, attrs: dict[str, Any] | None = None) -> str:
        """MERGE a :Claimant node keyed on ``sin_hash``.

        Idempotent: repeated calls for the same ``sin_hash`` update the node's
        attributes without creating a duplicate.
        """
        params = {"sin_hash": sin_hash, "attrs": self._clean_attrs(attrs, {"sin_hash"})}
        async with self._driver.session(database=self._database) as session:
            result = await session.run(UPSERT_CLAIMANT_CYPHER, parameters=params)
            record = await result.single()
        return record["sin_hash"] if record else sin_hash

    async def upsert_employer(self, bn: str, attrs: dict[str, Any] | None = None) -> str:
        """MERGE an :Employer node keyed on ``bn``.

        Idempotent: repeated calls for the same ``bn`` update the node's
        attributes without creating a duplicate.
        """
        params = {"bn": bn, "attrs": self._clean_attrs(attrs, {"bn"})}
        async with self._driver.session(database=self._database) as session:
            result = await session.run(UPSERT_EMPLOYER_CYPHER, parameters=params)
            record = await result.single()
        return record["bn"] if record else bn

    async def add_employed_by_edge(
        self, sin_hash: str, bn: str, attrs: dict[str, Any] | None = None
    ) -> None:
        """MERGE the ``EMPLOYED_BY`` relationship between a claimant and employer.

        The relationship carries attributes such as ``separation_date``,
        ``insurable_hours``, ``postal_fsa`` and ``roe_submission_ts``. MERGE on
        the (Claimant, Employer) node pair guarantees a single edge is
        maintained no matter how many times the claim is re-processed.
        """
        params = {
            "sin_hash": sin_hash,
            "bn": bn,
            "attrs": self._clean_attrs(attrs, set()),
        }
        async with self._driver.session(database=self._database) as session:
            result = await session.run(ADD_EMPLOYED_BY_EDGE_CYPHER, parameters=params)
            await result.single()

    # ------------------------------------------------------------------
    # Read operations (for projection / community detection)
    # ------------------------------------------------------------------

    async def fetch_shared_employer_edges(
        self, bns: list[str] | None = None
    ) -> list[tuple[str, str]]:
        """Fetch ``(claimant_id, employer_id)`` EMPLOYED_BY edges for a cohort.

        These bipartite edges are the input to
        :func:`community.project_claimant_graph`, which projects them into a
        claimant-only graph (shared employer = edge) for Louvain community
        detection.

        Parameters
        ----------
        bns:
            Optional list of employer Business Numbers to restrict the cohort.
            When ``None`` or empty, all EMPLOYED_BY edges in the graph are
            returned (useful for local analytics / dev).

        Returns
        -------
        list of tuple
            ``(claimant_id, employer_id)`` pairs. Works against a live Neo4j or
            the in-memory :class:`FakeAsyncDriver` (test mode).
        """
        params = {"bns": list(bns) if bns else None}
        async with self._driver.session(database=self._database) as session:
            result = await session.run(FETCH_SHARED_EMPLOYER_EDGES_CYPHER, parameters=params)
            rows = await result.data()
        return [(row["claimant_id"], row["employer_id"]) for row in rows]

    # ------------------------------------------------------------------
    # Health / lifecycle
    # ------------------------------------------------------------------

    async def verify_connectivity(self) -> bool:
        """Return ``True`` when the backing Neo4j (or fake) is reachable."""
        try:
            await self._driver.verify_connectivity()
            async with self._driver.session(database=self._database) as session:
                result = await session.run(HEALTHCHECK_CYPHER)
                record = await result.single()
            return bool(record and record.get("ok") == 1)
        except Exception:  # pragma: no cover - defensive; network dependent
            return False

    async def close(self) -> None:
        """Close the underlying driver and release its connection pool."""
        await self._driver.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_attrs(attrs: dict[str, Any] | None, exclude_keys: set[str]) -> dict[str, Any]:
        """Return a copy of ``attrs`` with ``None`` values and key-collisions removed.

        Dropping ``None`` avoids overwriting existing node properties with null,
        and excluding the merge key(s) prevents ``SET n += $attrs`` from
        redundantly (or inconsistently) re-writing the identity property.
        """
        if not attrs:
            return {}
        return {
            k: v
            for k, v in attrs.items()
            if v is not None and k not in exclude_keys
        }


__all__ = [
    "GraphRepository",
    "FakeAsyncDriver",
    "UPSERT_CLAIMANT_CYPHER",
    "UPSERT_EMPLOYER_CYPHER",
    "ADD_EMPLOYED_BY_EDGE_CYPHER",
    "FETCH_SHARED_EMPLOYER_EDGES_CYPHER",
    "HEALTHCHECK_CYPHER",
]
