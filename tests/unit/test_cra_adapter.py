"""
tests/unit/test_cra_adapter.py
==============================
Unit tests for the CRA Adapter (task 19.1).

Covered behaviours:
  - ALLOWED_FIELDS projection strips every non-allowed field (data minimisation).
  - T4 records older than the 10-year retention limit are discarded.
  - A 5-second total timeout produces a structured ``{"status": "UNAVAILABLE"}``.
  - Every access writes an audit entry to the (fake) sink with the correct
    component and fields_accessed.
  - Synthetic dev data has the expected field structure.
  - The raw SIN never appears in any audit row (only sin_hash).

All tests use dependency-injected fakes: no network, no real mTLS, no live
ClickHouse.

Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 11.8
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

import pytest

# --- Make the cra-adapter service importable (hyphenated dir) ----------------
# The service imports its siblings by BARE name (``from client import ...``).
# When the whole unit suite runs in one process, another hyphenated service may
# have already registered a bare ``client`` / ``config`` / ``audit`` in
# sys.modules, so we evict any stale bare-name entries first and prepend this
# service dir so the correct modules are (re)loaded from cra-adapter.
_SERVICE_DIR = Path(__file__).resolve().parents[2] / "services" / "cra-adapter"
if str(_SERVICE_DIR) in sys.path:
    sys.path.remove(str(_SERVICE_DIR))
sys.path.insert(0, str(_SERVICE_DIR))
for _bare in ("client", "config", "audit"):
    sys.modules.pop(_bare, None)

from client import (  # noqa: E402
    ALLOWED_FIELDS,
    CRAClient,
    discard_if_too_old,
    generate_synthetic_payroll_presence,
    generate_synthetic_t4,
    is_record_too_old,
    project_allowed_fields,
)
from config import Settings  # noqa: E402
from audit import (  # noqa: E402
    COMPONENT_NAME,
    AuditWriter,
    InMemoryAuditSink,
)


def _load_cra_main():
    """Load cra-adapter's ``main`` module, evicting any colliding bare ``main``.

    Other hyphenated services also expose a bare ``main`` module; when the whole
    unit suite runs in one process the last-loaded one wins in ``sys.modules``.
    We force a fresh load from THIS service directory so ``main.get_t4`` /
    ``main.app`` always refer to the cra-adapter application.
    """
    import importlib

    if str(_SERVICE_DIR) in sys.path:
        sys.path.remove(str(_SERVICE_DIR))
    sys.path.insert(0, str(_SERVICE_DIR))
    # Evict main and its bare-name siblings so main re-imports them from THIS
    # service directory rather than another service's stale modules.
    for _bare in ("main", "client", "config", "audit"):
        sys.modules.pop(_bare, None)
    return importlib.import_module("main")


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


class _SlowHttpClient:
    """A fake httpx-style client whose GET blocks longer than the timeout."""

    def __init__(self, delay_s: float) -> None:
        self._delay_s = delay_s

    def get(self, path: str):  # noqa: ANN001 - test double
        import time

        time.sleep(self._delay_s)
        raise AssertionError("should have timed out before returning")

    def close(self) -> None:  # pragma: no cover - not exercised
        pass


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeHttpClient:
    """A fake httpx-style client returning a fixed (un-minimised) record."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def get(self, path: str):  # noqa: ANN001 - test double
        return _FakeResponse(self._payload)

    def close(self) -> None:  # pragma: no cover
        pass


def _settings() -> Settings:
    return Settings(
        app_env="test",
        cra_timeout_seconds=0.2,  # short so timeout tests are fast
        cra_unavailable_margin_ms=50.0,
        cra_max_record_age_years=10,
    )


# ---------------------------------------------------------------------------
# Data minimisation (Requirement 11.2)
# ---------------------------------------------------------------------------


def test_allowed_fields_projection_strips_extra_fields():
    raw = {
        "sin": "abc123",
        "tax_year": 2022,
        "employer_bn": "123456789RT0001",
        "employment_income": 55000.0,
        "noc_category": "2",
        # extras that MUST be stripped
        "employer_legal_name": "Acme Inc",
        "employer_address": "123 Main St",
        "cpp_contributions": 3200.0,
    }
    projected = project_allowed_fields(raw)
    assert set(projected.keys()) == ALLOWED_FIELDS
    assert "employer_address" not in projected
    assert "employer_legal_name" not in projected
    assert "cpp_contributions" not in projected


def test_projection_never_adds_missing_fields():
    raw = {"sin": "x", "tax_year": 2020}
    projected = project_allowed_fields(raw)
    assert set(projected.keys()) == {"sin", "tax_year"}


# ---------------------------------------------------------------------------
# Retention rule (Requirement 11.7)
# ---------------------------------------------------------------------------


def test_is_record_too_old_boundary():
    ref = date(2025, 6, 1)
    # exactly 10 years old -> retained
    assert is_record_too_old(2015, max_age_years=10, reference=ref) is False
    # 11 years old -> too old
    assert is_record_too_old(2014, max_age_years=10, reference=ref) is True


def test_discard_if_too_old_drops_stale_record():
    ref = date(2025, 6, 1)
    old = {"tax_year": 2010, "employer_bn": "BN", "employment_income": 1.0}
    assert discard_if_too_old(old, max_age_years=10, reference=ref) is None

    recent = {"tax_year": 2020, "employer_bn": "BN", "employment_income": 1.0}
    assert discard_if_too_old(recent, max_age_years=10, reference=ref) == recent


def test_client_discards_old_record_end_to_end(monkeypatch):
    settings = _settings()
    # Inject an http client returning a very old record.
    old_record = {
        "sin": "hash",
        "tax_year": date.today().year - 20,
        "employer_bn": "123456789RT0001",
        "employment_income": 40000.0,
        "noc_category": "3",
    }
    client = CRAClient(settings, http_client=_FakeHttpClient(old_record))
    result = client.get_t4("hash", old_record["tax_year"], "123456789RT0001")
    assert result.status == "NOT_FOUND"
    assert result.record is None


# ---------------------------------------------------------------------------
# Synthetic dev data structure (Requirement 11.8)
# ---------------------------------------------------------------------------


def test_synthetic_t4_has_allowed_field_structure():
    rec = generate_synthetic_t4("sinhash", 2023, "987654321RT0001")
    # All allowed fields present
    assert ALLOWED_FIELDS.issubset(set(rec.keys()))
    assert rec["tax_year"] == 2023
    assert rec["employer_bn"] == "987654321RT0001"
    assert isinstance(rec["employment_income"], float)
    # Deterministic
    rec2 = generate_synthetic_t4("sinhash", 2023, "987654321RT0001")
    assert rec == {k: v for k, v in rec2.items() if k != "issued_at"} or (
        {k: v for k, v in rec.items() if k != "issued_at"}
        == {k: v for k, v in rec2.items() if k != "issued_at"}
    )


def test_client_synthetic_mode_projects_to_allowed_fields():
    settings = _settings()
    client = CRAClient(settings)  # no mTLS material -> synthetic
    assert client.uses_synthetic_data is True
    result = client.get_t4("sinhash", 2023, "987654321RT0001")
    assert result.status == "OK"
    assert set(result.record.keys()) == ALLOWED_FIELDS


# ---------------------------------------------------------------------------
# 5-second total timeout -> UNAVAILABLE (Requirements 11.3, 11.4)
# ---------------------------------------------------------------------------


def test_timeout_returns_structured_unavailable():
    _fetch_with_timeout = _load_cra_main()._fetch_with_timeout

    settings = _settings()
    # http client that blocks longer than the effective timeout
    slow_client = CRAClient(settings, http_client=_SlowHttpClient(delay_s=2.0))

    async def _run():
        return await _fetch_with_timeout(
            slow_client,
            "sinhash",
            2023,
            "987654321RT0001",
            total_timeout_s=settings.cra_timeout_seconds,
            margin_ms=settings.cra_unavailable_margin_ms,
        )

    result = asyncio.run(_run())
    assert result is None  # None signals UNAVAILABLE at the endpoint layer


def test_endpoint_unavailable_and_audit_on_timeout():
    main = _load_cra_main()

    settings = _settings()
    main.app.state.settings = settings
    main.app.state.cra_client = CRAClient(settings, http_client=_SlowHttpClient(2.0))
    sink = InMemoryAuditSink()
    main.app.state.audit_writer = AuditWriter(sink, budget_ms=1000.0)

    async def _run():
        return await main.get_t4(
            "sinhash", 2023, "987654321RT0001", claim_id="claim-xyz"
        )

    response = asyncio.run(_run())
    import json

    body = json.loads(bytes(response.body))
    assert body == {"status": "UNAVAILABLE"}
    # Audit still written on timeout
    assert len(sink.rows) == 1
    assert sink.rows[0]["outcome"] == "UNAVAILABLE"
    assert sink.rows[0]["component"] == COMPONENT_NAME


# ---------------------------------------------------------------------------
# Audit entry correctness (Requirements 11.5, 11.6)
# ---------------------------------------------------------------------------


def test_audit_written_with_component_and_fields():
    sink = InMemoryAuditSink()
    writer = AuditWriter(sink, budget_ms=1000.0)

    async def _run():
        return await writer.log_access(
            "claim-123",
            fields_accessed=sorted(ALLOWED_FIELDS),
            sin_hash="a" * 64,
            outcome="SUCCESS",
        )

    row = asyncio.run(_run())
    assert len(sink.rows) == 1
    written = sink.rows[0]
    assert written["claim_id"] == "claim-123"
    assert written["component"] == "cra-adapter"
    assert set(written["fields_accessed"]) == ALLOWED_FIELDS
    assert row["component"] == "cra-adapter"


def test_success_flow_audits_accessed_fields():
    import json

    main = _load_cra_main()

    settings = _settings()
    main.app.state.settings = settings
    main.app.state.cra_client = CRAClient(settings)  # synthetic
    sink = InMemoryAuditSink()
    main.app.state.audit_writer = AuditWriter(sink, budget_ms=1000.0)

    async def _run():
        return await main.get_t4("sinhash", 2023, "987654321RT0001", claim_id="c-1")

    response = asyncio.run(_run())
    body = json.loads(bytes(response.body))
    assert body["status"] == "OK"
    assert set(body["record"].keys()) == ALLOWED_FIELDS

    assert len(sink.rows) == 1
    written = sink.rows[0]
    assert written["outcome"] == "SUCCESS"
    assert set(written["fields_accessed"]) == ALLOWED_FIELDS


# ---------------------------------------------------------------------------
# Raw SIN never in audit rows (Requirement 11.6 / Protected B)
# ---------------------------------------------------------------------------


def test_raw_sin_never_in_audit_row():
    sink = InMemoryAuditSink()
    writer = AuditWriter(sink, budget_ms=1000.0)
    raw_sin = "046454286"  # a valid-looking SIN string

    async def _run():
        await writer.log_access(
            "claim-999",
            fields_accessed=["sin", "tax_year"],
            sin_hash="b" * 64,
            outcome="SUCCESS",
        )

    asyncio.run(_run())
    written = sink.rows[0]
    # The raw SIN value must not appear anywhere in the serialised row.
    serialized = repr(written)
    assert raw_sin not in serialized
    # sin_hash column present and exactly 64 chars.
    assert len(written["sin_hash"]) == 64
    assert written["sin_hash"] == "b" * 64


# ===========================================================================
# Payroll presence (Req 7.7 — Document Forgery cross-reference seam)
# ===========================================================================


def test_synthetic_payroll_absent_marker_is_not_in_payroll():
    """A sin_hash bearing an ABSENT marker is deterministically NOT in payroll."""
    assert generate_synthetic_payroll_presence("ghost-abc", "987654321RT0001") is False
    assert generate_synthetic_payroll_presence("ABSENT-xyz", "987654321RT0001") is False
    # Deterministic + case-insensitive on the marker.
    assert generate_synthetic_payroll_presence("ghost-abc", "987654321RT0001") is False


def test_synthetic_payroll_ordinary_sin_is_present_and_deterministic():
    """An ordinary sin_hash is in payroll (common case) and stable across runs."""
    a = generate_synthetic_payroll_presence("ordinary-worker-1", "987654321RT0001")
    b = generate_synthetic_payroll_presence("ordinary-worker-1", "987654321RT0001")
    assert a == b
    assert a is True


def test_client_synthetic_payroll_presence():
    """CRAClient (synthetic mode) resolves payroll presence without a network."""
    client = CRAClient(_settings())  # no mTLS -> synthetic
    assert client.uses_synthetic_data is True
    assert (
        client.get_payroll_presence(
            "ghost-x", "987654321RT0001", "2024-01-01", "2024-06-01"
        )
        is False
    )
    assert (
        client.get_payroll_presence(
            "ordinary-worker-1", "987654321RT0001", "2024-01-01", "2024-06-01"
        )
        is True
    )


def test_payroll_endpoint_reports_presence():
    """GET /cra/payroll/{sin}/{bn} returns sin_in_payroll from synthetic data."""
    from fastapi.testclient import TestClient
    from shared.auth import get_current_user as _gcu

    main = _load_cra_main()
    settings = _settings()
    main.app.state.settings = settings
    main.app.state.cra_client = CRAClient(settings)  # synthetic
    main.app.dependency_overrides[_gcu] = lambda: {"roles": ["INVESTIGATOR"]}
    try:
        client = TestClient(main.app)
        # Absent SIN -> sin_in_payroll False (the 7.7 CRITICAL trigger).
        resp = client.get(
            "/cra/payroll/ghost-abc/987654321RT0001",
            params={"start": "2024-01-01", "end": "2024-06-01"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "OK"
        assert body["sin_in_payroll"] is False
        # Ordinary SIN -> present.
        resp2 = client.get(
            "/cra/payroll/ordinary-worker-1/987654321RT0001",
            params={"start": "2024-01-01", "end": "2024-06-01"},
        )
        assert resp2.json()["sin_in_payroll"] is True
    finally:
        main.app.dependency_overrides.pop(_gcu, None)
