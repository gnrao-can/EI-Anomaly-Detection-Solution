"""
tests/integration/test_slice2_dashboard_backend.py
===================================================
Slice 2 — the investigator dashboard ↔ portal-backend seam over REAL transports.

Drives the reworked portal-backend (services/portal-backend) as a real HTTP
service (LoopbackService) backed by a REAL PostgreSQL (testcontainers, seeded
via the actual migrations 001–004) and a REAL Kafka broker (testcontainers),
then exercises the exact endpoints the React dashboard SPA calls
(services/claimant-portal/src/api/*). This is the Slice 2 integration checkpoint
per the regeneration rule: "no seam is done until an integration test exercises
it over REAL transports."

What it proves against the real schema + brokers:
  * 22.1 — GET /portal/investigator/claims returns ONLY flagged claims
    (FLAG_FOR_REVIEW/INVESTIGATE), sorted by risk_score DESC, for INVESTIGATOR.
  * 22.6 — REVIEWER gets the queue (200) with PII masked; REVIEWER and CLAIMANT
    are DENIED (403) on the claim DETAIL endpoint; INVESTIGATOR sees it unmasked.
  * 22.2 — GET /portal/investigator/claims/{id} returns score/decision, the
    indicators, and the SHAP attributions read from real tables.
  * 22.3 + 22.4 — PUT /api/v1/claims/{id}/disposition persists to the real
    claim_dispositions table (detail then reflects it) AND publishes a real
    outcome.feedback event to Kafka for CONFIRMED_FRAUD/FALSE_POSITIVE;
    NEEDS_FURTHER_REVIEW persists but publishes NOTHING.
  * 22.1 (SSE) — GET /portal/events/claims streams a broadcast flagged claim.

Auth note: the portal's own endpoints authenticate via the signed HttpOnly
session cookie (the browser↔portal credential), NOT a GCKey Bearer — the Bearer
is only forwarded on downstream Claim Intake calls. So this test mints the
portal session cookie directly with the shared SESSION_SECRET (exactly what the
GCKey callback issues), which is the faithful credential for these endpoints.

Requirements exercised: 22.1, 22.2, 22.3, 22.4, 22.6.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from tests.conftest import load_service_module  # noqa: E402
from tests.integration.conftest import (  # noqa: E402
    CONTAINER_RUNTIME_AVAILABLE,
    CONTAINER_RUNTIME_SKIP_REASON,
    python_driver_available,
)
from tests.integration.loopback_service import LoopbackService  # noqa: E402

pytestmark = pytest.mark.skipif(
    not CONTAINER_RUNTIME_AVAILABLE,
    reason=CONTAINER_RUNTIME_SKIP_REASON or "container runtime unavailable",
)

_SESSION_SECRET = "itest_portal_session_secret"
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_MIGRATIONS = (
    "001_create_claims.sql",
    "002_create_risk_assessments.sql",
    "003_create_indicators.sql",
    "004_create_audit_log.sql",
)


def _migration_sql(name: str) -> str:
    with open(
        os.path.join(_REPO_ROOT, "scripts", "migrations", name), "r", encoding="utf-8"
    ) as handle:
        return handle.read()


async def _seed_schema_and_data(dsn: str) -> dict:
    """Apply migrations 001–004 and seed a flagged + a non-flagged claim.

    Returns identifiers the test asserts against.
    """
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        # The portal role referenced by the audit_log migration.
        await conn.execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='ei_ads_app') "
            "THEN CREATE ROLE ei_ads_app LOGIN PASSWORD 'app'; END IF; END $$;"
        )
        for name in _MIGRATIONS:
            await conn.execute(_migration_sql(name))

        now = datetime.now(timezone.utc)
        # Two flagged claims (different scores, to assert DESC ordering) and one
        # AUTO_APPROVE claim that must NOT appear in the flagged queue.
        high = str(uuid.uuid4())   # INVESTIGATE, score 92
        low = str(uuid.uuid4())    # FLAG_FOR_REVIEW, score 55
        approved = str(uuid.uuid4())  # AUTO_APPROVE, score 10 (excluded)

        async def _insert_claim(claim_id, status, received_at):
            await conn.execute(
                "INSERT INTO claims (claim_id, sin_hash, employer_bn, insurable_hours, "
                "insurable_earnings, separation_date, separation_reason, status, received_at) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                claim_id, ("a" * 64), "123456789RP0001", 800, 25000,
                date(2024, 1, 1), "Shortage of work", status, received_at,
            )

        async def _insert_assessment(claim_id, score, decision, scored_at):
            assessment_id = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO risk_assessments (assessment_id, claim_id, risk_score, "
                "risk_decision, shap_base_value, explanation_en, explanation_fr, "
                "model_version, scored_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                assessment_id, claim_id, score, decision, 50.0,
                "High risk pattern detected.", "Motif à risque élevé détecté.",
                "itest_v1", scored_at,
            )
            return assessment_id

        await _insert_claim(high, "INVESTIGATE", now - timedelta(minutes=5))
        await _insert_claim(low, "SCORED", now - timedelta(minutes=4))
        await _insert_claim(approved, "APPROVED", now - timedelta(minutes=3))

        high_assess = await _insert_assessment(high, 92, "INVESTIGATE", now - timedelta(minutes=2))
        await _insert_assessment(low, 55, "FLAG_FOR_REVIEW", now - timedelta(minutes=1))
        await _insert_assessment(approved, 10, "AUTO_APPROVE", now)

        # One indicator + one shap attribution on the high claim (detail view).
        indicator_id = str(uuid.uuid4())
        await conn.execute(
            "INSERT INTO indicators (indicator_id, claim_id, assessment_id, engine_type, "
            "severity, algorithm, title, explanation_en, explanation_fr, shap_contribution) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
            indicator_id, high, high_assess, "REPETITIVE_PATTERN", "HIGH",
            "sequential_pattern_mining", "Repeated employer pattern",
            "Three claims from the same employer.", "Trois demandes du même employeur.", 12.0,
        )
        await conn.execute(
            "INSERT INTO shap_attributions (assessment_id, indicator_id, feature_name, "
            "contribution, base_value) VALUES ($1,$2,$3,$4,$5)",
            high_assess, indicator_id, "employer_repetition", 12.0, 50.0,
        )
        return {"high": high, "low": low, "approved": approved, "indicator_id": indicator_id}
    finally:
        await conn.close()


def _session_cookie(portal_auth, role: str, sin_hash=None, subject=None) -> dict:
    """Mint the portal's signed session cookie (the browser↔portal credential)."""
    payload = {"role": role, "sin_hash": sin_hash, "subject": subject or f"{role.lower()}-sub"}
    token = portal_auth.sign_session(payload, _SESSION_SECRET, max_age=3600)
    return {"ei_ads_session": token}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dashboard_backend_over_real_postgres_and_kafka(itest_service_auth):
    """Investigator dashboard endpoints over real Postgres + Kafka + HTTP."""
    if not python_driver_available("asyncpg"):
        pytest.skip("asyncpg driver not installed")
    from testcontainers.postgres import PostgresContainer
    from testcontainers.kafka import KafkaContainer
    from aiokafka import AIOKafkaConsumer

    _SIBLINGS = ["config", "auth", "repository", "sse", "gckey", "main"]
    portal_config = load_service_module("portal-backend", "config", siblings=_SIBLINGS)
    portal_auth = load_service_module("portal-backend", "auth", siblings=_SIBLINGS)

    pg = kafka = None
    service = None
    feedback_consumer = None
    try:
        # ── Real Postgres, seeded via the actual migrations. ────────────────
        pg = PostgresContainer("postgres:16-alpine")
        pg.start()
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        seeded = await _seed_schema_and_data(dsn)

        # ── Real Kafka. ─────────────────────────────────────────────────────
        kafka = KafkaContainer()
        kafka.start()
        bootstrap = kafka.get_bootstrap_server()

        # ── Point the portal at the real backends via env, then load app. ──
        parsed = httpx.URL(dsn)
        os.environ["POSTGRES_HOST"] = parsed.host
        os.environ["POSTGRES_PORT"] = str(parsed.port)
        os.environ["POSTGRES_DB"] = parsed.path.lstrip("/")
        os.environ["POSTGRES_USER"] = parsed.username
        os.environ["POSTGRES_PASSWORD"] = parsed.password
        os.environ["KAFKA_BOOTSTRAP_SERVERS"] = bootstrap
        os.environ["SESSION_SECRET"] = _SESSION_SECRET
        portal_config.get_settings.cache_clear()

        portal_main = load_service_module("portal-backend", "main", siblings=_SIBLINGS)

        # The lifespan (run by LoopbackService/uvicorn) builds the real asyncpg
        # pool + AIOKafkaProducer from the env above, so the app talks to the
        # real containers over real sockets.
        service = LoopbackService(portal_main.app, health_path="/healthz").start()
        base = service.base_url

        inv = _session_cookie(portal_auth, "INVESTIGATOR", subject="inv-42")
        rev = _session_cookie(portal_auth, "REVIEWER")
        claimant = _session_cookie(portal_auth, "CLAIMANT", sin_hash="x")

        async with httpx.AsyncClient(base_url=base, timeout=10.0) as client:
            # ── 22.1: flagged queue, sorted desc, flagged-only (INVESTIGATOR) ─
            q = await client.get("/portal/investigator/claims", cookies=inv)
            assert q.status_code == 200, q.text
            claims = q.json()["claims"]
            ids = [c["claim_id"] for c in claims]
            assert seeded["approved"] not in ids  # AUTO_APPROVE excluded
            assert ids == [seeded["high"], seeded["low"]]  # 92 before 55
            assert [c["risk_score"] for c in claims] == [92, 55]

            # ── 22.6: REVIEWER sees the queue but PII masked ─────────────────
            qr = await client.get("/portal/investigator/claims", cookies=rev)
            assert qr.status_code == 200
            assert qr.json()["claims"][0]["claimant_name"] is None

            # ── 22.6: detail is INVESTIGATOR/ADMIN only ──────────────────────
            d_inv = await client.get(
                f"/portal/investigator/claims/{seeded['high']}", cookies=inv
            )
            assert d_inv.status_code == 200, d_inv.text
            detail = d_inv.json()
            assert detail["risk_score"] == 92
            assert detail["risk_decision"] == "INVESTIGATE"
            assert len(detail["indicators"]) == 1
            assert detail["indicators"][0]["engine"] == "REPETITIVE_PATTERN"
            assert len(detail["shap_attributions"]) == 1
            assert detail["explanation_en"]

            d_rev = await client.get(
                f"/portal/investigator/claims/{seeded['high']}", cookies=rev
            )
            assert d_rev.status_code == 403  # Req 22.6 (the guard fix)
            d_clm = await client.get(
                f"/portal/investigator/claims/{seeded['high']}", cookies=claimant
            )
            assert d_clm.status_code == 403

            # ── 22.3 + 22.4: disposition persists AND publishes outcome.feedback ─
            feedback_consumer = AIOKafkaConsumer(
                "outcome.feedback",
                bootstrap_servers=bootstrap,
                group_id="itest-slice2-feedback",
                auto_offset_reset="earliest",
                enable_auto_commit=True,
            )
            await feedback_consumer.start()

            put = await client.put(
                f"/api/v1/claims/{seeded['high']}/disposition",
                json={"disposition": "CONFIRMED_FRAUD", "case_notes": "verified"},
                cookies=inv,
            )
            assert put.status_code == 200, put.text

            # Persisted to claim_dispositions -> detail now reflects it (22.3).
            d2 = await client.get(
                f"/portal/investigator/claims/{seeded['high']}", cookies=inv
            )
            assert d2.json()["disposition"] == "CONFIRMED_FRAUD"
            assert d2.json()["case_notes"] == "verified"

            # Real outcome.feedback event on Kafka (22.4).
            event = None
            deadline = asyncio.get_event_loop().time() + 20.0
            while asyncio.get_event_loop().time() < deadline:
                try:
                    msg = await asyncio.wait_for(feedback_consumer.getone(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
                candidate = json.loads(msg.value.decode("utf-8"))
                if candidate.get("claim_id") == seeded["high"]:
                    event = candidate
                    break
            assert event is not None, "no outcome.feedback event observed"
            assert event["event_type"] == "outcome.feedback"
            assert event["disposition"] == "CONFIRMED_FRAUD"

            # ── 22.4: NEEDS_FURTHER_REVIEW persists but publishes NOTHING ────
            put2 = await client.put(
                f"/api/v1/claims/{seeded['low']}/disposition",
                json={"disposition": "NEEDS_FURTHER_REVIEW", "case_notes": "later"},
                cookies=inv,
            )
            assert put2.status_code == 200
            # No feedback event should arrive for the 'low' claim.
            saw_low = False
            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                try:
                    msg = await asyncio.wait_for(feedback_consumer.getone(), timeout=2.0)
                except asyncio.TimeoutError:
                    break
                if json.loads(msg.value.decode("utf-8")).get("claim_id") == seeded["low"]:
                    saw_low = True
                    break
            assert saw_low is False, "NEEDS_FURTHER_REVIEW must not publish outcome.feedback"

            # ── 22.1 (SSE): a broadcast flagged claim reaches the stream ─────
            broadcast = {
                "claim_id": seeded["high"], "risk_score": 92,
                "risk_decision": "INVESTIGATE", "received_at": "2024-01-01T00:00:00+00:00",
                "employer_bn": "123456789RP0001", "claimant_name": "Test",
                "claimant_sin": "***-**-999", "address": None,
            }

            async def _read_one_sse_frame():
                async with httpx.AsyncClient(base_url=base, timeout=10.0) as sse_client:
                    async with sse_client.stream(
                        "GET", "/portal/events/claims", cookies=inv
                    ) as resp:
                        assert resp.status_code == 200
                        async for line in resp.aiter_lines():
                            if line.startswith("data:"):
                                return json.loads(line[len("data:"):].strip())
                return None

            sse_task = asyncio.ensure_future(_read_one_sse_frame())
            await asyncio.sleep(0.5)  # let the subscriber attach
            await portal_main.app.state.broadcaster.publish(broadcast)
            frame = await asyncio.wait_for(sse_task, timeout=10.0)
            assert frame is not None and frame["claim_id"] == seeded["high"]
    finally:
        if feedback_consumer is not None:
            await feedback_consumer.stop()
        if service is not None:
            service.stop()
        if kafka is not None:
            kafka.stop()
        if pg is not None:
            pg.stop()
        for key in (
            "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER",
            "POSTGRES_PASSWORD", "KAFKA_BOOTSTRAP_SERVERS", "SESSION_SECRET",
        ):
            os.environ.pop(key, None)
        portal_config.get_settings.cache_clear()
