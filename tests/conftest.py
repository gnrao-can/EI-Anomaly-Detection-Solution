"""
tests/conftest.py
=================
Project-level pytest fixtures and shared test infrastructure for EI-ADS.

Shared service-module loader
----------------------------
Several services live in hyphenated directories (``services/pipeline-orchestrator``,
``services/cbsa-adapter``, ``services/ensemble-scorer``, ...) which are therefore
NOT importable as normal Python packages. Their modules also import each other by
*bare* name (``from correlator import ...``, ``from config import ...``,
``from client import ...``). When the whole ``tests/unit`` suite is collected and
run in a single process, those bare names collide in ``sys.modules`` — one
service's ``config`` / ``client`` / ``main`` overwrites another's — which causes
failures and collection errors that do NOT occur when a test file is run in
isolation.

:func:`load_service_module` fixes this cleanly. It loads a module from a
hyphenated service directory under a UNIQUE namespaced key
(e.g. ``"svc__pipeline-orchestrator__correlator"``) so it never collides with any
other service's module. While the target module executes, its bare-name sibling
imports are satisfied by temporarily registering the required siblings under
their bare keys, and any pre-existing bare-name modules are snapshotted and
restored afterwards. This is exactly the robust snapshot/restore pattern used in
``tests/unit/test_cbsa_adapter.py``, generalised for reuse.

Future tests SHOULD adopt :func:`load_service_module` instead of ad-hoc
``sys.path`` juggling, so the unit suite stays collision-free when run as a
whole.

Usage
-----
    from tests.conftest import load_service_module

    # Load `correlator.py` from services/pipeline-orchestrator, first making
    # sure its siblings are importable by bare name during execution:
    correlator = load_service_module(
        "pipeline-orchestrator", "correlator",
        siblings=["correlator"],
    )
    EventCorrelator = correlator.EventCorrelator
"""

from __future__ import annotations

import importlib.util
import os
import sys
from types import ModuleType
from typing import Iterable, Optional

# Repository root == the parent of the ``tests`` directory.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SERVICES_ROOT = os.path.join(_REPO_ROOT, "services")


def _service_dir(service_dir_name: str) -> str:
    """Return the absolute path to a service directory under ``services/``."""
    return os.path.join(_SERVICES_ROOT, service_dir_name)


def _namespaced_key(service_dir_name: str, module_name: str) -> str:
    """Build the unique, collision-free ``sys.modules`` key for a service module."""
    return f"svc__{service_dir_name}__{module_name}"


def load_service_module(
    service_dir_name: str,
    module_name: str,
    *,
    siblings: Optional[Iterable[str]] = None,
) -> ModuleType:
    """Load a module from a hyphenated service directory without name collisions.

    Parameters
    ----------
    service_dir_name:
        The directory name under ``services/`` (may contain a hyphen), e.g.
        ``"pipeline-orchestrator"`` or ``"ensemble-scorer"``.
    module_name:
        The bare module name to load (without ``.py``), e.g. ``"correlator"``.
    siblings:
        Optional list of bare module names (including ``module_name`` itself if
        the target imports itself/relatives by bare name) that the target module
        imports *by bare name* while executing. Each is loaded from the same
        service directory and temporarily registered under its bare key so those
        intra-service imports resolve. Any pre-existing bare-name entry is
        snapshotted and restored afterwards, leaving only the namespaced
        ``svc__<service>__<module>`` keys behind.

    Returns
    -------
    ModuleType
        The loaded target module. Re-loading the same (service, module) pair
        returns the cached namespaced module rather than executing it twice.

    Notes
    -----
    The service directory is placed on ``sys.path`` so that any ``import httpx``
    / ``from shared...`` style imports inside the service still resolve normally;
    only the *bare sibling* names are namespaced/restored.
    """
    service_dir = _service_dir(service_dir_name)
    if not os.path.isdir(service_dir):
        raise FileNotFoundError(f"service directory not found: {service_dir}")

    # Return the cached namespaced module if we've already loaded it.
    target_key = _namespaced_key(service_dir_name, module_name)
    cached = sys.modules.get(target_key)
    if cached is not None:
        return cached

    if service_dir not in sys.path:
        sys.path.insert(0, service_dir)

    bare_names = list(siblings) if siblings is not None else [module_name]
    # Ensure the target module's bare name is present so self/relative bare
    # imports work while it executes.
    if module_name not in bare_names:
        bare_names.append(module_name)

    # 1) Snapshot any pre-existing bare-name modules so we can restore them.
    saved: dict[str, Optional[ModuleType]] = {
        name: sys.modules.get(name) for name in bare_names
    }

    loaded: dict[str, ModuleType] = {}
    try:
        # 2) Load each sibling, registering it under BOTH its bare key (so
        #    sibling ``from X import Y`` resolves) and a namespaced key (stable,
        #    collision-free reference for tests).
        for bare in bare_names:
            filename = os.path.join(service_dir, f"{bare}.py")
            if not os.path.isfile(filename):
                # Skip siblings that don't exist as files (they may be provided
                # by shared/ or third-party packages, resolved via sys.path).
                continue
            spec = importlib.util.spec_from_file_location(bare, filename)
            if spec is None or spec.loader is None:  # pragma: no cover - defensive
                raise ImportError(f"cannot build import spec for {filename}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[bare] = module
            sys.modules[_namespaced_key(service_dir_name, bare)] = module
            spec.loader.exec_module(module)
            loaded[bare] = module
    finally:
        # 3) Restore the pre-existing bare-name modules, leaving only the
        #    namespaced ``svc__...`` entries behind.
        for name, previous in saved.items():
            if previous is not None:
                sys.modules[name] = previous
            else:
                sys.modules.pop(name, None)

    target = loaded.get(module_name) or sys.modules.get(target_key)
    if target is None:  # pragma: no cover - defensive
        raise ImportError(
            f"failed to load module {module_name!r} from service "
            f"{service_dir_name!r}"
        )
    return target
