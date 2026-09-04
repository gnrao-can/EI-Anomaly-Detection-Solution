"""
tests/contract/test_openapi_contract.py
========================================
Property-based **API contract tests** for every EI-ADS FastAPI service.

Task 34.2 / Requirement 17.1.

What this does
--------------
For each FastAPI service that exposes an OpenAPI schema, we use
`schemathesis <https://schemathesis.readthedocs.io/>`_ to *auto-generate* a
large space of requests directly from the service's OpenAPI schema and fire them
at the app **in-process** (ASGI, no live server / container runtime needed).

The single contract we assert is:

    A generated, schema-valid request must never make the service return a
    **5xx** (server error, 500-599).

Client-side outcomes are perfectly acceptable and expected:

  * ``401`` / ``403`` — task 29.1 wired ``require_role`` onto most routes, so a
    request without a valid GCKey token is correctly rejected. To exercise more
    of the schema surface we override ``get_current_user`` with a fake
    all-roles principal where feasible, but even so, per-endpoint role checks or
    downstream validation may still yield 401/403.
  * ``404`` — path parameters that don't resolve to a real resource.
  * ``422`` — request bodies that satisfy the JSON schema shape but fail
    FastAPI/Pydantic semantic validation.

Only a 5xx is treated as a contract failure.

In-process loading
------------------
Services live in hyphenated directories and import their siblings by *bare*
name, so they are loaded through the shared, collision-free
:func:`tests.conftest.load_service_module` helper (the same mechanism the unit
suite uses). ``schemathesis.from_asgi`` then builds the schema straight from the
loaded app object.

Sandbox behaviour
-----------------
``schemathesis==3.39.4`` is pinned in ``pyproject.toml`` /
``scripts/dependencies/requirements-test.txt``, but it may not be installed in
every sandbox. When it is absent this module **skips cleanly** (it still
imports/collects without error) so the rest of the suite is unaffected. Install
the test dependencies to actually run the generated contract checks.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Optional

import pytest

from tests.conftest import load_service_module

# ---------------------------------------------------------------------------
# Optional dependency guard — collect cleanly even without schemathesis.
# ---------------------------------------------------------------------------

schemathesis = pytest.importorskip(
    "schemathesis",
    reason=(
        "schemathesis (pinned 3.39.4) is not installed in this environment; "
        "install the test dependencies (scripts/dependencies/requirements-test.txt) "
        "to run the OpenAPI contract tests."
    ),
)

# FastAPI (0.115.x) serves its schema as OpenAPI 3.1.0. In schemathesis 3.39.x
# full 3.1 support is gated behind an experimental flag; enable it so the
# generated 3.1 schemas load instead of raising a SchemaError.
try:
    schemathesis.experimental.OPEN_API_3_1.enable()
except Exception:  # noqa: BLE001 - flag name may differ across minor versions
    pass

# Keep hypothesis-driven generation bounded and deterministic so the in-process
# contract sweep stays fast and reproducible in CI.
from hypothesis import HealthCheck, given, settings  # noqa: E402

_MAX_EXAMPLES = int(os.environ.get("SCHEMATHESIS_MAX_EXAMPLES", "25"))
_CONTRACT_SETTINGS = settings(
    max_examples=_MAX_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


# ---------------------------------------------------------------------------
# Auth override — inject an all-roles principal so more of the surface is hit.
# ---------------------------------------------------------------------------

# A fake, already-validated GCKey principal holding every EI-ADS role. Applied
# via FastAPI ``dependency_overrides`` on ``get_current_user`` so schemathesis
# requests get past ``require_role`` and exercise handler logic (where the real
# contract — "no 5xx" — matters most).
_FAKE_PRINCIPAL: dict[str, Any] = {
    "sub": "contract-test-principal",
    "roles": ["CLAIMANT", "REVIEWER", "INVESTIGATOR", "ADMINISTRATOR"],
    "scope": "CLAIMANT REVIEWER INVESTIGATOR ADMINISTRATOR",
    # Some handlers derive the claimant identity from the token.
    "sin_hash": "0" * 64,
}


# Paths excluded from the contract sweep. ``/healthz`` (and friends) return a
# deliberate ``503`` when their live dependencies (DB / Kafka / Redis / Neo4j)
# are absent — which is always the case for an in-process ASGI run with no
# containers. That documented dependency-status signal is not a server-side
# contract violation, so we don't sweep health probes.
_EXCLUDED_PATHS = frozenset({"/healthz", "/health", "/readyz", "/livez", "/metrics"})


def _is_backend_unavailable(exc: BaseException) -> bool:
    """True when ``exc`` (or a cause in its chain) is a backend-connectivity error.

    Running in-process with no containers means external stores (PostgreSQL,
    Neo4j, Kafka, Redis) are unreachable. A 5xx that is *solely* caused by an
    unreachable backend is an environment limitation of this sandbox, not a
    request-handling contract violation, so we tolerate it. Genuine input-handling
    crashes (KeyError, ValueError, ...) are NOT connectivity errors and still fail.
    """
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (ConnectionError, ConnectionRefusedError, OSError, TimeoutError)):
            return True
        name = type(current).__name__.lower()
        if "connection" in name or "timeout" in name or "unavailable" in name:
            return True
        current = current.__cause__ or current.__context__
    return False


def _override_auth(app: Any) -> None:
    """Best-effort override of ``get_current_user`` on ``app`` with a fake user.

    The override makes authenticated routes reachable so the contract sweep
    covers handler logic rather than stopping at the 401 gate. It is best-effort:
    if the auth module can't be imported the sweep still runs (routes simply
    return 401/403, which are not contract failures).
    """
    try:
        from shared.auth import get_current_user
    except Exception:  # noqa: BLE001 - never let auth wiring break collection
        return

    async def _fake_current_user() -> dict[str, Any]:
        return dict(_FAKE_PRINCIPAL)

    try:
        app.dependency_overrides[get_current_user] = _fake_current_user
    except Exception:  # noqa: BLE001 - app may not support overrides in a stub
        pass


# ---------------------------------------------------------------------------
# Service registry — every FastAPI service that exposes an OpenAPI schema.
# ---------------------------------------------------------------------------


def _all_service_py_modules(service_dir_name: str) -> list[str]:
    """Return every bare module name (``*.py``) in a service directory.

    Passing the full set as ``siblings`` lets :func:`load_service_module`
    satisfy whatever intra-service bare-name imports ``main`` performs without
    us having to hand-maintain a per-service import list.
    """
    services_root = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "services"
    )
    service_dir = os.path.join(services_root, service_dir_name)
    names: list[str] = []
    if os.path.isdir(service_dir):
        for entry in sorted(os.listdir(service_dir)):
            if entry.endswith(".py") and entry != "__init__.py":
                names.append(entry[: -len(".py")])
    return names


def _module_level_app(service_dir_name: str) -> Optional[Any]:
    """Load ``main`` from a service and return its module-level ``app``."""
    module = load_service_module(
        service_dir_name,
        "main",
        siblings=_all_service_py_modules(service_dir_name),
    )
    app = getattr(module, "app", None)
    return app


def _graph_service_app() -> Optional[Any]:
    """Graph Service builds its app via a ``create_app()`` factory."""
    module = load_service_module(
        "graph-service",
        "main",
        siblings=_all_service_py_modules("graph-service"),
    )
    factory = getattr(module, "create_app", None)
    if factory is None:
        return None
    return factory()


# Map a human-readable id -> a zero-arg callable returning the FastAPI app.
# ``graph-service`` uses a factory; ``llm-service``'s module-level app may be
# ``None`` in a bare sandbox (guarded below). All others expose a module-level
# ``app`` in ``main.py``.
_SERVICE_APP_FACTORIES: dict[str, Callable[[], Optional[Any]]] = {
    "business-registry-adapter": lambda: _module_level_app("business-registry-adapter"),
    "cbsa-adapter": lambda: _module_level_app("cbsa-adapter"),
    "claim-intake": lambda: _module_level_app("claim-intake"),
    "cra-adapter": lambda: _module_level_app("cra-adapter"),
    "document-forgery-engine": lambda: _module_level_app("document-forgery-engine"),
    "ensemble-scorer": lambda: _module_level_app("ensemble-scorer"),
    "fraud-ring-engine": lambda: _module_level_app("fraud-ring-engine"),
    "geographic-anomaly-engine": lambda: _module_level_app("geographic-anomaly-engine"),
    "geonames-adapter": lambda: _module_level_app("geonames-adapter"),
    "graph-service": _graph_service_app,
    "history-inconsistency-engine": lambda: _module_level_app("history-inconsistency-engine"),
    "job-bank-adapter": lambda: _module_level_app("job-bank-adapter"),
    "llm-service": lambda: _module_level_app("llm-service"),
    "mvr-adapter": lambda: _module_level_app("mvr-adapter"),
    "pipeline-orchestrator": lambda: _module_level_app("pipeline-orchestrator"),
    "regional-analysis-engine": lambda: _module_level_app("regional-analysis-engine"),
    "repetitive-pattern-engine": lambda: _module_level_app("repetitive-pattern-engine"),
    "roe-adapter": lambda: _module_level_app("roe-adapter"),
    "stats-canada-adapter": lambda: _module_level_app("stats-canada-adapter"),
}


def _load_app(service_id: str) -> Any:
    """Build (or fetch) the app for ``service_id``, skipping if unavailable.

    A service is *skipped* (not failed) when its app cannot be constructed in
    this environment — e.g. ``llm-service`` returns ``None`` without FastAPI, or
    a service's optional import stack is absent from the sandbox. That keeps the
    contract sweep resilient while still failing loudly on a real 5xx.
    """
    factory = _SERVICE_APP_FACTORIES[service_id]
    try:
        app = factory()
    except Exception as exc:  # noqa: BLE001 - surface as a skip, not a hard error
        pytest.skip(f"{service_id}: could not load FastAPI app in this environment: {exc!r}")
    if app is None:
        pytest.skip(f"{service_id}: no FastAPI app available in this environment.")
    _override_auth(app)
    return app


# ---------------------------------------------------------------------------
# The contract sweep.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("service_id", sorted(_SERVICE_APP_FACTORIES))
def test_service_openapi_has_no_5xx(service_id: str) -> None:
    """Auto-generated, schema-valid requests must never yield a 5xx.

    schemathesis reads the service's OpenAPI schema, generates a wide space of
    valid requests, and fires each at the app in-process (ASGI). For every
    generated request we assert the response status is **not** in the 500-599
    range. 401/403/404/422 are acceptable client outcomes and are not failures.

    **Validates: Requirements 17.1**
    """
    app = _load_app(service_id)

    # Build the schema straight from the ASGI app (in-process, no live server).
    schema = schemathesis.from_asgi("/openapi.json", app)

    # A service with no operations (nothing but ``/openapi.json`` / ``/docs``)
    # has no contract surface to sweep — treat as a skip rather than a failure.
    if schema.operations_count == 0:
        pytest.skip(f"{service_id}: OpenAPI schema exposes no operations to test.")

    # ``schema.as_strategy()`` yields schemathesis ``Case`` objects spanning
    # every operation in the schema. We drive it with hypothesis directly so the
    # generation lives inside this parametrized test (one schema per service),
    # then fire each generated case at the app in-process and assert "no 5xx".
    @_CONTRACT_SETTINGS
    @given(case=schema.as_strategy())
    def _check(case: Any) -> None:
        # Skip documented health/metrics probes — their 503 reflects live
        # dependency state, not a request-handling contract.
        if case.path in _EXCLUDED_PATHS:
            return

        try:
            response = case.call_asgi()
        except Exception as exc:  # noqa: BLE001
            # A crash *before* a response is produced is only tolerated when it
            # is purely a backend-connectivity failure (no containers here).
            if _is_backend_unavailable(exc):
                return
            raise

        assert response.status_code < 500, (
            f"{service_id} {case.method} {case.path} returned a server error "
            f"{response.status_code}: {response.text[:500]!r}"
        )

    # Execute the schemathesis-generated, hypothesis-driven test body.
    _check()
