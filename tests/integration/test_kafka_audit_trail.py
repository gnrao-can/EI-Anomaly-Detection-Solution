"""
tests/integration/test_kafka_audit_trail.py
============================================
Kafka event-flow and audit-trail integration tests (task 33.2).

This file covers the three requirement bullets of task 33.2:

  1. ``claim.received`` is consumed within 100 ms of publication  (Req 2.1)
  2. an audit-log entry is written to ClickHouse within 500 ms for each
     adapter/component call                                       (Req 2.7)
  3. the audit log is immutable — an UPDATE against PostgreSQL ``audit_log``
     is denied by the INSERT-only Row-Level Security policy       (Req 19.3)

Two execution paths, mirroring the sibling ``test_orchestrator_flow.py`` (33.1):

  * **container-backed** — the *preferred* path. Uses ``testcontainers`` to
    start real Kafka, ClickHouse and PostgreSQL instances and asserts the timing
    and immutability contracts against the real infrastructure. These classes
    are guarded with ``skipif`` (via ``tests/integration/conftest.py``) and are
    SKIPPED — never failed — when ``testcontainers`` or a container daemon is
    absent, as in the CI sandbox.
  * **in-process** — always runs, no external services. It drives the *real*
    orchestrator modules (``consumer`` / ``correlator`` / ``publisher`` built by
    tasks 6.1–6.3) through their public seams to verify the same contracts:
      - the consumer hands a ``claim.received`` envelope to the correlator well
        within the 100 ms initiation budget,
      - ``ResultPublisher.write_audit`` writes a complete, schema-shaped audit
        row within the 500 ms budget for each adapter/component call,
      - the migration SQL defines an INSERT-only RLS policy and no UPDATE/DELETE
        policy (the mechanism that denies UPDATEs at the DB layer).

Requirements: 2.7, 19.3 (and 2.1 for the 100 ms consumption bullet)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4

import pytest

# ---------------------------------------------------------------------------
# Load the hyphenated pipeline-orchestrator modules collision-free, exactly as
# the sibling integration test does (see tests/conftest.load_service_module).
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from tests.conftest import load_service_module  # noqa: E402
from tests.integration.conftest import (  # noqa: E402
    CONTAINER_RUNTIME_AVAILABLE,
    CONTAINER_RUNTIME_SKIP_REASON,
    python_driver_available,
)

_SERVICE = "pipeline-orchestrator"

_correlator = load_service_module(_SERVICE, "correlator", siblings=["correlator"])
_consumer = load_service_module(
    _SERVICE, "consumer", siblings=["correlator", "consumer"]
)
_publisher = load_service_module(
    _SERVICE, "publisher", siblings=["scoring", "publisher"]
)

EventCorrelator = _correlator.EventCorrelator
InMemoryPairingStore = _correlator.InMemoryPairingStore
CLAIM_RECEIVED = _correlator.CLAIM_RECEIVED
ROE_RECEIVED = _correlator.ROE_RECEIVED

OrchestratorConsumer = _consumer.OrchestratorConsumer

ResultPublisher = _publisher.ResultPublisher
ADAPTER_AUDIT_TABLE = _publisher.ADAPTER_AUDIT_TABLE
AUDIT_BUDGET_MS = _publisher.AUDIT_BUDGET_MS


# ---------------------------------------------------------------------------
# Constants for the task's timing bullets.
# ---------------------------------------------------------------------------

#: Req 2.1 — the pipeline must initiate on a consumed ``claim.received`` within
#: 100 ms of the (later) event timestamp.
CONSUMPTION_BUDGET_MS = 100.0

#: Req 2.7 — every audit entry must be written within 500 ms of its event.
AUDIT_WRITE_BUDGET_MS = 500.0

#: The adapter/component names whose calls must each produce an audit entry.
SAMPLE_ADAPTER_CALLS = [
    "cra-adapter",
    "cbsa-adapter",
    "geonames-adapter",
    "business-registry-adapter",
    "mvr-adapter",
    "stats-canada-adapter",
    "job-bank-adapter",
]


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


class TimestampedAuditSink:
    """Fake ClickHouse sink that records insert rows and their arrival time.

    The recorded ``monotonic`` timestamp lets the in-process test measure the
    end-to-end write latency the same way the container-backed test measures the
    real ClickHouse round-trip.
    """

    def __init__(self) -> None:
        self.rows: list[tuple[str, dict, float]] = []

    async def insert(self, table: str, row: dict) -> None:
        # Simulate a trivially small write cost; real ClickHouse is measured for
        # real in the container-backed test below.
        self.rows.append((table, row, time.monotonic()))

    def rows_for_component(self, component: str) -> list[dict]:
        return [r for (_t, r, _ts) in self.rows if r.get("component") == component]


# ===========================================================================
# In-process path — ALWAYS runs (no external services)
# ===========================================================================


@pytest.mark.asyncio
async def test_claim_received_consumed_within_100ms_in_process():
    """A consumed ``claim.received`` envelope reaches the correlator < 100 ms.

    Requirement 2.1: parallel engine execution must be *initiated* within 100 ms
    of the consumed event. We measure the consumer -> correlator hand-off, which
    is the code path that gates that initiation, and assert it completes well
    within the 100 ms budget.

    Validates: Requirements 2.1
    """
    claim_id = str(uuid4())
    store = InMemoryPairingStore()
    correlator = EventCorrelator(store)

    consumed_at: list[float] = []

    async def dispatch_callback(pair):  # only fires on the *pair*, not here
        consumed_at.append(time.monotonic())

    consumer = OrchestratorConsumer(correlator, dispatch_callback)

    now = datetime.now(timezone.utc).isoformat()
    envelope = {
        "event_type": CLAIM_RECEIVED,
        "claim_id": claim_id,
        "payload": {"claim_id": claim_id, "received_at": now},
    }

    start = time.monotonic()
    result = await consumer.handle_message(envelope)
    elapsed_ms = (time.monotonic() - start) * 1000.0

    # First (unpaired) event: correlator stores it and returns None.
    assert result is None
    # The consume + correlate hand-off is comfortably within the 100 ms budget.
    assert elapsed_ms < CONSUMPTION_BUDGET_MS, (
        f"claim.received consumption took {elapsed_ms:.2f} ms "
        f"(budget {CONSUMPTION_BUDGET_MS} ms)"
    )


@pytest.mark.asyncio
async def test_audit_entry_written_within_500ms_per_adapter_call_in_process():
    """Each adapter/component call produces a complete audit row within 500 ms.

    Requirement 2.7: a complete, immutable audit entry is written for every
    adapter call within 500 ms of the event. We invoke ``write_audit`` once per
    sample adapter and assert (a) a row is produced per call, (b) it carries the
    Protected-B-safe schema shape, and (c) each write is within the 500 ms
    budget.

    Validates: Requirements 2.7
    """
    audit = TimestampedAuditSink()
    publisher = ResultPublisher(producer=_NullProducer(), audit_sink=audit)
    claim_id = str(uuid4())

    for component in SAMPLE_ADAPTER_CALLS:
        start = time.monotonic()
        row = await publisher.write_audit(
            claim_id=claim_id,
            component=component,
            action="ADAPTER_CALL",
            fields_accessed=["employment_income", "noc_category"],
            outcome="SUCCESS",
            duration_ms=12.5,
        )
        elapsed_ms = (time.monotonic() - start) * 1000.0

        # Each adapter call is audited within the 500 ms budget.
        assert elapsed_ms < AUDIT_WRITE_BUDGET_MS, (
            f"audit write for {component} took {elapsed_ms:.2f} ms "
            f"(budget {AUDIT_WRITE_BUDGET_MS} ms)"
        )
        # The row is complete and Protected-B safe (sin_hash slot, no raw SIN).
        assert row["component"] == component
        assert row["claim_id"] == claim_id
        assert row["fields_accessed"] == ["employment_income", "noc_category"]
        assert len(row["sin_hash"]) == 64  # fixed-width hash slot, never raw SIN

    # Exactly one audit row was written to the adapter_audit_log table per call.
    assert len(audit.rows) == len(SAMPLE_ADAPTER_CALLS)
    assert all(table == ADAPTER_AUDIT_TABLE for (table, _row, _ts) in audit.rows)
    for component in SAMPLE_ADAPTER_CALLS:
        assert len(audit.rows_for_component(component)) == 1

    # Sanity: the module's own budget constant matches the requirement.
    assert AUDIT_BUDGET_MS == AUDIT_WRITE_BUDGET_MS


def test_audit_log_migration_enforces_immutability():
    """The 004 migration makes ``audit_log`` append-only and immutable.

    Requirement 2.7 / 19.3: the audit log is immutable. Immutability is enforced
    by enabling Row-Level Security on ``audit_log`` (so INSERT is allowed and
    existing rows remain visible to a mutation attempt) PLUS a
    ``BEFORE UPDATE OR DELETE`` trigger that raises ``insufficient_privilege``
    (SQLSTATE 42501) for every affected row.

    Note: an INSERT-only RLS policy alone is NOT sufficient — with no
    UPDATE/DELETE visibility policy, an UPDATE/DELETE does not error; it finds no
    rows visible and silently reports ``UPDATE 0`` / ``DELETE 0`` (tamper
    tolerance, not immutability). The trigger is what turns the attempt into a
    hard 42501 denial. This test asserts that structural guarantee statically;
    the container-backed test below proves the runtime behaviour (an actual
    UPDATE is denied with 42501).

    Validates: Requirements 2.7, 19.3
    """
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    migration = os.path.join(
        repo_root, "scripts", "migrations", "004_create_audit_log.sql"
    )
    with open(migration, "r", encoding="utf-8") as handle:
        sql = handle.read()

    normalized = sql.upper()

    # RLS is enabled on the audit_log table.
    assert re.search(
        r"ALTER\s+TABLE\s+AUDIT_LOG\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY", normalized
    ), "audit_log must ENABLE ROW LEVEL SECURITY"

    # An INSERT policy exists so the application can append audit rows.
    assert re.search(
        r"CREATE\s+POLICY\s+\w+\s+ON\s+AUDIT_LOG\s+FOR\s+INSERT", normalized
    ), "audit_log must define a FOR INSERT policy (append-only writes)"

    # A BEFORE UPDATE OR DELETE trigger enforces immutability by raising 42501.
    assert re.search(
        r"CREATE\s+TRIGGER\s+\w+\s+BEFORE\s+UPDATE\s+OR\s+DELETE\s+ON\s+AUDIT_LOG",
        normalized,
    ), (
        "audit_log must define a BEFORE UPDATE OR DELETE trigger to enforce "
        "immutability (an INSERT-only RLS policy alone silently no-ops mutations)"
    )

    # The trigger function raises insufficient_privilege (SQLSTATE 42501).
    assert "42501" in normalized, (
        "the immutability trigger must raise insufficient_privilege (SQLSTATE "
        "42501) on UPDATE/DELETE"
    )


class _NullProducer:
    """No-op Kafka producer for tests that only exercise the audit path."""

    async def send_and_wait(self, topic, value, key=None, headers=None):
        return object()


# ===========================================================================
# Container-backed path — PREFERRED; SKIPPED when no runtime is available
# ===========================================================================


@pytest.mark.integration
@pytest.mark.skipif(
    not CONTAINER_RUNTIME_AVAILABLE,
    reason=CONTAINER_RUNTIME_SKIP_REASON or "container runtime unavailable",
)
class TestKafkaConsumptionWithContainer:
    """``claim.received`` consumed within 100 ms over a real Kafka broker (2.1)."""

    @pytest.mark.asyncio
    async def test_claim_received_consumed_within_100ms_over_real_kafka(self):
        from testcontainers.kafka import KafkaContainer  # guarded import
        from aiokafka import AIOKafkaProducer

        with KafkaContainer() as kafka:
            bootstrap = kafka.get_bootstrap_server()

            claim_id = str(uuid4())
            store = InMemoryPairingStore()
            correlator = EventCorrelator(store)

            consumed_at: dict[str, float] = {}

            # Wrap the correlator so we can timestamp the moment the consumer
            # loop hands a consumed claim.received event to it.
            original_on_event = correlator.on_event

            async def timing_on_event(event_type, key, payload):
                if event_type == CLAIM_RECEIVED and key == claim_id:
                    consumed_at[key] = time.monotonic()
                return await original_on_event(event_type, key, payload)

            correlator.on_event = timing_on_event  # type: ignore[assignment]

            async def dispatch_callback(pair):
                return None

            consumer = OrchestratorConsumer(
                correlator,
                dispatch_callback,
                bootstrap_servers=bootstrap,
                group_id="itest-audit-consumption",
                degrade_gracefully=False,
            )
            started = await consumer.start()
            assert started is True

            producer = AIOKafkaProducer(bootstrap_servers=bootstrap)
            await producer.start()
            try:
                now = datetime.now(timezone.utc).isoformat()
                envelope = {
                    "event_type": CLAIM_RECEIVED,
                    "claim_id": claim_id,
                    "payload": {"claim_id": claim_id, "received_at": now},
                }
                published_at = time.monotonic()
                await producer.send_and_wait(
                    CLAIM_RECEIVED,
                    value=json.dumps(envelope).encode("utf-8"),
                    key=claim_id.encode("utf-8"),
                )

                # Wait for the consumer loop to process the event.
                for _ in range(100):
                    if claim_id in consumed_at:
                        break
                    await asyncio.sleep(0.005)

                assert claim_id in consumed_at, "claim.received was never consumed"
                latency_ms = (consumed_at[claim_id] - published_at) * 1000.0
                assert latency_ms <= CONSUMPTION_BUDGET_MS, (
                    f"claim.received consumed in {latency_ms:.2f} ms "
                    f"(budget {CONSUMPTION_BUDGET_MS} ms)"
                )
            finally:
                await producer.stop()
                await consumer.stop()


@pytest.mark.integration
@pytest.mark.skipif(
    not CONTAINER_RUNTIME_AVAILABLE,
    reason=CONTAINER_RUNTIME_SKIP_REASON or "container runtime unavailable",
)
@pytest.mark.skipif(
    not python_driver_available("clickhouse_connect"),
    reason="clickhouse-connect driver not installed",
)
class TestClickHouseAuditWriteWithContainer:
    """Audit entry written to real ClickHouse within 500 ms per adapter call (2.7)."""

    @pytest.mark.asyncio
    async def test_audit_written_to_clickhouse_within_500ms(self):
        from testcontainers.clickhouse import ClickHouseContainer  # guarded import
        import clickhouse_connect

        with ClickHouseContainer("clickhouse/clickhouse-server:24.8") as ch:
            client = clickhouse_connect.get_client(
                host=ch.get_container_host_ip(),
                port=int(ch.get_exposed_port(8123)),
                username=ch.username,
                password=ch.password,
            )

            # Apply the analytics DDL (database + adapter_audit_log table).
            repo_root = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "..")
            )
            client.command("CREATE DATABASE IF NOT EXISTS ei_ads_analytics")
            ddl_path = os.path.join(
                repo_root,
                "scripts",
                "clickhouse",
                "005_create_adapter_audit_log.sql",
            )
            with open(ddl_path, "r", encoding="utf-8") as handle:
                client.command(handle.read())

            # A ClickHouseAuditSink wired to the container writes one row per
            # adapter call; we measure each round-trip against the 500 ms budget.
            from importlib import import_module  # noqa: F401 - readability only

            ClickHouseAuditSink = _publisher.ClickHouseAuditSink
            sink = ClickHouseAuditSink(
                host=ch.get_container_host_ip(),
                port=int(ch.get_exposed_port(8123)),
                username=ch.username,
                password=ch.password,
                database="ei_ads_analytics",
            )
            publisher = ResultPublisher(producer=_NullProducer(), audit_sink=sink)
            claim_id = str(uuid4())

            for component in SAMPLE_ADAPTER_CALLS:
                start = time.monotonic()
                await publisher.write_audit(
                    claim_id=claim_id,
                    component=component,
                    action="ADAPTER_CALL",
                    fields_accessed=["employment_income"],
                    outcome="SUCCESS",
                    duration_ms=10.0,
                )
                elapsed_ms = (time.monotonic() - start) * 1000.0
                assert elapsed_ms <= AUDIT_WRITE_BUDGET_MS, (
                    f"ClickHouse audit write for {component} took "
                    f"{elapsed_ms:.2f} ms (budget {AUDIT_WRITE_BUDGET_MS} ms)"
                )

            # Every adapter call is durably recorded in adapter_audit_log.
            count = client.query(
                "SELECT count() FROM ei_ads_analytics.adapter_audit_log "
                "WHERE claim_id = %(cid)s",
                parameters={"cid": claim_id},
            ).result_rows[0][0]
            assert count == len(SAMPLE_ADAPTER_CALLS)


@pytest.mark.integration
@pytest.mark.skipif(
    not CONTAINER_RUNTIME_AVAILABLE,
    reason=CONTAINER_RUNTIME_SKIP_REASON or "container runtime unavailable",
)
@pytest.mark.skipif(
    not python_driver_available("asyncpg"),
    reason="asyncpg driver not installed",
)
class TestImmutableAuditLogWithContainer:
    """An UPDATE against PostgreSQL ``audit_log`` is denied by RLS (19.3)."""

    @pytest.mark.asyncio
    async def test_update_on_audit_log_is_permission_denied(
        self, itest_audit_log_migration_sql
    ):
        import asyncpg
        from testcontainers.postgres import PostgresContainer

        with PostgresContainer("postgres:16-alpine") as pg:
            # Superuser connection to set up the schema, the application role,
            # and the seed row (application inserts are always permitted).
            admin_dsn = pg.get_connection_url().replace(
                "postgresql+psycopg2://", "postgresql://"
            )
            admin = await asyncpg.connect(admin_dsn)
            try:
                # gen_random_uuid() lives in pgcrypto on some images.
                await admin.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

                # The migration creates audit_log with RLS + INSERT-only policy.
                # It references the ei_ads_app application role and the claims
                # table (FK target); create the minimum needed to apply it.
                await admin.execute(
                    "CREATE TABLE IF NOT EXISTS claims (claim_id UUID PRIMARY KEY)"
                )
                await admin.execute(
                    "DO $$ BEGIN "
                    "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ei_ads_app') "
                    "THEN CREATE ROLE ei_ads_app LOGIN PASSWORD 'app'; END IF; "
                    "END $$;"
                )
                await admin.execute(itest_audit_log_migration_sql)

                # The application role must NOT bypass RLS, and needs table privs.
                await admin.execute("ALTER ROLE ei_ads_app NOBYPASSRLS")
                await admin.execute(
                    "GRANT INSERT, SELECT, UPDATE, DELETE ON audit_log TO ei_ads_app"
                )
                await admin.execute(
                    "GRANT USAGE, SELECT ON SEQUENCE audit_log_log_id_seq TO ei_ads_app"
                )

                # Seed one audit row as the application role (INSERT is allowed).
                app_dsn = _dsn_with_user(admin_dsn, "ei_ads_app", "app")
                app = await asyncpg.connect(app_dsn)
                try:
                    await app.execute(
                        "INSERT INTO audit_log (component, action, outcome) "
                        "VALUES ($1, $2, $3)",
                        "cra-adapter",
                        "ADAPTER_CALL",
                        "SUCCESS",
                    )

                    # The immutability contract: UPDATE is denied by RLS because
                    # there is no FOR UPDATE policy for the application role.
                    with pytest.raises(asyncpg.PostgresError) as exc_info:
                        await app.execute(
                            "UPDATE audit_log SET outcome = 'TAMPERED'"
                        )
                    # RLS denials surface as insufficient_privilege (42501).
                    assert exc_info.value.sqlstate == "42501", (
                        "expected permission-denied (42501) for UPDATE on "
                        f"immutable audit_log, got {exc_info.value.sqlstate}"
                    )
                finally:
                    await app.close()
            finally:
                await admin.close()


def _dsn_with_user(dsn: str, user: str, password: str) -> str:
    """Return ``dsn`` rewritten to authenticate as ``user`` / ``password``."""
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(dsn)
    netloc = f"{user}:{password}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))
