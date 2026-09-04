"""
tests/integration/conftest.py
==============================
Integration-suite-scoped fixtures and container-runtime capability detection.

Task 33.2 (and the sibling 33.1 / 33.3 integration tasks) want to exercise the
real infrastructure — Kafka, ClickHouse, PostgreSQL — via ``testcontainers``.
None of that is available in the CI sandbox (no ``testcontainers`` package and,
typically, no reachable container daemon), so every container-backed test MUST
be guarded so the suite *collects and passes* without a runtime.

This module centralises that capability detection behind small, clearly-scoped
helpers and fixtures so each sibling integration file can share one consistent
skip decision instead of re-implementing the probe:

  * :func:`container_runtime_available` — is there a reachable Docker/Podman
    daemon AND the ``testcontainers`` package?
  * :func:`python_driver_available`     — is a given client driver importable
    (``clickhouse_connect``, ``asyncpg``, ``aiokafka``)?

The fixtures below are deliberately namespaced with an ``itest_`` prefix and are
additive: they do not redefine or shadow any fixture from the project-level
``tests/conftest.py`` (which only provides ``load_service_module``), so they are
safe to add alongside whatever the sibling 33.1 / 33.3 tasks contribute.

Running the container-backed tests locally
------------------------------------------
    # 1. Install the optional test dependencies (already pinned in the design):
    pip install testcontainers==4.9.0 clickhouse-connect==0.8.12
    # 2. Ensure a container runtime is up and reachable:
    #      Docker Desktop / colima / podman machine  (DOCKER_HOST or
    #      /var/run/docker.sock must be reachable)
    # 3. Run only the integration suite:
    pytest tests/integration -m integration -v
"""

from __future__ import annotations

import importlib.util
import os
import socket
from urllib.parse import urlparse

import pytest


# ---------------------------------------------------------------------------
# Capability probes (module-level so skipif decorators can call them)
# ---------------------------------------------------------------------------


def python_driver_available(module_name: str) -> bool:
    """Return ``True`` when ``module_name`` can be imported in this interpreter.

    Used to guard tests that need optional client drivers such as
    ``clickhouse_connect`` (ClickHouse) or ``asyncpg`` (PostgreSQL) which are not
    installed in the sandbox.
    """
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):  # pragma: no cover - defensive
        return False


def _daemon_socket_reachable() -> tuple[bool, str]:
    """Best-effort check that a container daemon is actually reachable.

    ``/var/run/docker.sock`` frequently *exists* on developer machines (e.g. a
    dangling symlink from Docker Desktop) even when nothing is listening, so we
    additionally try to connect to it / to ``$DOCKER_HOST`` rather than trusting
    the path to exist. A ``tcp://`` ``DOCKER_HOST`` is probed with a short TCP
    connect; a unix socket is probed with an ``AF_UNIX`` connect.
    """
    docker_host = os.environ.get("DOCKER_HOST", "").strip()

    if docker_host:
        parsed = urlparse(docker_host)
        if parsed.scheme in ("tcp", "http", "https") and parsed.hostname:
            port = parsed.port or (2376 if parsed.scheme == "https" else 2375)
            try:
                with socket.create_connection((parsed.hostname, port), timeout=1.0):
                    return True, ""
            except OSError as exc:
                return False, f"DOCKER_HOST {docker_host} unreachable ({exc})"
        if parsed.scheme == "unix" and parsed.path:
            return _probe_unix_socket(parsed.path)
        # Unknown scheme — assume the caller knows what they are doing.
        return True, ""

    # No DOCKER_HOST: fall back to the conventional unix socket path.
    default_sock = "/var/run/docker.sock"
    if os.path.exists(default_sock):
        return _probe_unix_socket(default_sock)
    return False, "no container daemon (Docker/Podman) socket found"


def _probe_unix_socket(path: str) -> tuple[bool, str]:
    """Try an ``AF_UNIX`` connect to ``path`` to confirm a daemon is listening."""
    if not hasattr(socket, "AF_UNIX"):  # pragma: no cover - Windows
        return os.path.exists(path), ""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(1.0)
    try:
        sock.connect(path)
        return True, ""
    except OSError as exc:
        return False, f"container daemon socket {path} not listening ({exc})"
    finally:
        sock.close()


def container_runtime_available() -> tuple[bool, str]:
    """Report whether ``testcontainers`` + a reachable daemon are both present.

    Returns ``(available, reason)`` — ``reason`` is a human-readable skip message
    when ``available`` is ``False`` (empty otherwise). Container-backed tests use
    this to ``pytest.mark.skipif`` themselves rather than failing the suite.
    """
    if importlib.util.find_spec("testcontainers") is None:
        return False, "testcontainers package not installed"
    return _daemon_socket_reachable()


# Evaluate once at import time so decorators get a stable reason string.
CONTAINER_RUNTIME_AVAILABLE, CONTAINER_RUNTIME_SKIP_REASON = container_runtime_available()


# ---------------------------------------------------------------------------
# Fixtures (all namespaced ``itest_`` to avoid clashing with sibling tasks)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def itest_container_runtime():
    """Skip the requesting test unless a container runtime is available.

    Yields the ``(available, reason)`` tuple for tests that want to introspect
    it; skips with a clear reason otherwise.
    """
    if not CONTAINER_RUNTIME_AVAILABLE:
        pytest.skip(CONTAINER_RUNTIME_SKIP_REASON or "container runtime unavailable")
    yield (CONTAINER_RUNTIME_AVAILABLE, CONTAINER_RUNTIME_SKIP_REASON)


@pytest.fixture
def itest_audit_log_migration_sql() -> str:
    """Return the SQL text of the PostgreSQL ``audit_log`` migration (004).

    Container-backed tests apply this against a real PostgreSQL instance to
    reproduce the INSERT-only Row-Level Security policy that enforces audit-log
    immutability (Requirement 19.3).
    """
    repo_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..")
    )
    migration = os.path.join(
        repo_root, "scripts", "migrations", "004_create_audit_log.sql"
    )
    with open(migration, "r", encoding="utf-8") as handle:
        return handle.read()
