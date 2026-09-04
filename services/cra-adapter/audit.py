"""
services/cra-adapter/audit.py
=============================
Audit-trail writer for the CRA Adapter (task 19.1).

Every access to CRA T4 data MUST be logged to the immutable ClickHouse
``adapter_audit_log`` table within **1 second** of the access, recording:

  - the Claim_ID,
  - the component (always ``"cra-adapter"``),
  - the fields that were accessed (``fields_accessed``),
  - a timestamp,

and, per Protected B handling, **never** the raw SIN — only the ``sin_hash``.

The writer is built around an injectable :class:`AuditSink` so it is fully
unit-testable with an in-memory fake — no live ClickHouse is required. In
production the sink is backed by ``clickhouse-connect`` (imported lazily and
degrading gracefully when unavailable, so an audit hiccup never crashes a claim
lookup).

Requirements: 11.5, 11.6, 19.3
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

logger = logging.getLogger("cra-adapter.audit")

#: This component's identity, recorded in every audit row.
COMPONENT_NAME: str = "cra-adapter"

#: ClickHouse database + table for the adapter audit trail.
ADAPTER_AUDIT_TABLE: str = "ei_ads_analytics.adapter_audit_log"

#: Budget within which every access must be logged (Requirement 11.5).
DEFAULT_AUDIT_BUDGET_MS: float = 1000.0


class AuditSink(Protocol):
    """Minimal audit-sink surface: insert one row into the audit table."""

    def insert(self, table: str, row: dict[str, Any]) -> None:  # pragma: no cover
        ...


class InMemoryAuditSink:
    """A fake :class:`AuditSink` that records rows in memory (for tests)."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def insert(self, table: str, row: dict[str, Any]) -> None:
        self.rows.append({"table": table, **row})


class ClickHouseAuditSink:
    """Production :class:`AuditSink` backed by ``clickhouse-connect``.

    The client is created lazily. Import and connection are best-effort: if the
    driver or server is unavailable the sink logs and no-ops so audit failures
    never crash a lookup.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 8123,
        username: str = "default",
        password: str = "",
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._client: Any = None
        self._unavailable = False

    def _get_client(self) -> Any:
        if self._client is None and not self._unavailable:
            try:
                import clickhouse_connect

                self._client = clickhouse_connect.get_client(
                    host=self._host,
                    port=self._port,
                    username=self._username,
                    password=self._password,
                )
            except Exception as exc:  # noqa: BLE001 - degrade gracefully
                logger.warning("ClickHouse audit sink unavailable: %s", exc)
                self._unavailable = True
                self._client = None
        return self._client

    def insert(self, table: str, row: dict[str, Any]) -> None:
        client = self._get_client()
        if client is None:
            return
        columns = list(row.keys())
        data = [[row[c] for c in columns]]
        client.insert(table, data, column_names=columns)


class AuditWriter:
    """Writes CRA-access audit rows to ClickHouse within the 1 s budget.

    Parameters
    ----------
    sink:
        The audit sink. Optional; when ``None``, audit writes no-op (still
        returning the row so callers/tests can inspect it) so the adapter is
        unaffected in environments without ClickHouse.
    budget_ms:
        The audit latency budget in milliseconds (default 1000 ms).
    """

    def __init__(
        self, sink: Optional[AuditSink] = None, *, budget_ms: float = DEFAULT_AUDIT_BUDGET_MS
    ) -> None:
        self._sink = sink
        self._budget_ms = budget_ms

    def build_row(
        self,
        claim_id: str,
        fields_accessed: list[str],
        *,
        sin_hash: str = "",
        outcome: str = "SUCCESS",
        actor_role: str = "SYSTEM",
        request_id: Optional[str] = None,
        duration_ms: Optional[float] = None,
    ) -> dict[str, Any]:
        """Construct an ``adapter_audit_log`` row for a CRA access.

        ``component`` is fixed to ``"cra-adapter"``. The raw SIN is never
        included — only ``sin_hash`` (normalised to a 64-char hex string).
        """
        return {
            "log_id": str(uuid.uuid4()),
            "claim_id": claim_id,
            "component": COMPONENT_NAME,
            "action": "t4_fetch",
            "fields_accessed": list(fields_accessed),
            "outcome": outcome,
            "actor_role": actor_role,
            # sin_hash column is FixedString(64); never the raw SIN.
            "sin_hash": (sin_hash or "").ljust(64, "0")[:64],
            "request_id": request_id or str(uuid.uuid4()),
            "duration_ms": float(duration_ms) if duration_ms is not None else None,
            "logged_at": datetime.now(timezone.utc),
        }

    async def log_access(
        self,
        claim_id: str,
        fields_accessed: list[str],
        *,
        sin_hash: str = "",
        outcome: str = "SUCCESS",
        actor_role: str = "SYSTEM",
        request_id: Optional[str] = None,
        duration_ms: Optional[float] = None,
    ) -> dict[str, Any]:
        """Log a single CRA access, returning the row that was written.

        The insert runs in a worker thread (``clickhouse-connect`` is
        synchronous) so the event loop is never blocked. A budget overrun is
        logged but never raised; audit failures are swallowed so a failed write
        never blocks or crashes claim processing. The row is always returned so
        tests can assert on it.
        """
        row = self.build_row(
            claim_id,
            fields_accessed,
            sin_hash=sin_hash,
            outcome=outcome,
            actor_role=actor_role,
            request_id=request_id,
            duration_ms=duration_ms,
        )

        if self._sink is None:
            logger.debug("no audit sink configured; skipping audit for claim %s", claim_id)
            return row

        start = time.monotonic()
        try:
            await asyncio.to_thread(self._sink.insert, ADAPTER_AUDIT_TABLE, row)
        except Exception as exc:  # noqa: BLE001 - never fail the lookup on audit
            logger.error("failed to write CRA audit entry for claim %s: %s", claim_id, exc)
            return row

        elapsed_ms = (time.monotonic() - start) * 1000.0
        if elapsed_ms > self._budget_ms:
            logger.warning(
                "CRA audit write for claim %s took %.1f ms, exceeding the %d ms budget",
                claim_id,
                elapsed_ms,
                self._budget_ms,
            )
        return row


__all__ = [
    "COMPONENT_NAME",
    "ADAPTER_AUDIT_TABLE",
    "DEFAULT_AUDIT_BUDGET_MS",
    "AuditSink",
    "InMemoryAuditSink",
    "ClickHouseAuditSink",
    "AuditWriter",
]
