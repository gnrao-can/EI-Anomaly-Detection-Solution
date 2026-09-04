"""
tests/unit/test_graph_repo.py
=============================
Unit tests for the Graph Service repository (services/graph-service/graph_repo.py).

These tests exercise :class:`GraphRepository` against an in-memory
:class:`FakeAsyncDriver` and verify that:

  - the correct **parameterised** Cypher ``MERGE`` statements are issued for
    ``upsert_claimant`` / ``upsert_employer`` / ``add_employed_by_edge``;
  - the merge keys (``sin_hash`` / ``bn``) and edge attributes are passed via
    the ``parameters`` mapping (never string-interpolated);
  - the operations are **idempotent** (repeated calls do not create duplicate
    nodes/edges, and later attribute values win).

No live Neo4j instance is required.

Requirements: 5.1
"""

from __future__ import annotations

import os
import sys

import pytest

# Make the graph-service module importable (service dirs are not on the path).
_SERVICE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services",
    "graph-service",
)
if _SERVICE_DIR not in sys.path:
    sys.path.insert(0, _SERVICE_DIR)

from graph_repo import (  # noqa: E402  (import after sys.path manipulation)
    ADD_EMPLOYED_BY_EDGE_CYPHER,
    UPSERT_CLAIMANT_CYPHER,
    UPSERT_EMPLOYER_CYPHER,
    FakeAsyncDriver,
    GraphRepository,
)

SIN_HASH = "a" * 64
OTHER_SIN_HASH = "b" * 64
BN = "123456789RT0001"


@pytest.fixture()
def driver() -> FakeAsyncDriver:
    return FakeAsyncDriver()


@pytest.fixture()
def repo(driver: FakeAsyncDriver) -> GraphRepository:
    return GraphRepository(driver=driver)


# ===========================================================================
# Construction / test-mode detection
# ===========================================================================


def test_injected_fake_driver_enables_test_mode(driver: FakeAsyncDriver):
    repo = GraphRepository(driver=driver)
    assert repo.test_mode is True
    assert repo.driver is driver


def test_test_mode_flag_builds_fake_driver():
    repo = GraphRepository(test_mode=True)
    assert repo.test_mode is True
    assert isinstance(repo.driver, FakeAsyncDriver)


def test_from_settings_uses_test_mode():
    class _S:
        neo4j_uri = "bolt://localhost:7687"
        neo4j_user = "neo4j"
        neo4j_password = "secret"
        neo4j_database = "neo4j"
        neo4j_test_mode = True

    repo = GraphRepository.from_settings(_S())
    assert repo.test_mode is True


# ===========================================================================
# upsert_claimant
# ===========================================================================


async def test_upsert_claimant_issues_parameterised_merge(repo, driver):
    result = await repo.upsert_claimant(SIN_HASH, {"postal_fsa": "K1A", "province_code": "ON"})

    assert result == SIN_HASH
    assert len(driver.calls) == 1
    query, params = driver.calls[0]

    # Correct MERGE statement, keyed on sin_hash.
    assert query == UPSERT_CLAIMANT_CYPHER
    assert "MERGE (c:Claimant {sin_hash: $sin_hash})" in query

    # Values are passed as parameters, not interpolated.
    assert params["sin_hash"] == SIN_HASH
    assert params["attrs"] == {"postal_fsa": "K1A", "province_code": "ON"}
    # The raw value never appears inside the query text.
    assert SIN_HASH not in query


async def test_upsert_claimant_drops_none_attrs(repo, driver):
    await repo.upsert_claimant(SIN_HASH, {"postal_fsa": "K1A", "surname_hash": None})
    _, params = driver.calls[0]
    assert params["attrs"] == {"postal_fsa": "K1A"}


async def test_upsert_claimant_excludes_key_from_attrs(repo, driver):
    # sin_hash must not be duplicated into the SET += payload.
    await repo.upsert_claimant(SIN_HASH, {"sin_hash": "SHOULD_BE_DROPPED", "postal_fsa": "K1A"})
    _, params = driver.calls[0]
    assert "sin_hash" not in params["attrs"]
    assert params["sin_hash"] == SIN_HASH


async def test_upsert_claimant_is_idempotent(repo, driver):
    await repo.upsert_claimant(SIN_HASH, {"postal_fsa": "K1A"})
    await repo.upsert_claimant(SIN_HASH, {"postal_fsa": "V6B"})

    # Two calls, but a single node keyed on sin_hash; latest attrs win.
    assert len(driver.claimants) == 1
    assert driver.claimants[SIN_HASH] == {"postal_fsa": "V6B"}


# ===========================================================================
# upsert_employer
# ===========================================================================


async def test_upsert_employer_issues_parameterised_merge(repo, driver):
    result = await repo.upsert_employer(BN, {"name": "Acme Inc"})

    assert result == BN
    query, params = driver.calls[0]
    assert query == UPSERT_EMPLOYER_CYPHER
    assert "MERGE (e:Employer {bn: $bn})" in query
    assert params["bn"] == BN
    assert params["attrs"] == {"name": "Acme Inc"}
    assert BN not in query


async def test_upsert_employer_is_idempotent(repo, driver):
    await repo.upsert_employer(BN, {"name": "Acme Inc"})
    await repo.upsert_employer(BN, {"name": "Acme Corp"})

    assert len(driver.employers) == 1
    assert driver.employers[BN] == {"name": "Acme Corp"}


# ===========================================================================
# add_employed_by_edge
# ===========================================================================


async def test_add_edge_issues_parameterised_merge(repo, driver):
    edge_attrs = {
        "separation_date": "2024-06-30",
        "insurable_hours": 800.0,
        "postal_fsa": "K1A",
        "claim_id": "claim-1",
    }
    await repo.add_employed_by_edge(SIN_HASH, BN, edge_attrs)

    query, params = driver.calls[0]
    assert query == ADD_EMPLOYED_BY_EDGE_CYPHER
    assert "MERGE (c)-[r:EMPLOYED_BY]->(e)" in query
    assert params["sin_hash"] == SIN_HASH
    assert params["bn"] == BN
    assert params["attrs"] == edge_attrs
    # No user values interpolated into the query.
    assert SIN_HASH not in query
    assert BN not in query
    assert "2024-06-30" not in query


async def test_add_edge_creates_endpoint_nodes(repo, driver):
    await repo.add_employed_by_edge(SIN_HASH, BN, {"claim_id": "claim-1"})
    # MERGE on both endpoints ensures the nodes exist.
    assert SIN_HASH in driver.claimants
    assert BN in driver.employers
    assert (SIN_HASH, BN) in driver.edges


async def test_add_edge_is_idempotent(repo, driver):
    await repo.add_employed_by_edge(SIN_HASH, BN, {"insurable_hours": 800.0})
    await repo.add_employed_by_edge(SIN_HASH, BN, {"insurable_hours": 810.0})

    # A single edge is maintained for the (claimant, employer) pair.
    assert len(driver.edges) == 1
    assert driver.edges[(SIN_HASH, BN)] == {"insurable_hours": 810.0}


async def test_distinct_claimants_produce_distinct_edges(repo, driver):
    await repo.add_employed_by_edge(SIN_HASH, BN, {"claim_id": "c1"})
    await repo.add_employed_by_edge(OTHER_SIN_HASH, BN, {"claim_id": "c2"})

    assert len(driver.edges) == 2
    assert (SIN_HASH, BN) in driver.edges
    assert (OTHER_SIN_HASH, BN) in driver.edges
    # Both point at the same single employer node (MERGE dedupes it).
    assert len(driver.employers) == 1


# ===========================================================================
# full claim construction sequence + connectivity/close
# ===========================================================================


async def test_full_construct_sequence_upserts_nodes_and_edge(repo, driver):
    await repo.upsert_claimant(SIN_HASH, {"postal_fsa": "K1A"})
    await repo.upsert_employer(BN, {"name": "Acme"})
    await repo.add_employed_by_edge(SIN_HASH, BN, {"claim_id": "claim-1"})

    assert [c[0] for c in driver.calls] == [
        UPSERT_CLAIMANT_CYPHER,
        UPSERT_EMPLOYER_CYPHER,
        ADD_EMPLOYED_BY_EDGE_CYPHER,
    ]
    assert len(driver.claimants) == 1
    assert len(driver.employers) == 1
    assert len(driver.edges) == 1


async def test_verify_connectivity_true_for_fake_driver(repo):
    assert await repo.verify_connectivity() is True


async def test_close_closes_driver(repo, driver):
    await repo.close()
    assert driver.closed is True
