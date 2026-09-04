# EI-ADS Dependency Versioning

## Strategy

All Python and npm dependencies are pinned to exact versions (no `^`, `~`, or `>=` ranges).
This ensures reproducible builds across all environments and satisfies Government of Canada
supply-chain security requirements (Requirement 19.6).

## Files

| File | Purpose |
|------|---------|
| `python-versions.toml` | Canonical version registry for all Python packages |
| `frontend-versions.json` | Canonical version registry for all npm packages |
| `requirements-shared.txt` | Shared library (all services) |
| `requirements-claim-intake.txt` | Claim Intake Service |
| `requirements-roe-intake.txt` | ROE Intake/Adapter Service |
| `requirements-orchestrator.txt` | Pipeline Orchestrator Service |
| `requirements-ml-engine.txt` | ML Engine / Ensemble Scorer |
| `requirements-llm-service.txt` | LLM / Explanation Engine Service |
| `requirements-graph-service.txt` | Graph Service |
| `requirements-detection-engines.txt` | All 6 Detection Engines (shared) |
| `requirements-test.txt` | Test and development tools |
| `EXCEPTIONS.md` | Accepted CVE exceptions with rationale |

## Updating Dependencies

1. Update the version in `python-versions.toml` or `frontend-versions.json`
2. Update the corresponding `requirements-*.txt` file
3. Run `pip-audit==2.7.3` against the updated file
4. Run `safety==3.2.11 check` against the updated file
5. If any CVE with CVSS >= 7.0 is found, either update to a patched version
   or document the exception in `EXCEPTIONS.md` with a GitHub issue reference
   and remediation date no more than 30 days out

## Security Gates

- Pre-commit hook: `pip-audit --strict --vulnerability-service osv`
- CI gate: `safety check --full-report --policy-file scripts/dependencies/safety-policy.yml`
- npm: `npm audit --audit-level=high`

## Local development on Python 3.11

EI-ADS targets **Python 3.11** (container base
`registry.access.redhat.com/ubi9/python-311`). The root `requirements.lock` is
fully pinned to the 3.11 stack (`torch==2.5.1`, `shap`, `transformers`,
`llama-cpp-python`, `sentence-transformers`, `prophet`, etc.).

> **`requirements.lock` cannot be installed on Python 3.13+.** `torch==2.5.1`
> (and several other pins) have no wheels for 3.13, so `pip install -r
> requirements.lock` aborts before anything installs and the services fail to
> boot with cascading `ModuleNotFoundError`s. Use Python 3.11 locally
> (`pyenv install 3.11.x` or `brew install python@3.11`).

### Run the four core services (no ML/LLM stack)

`scripts/dependencies/requirements-core.txt` is a strict, exact-pinned subset of
`requirements.lock` that is enough to boot the four core services —
claim-intake, roe-adapter, ensemble-scorer, graph-service — with all `/healthz`
endpoints returning 200. It excludes the heavy ML/LLM dependencies.

```bash
# From the repo root, with Python 3.11 active.
python3.11 -m venv .venv-core
source .venv-core/bin/activate
pip install -r scripts/dependencies/requirements-core.txt

# Make `import shared...` resolve when running services standalone.
export PYTHONPATH="$(pwd)"
```

> **Do NOT use `pip install -e .`** for this lightweight path — it pulls the
> full heavy dependency set from `pyproject.toml` / `requirements.lock`.

Launch each service from inside its own directory:

```bash
# claim-intake (port 8001)
cd services/claim-intake   && python -m uvicorn main:app --port 8001

# roe-adapter (port 8002)
cd services/roe-adapter    && python -m uvicorn main:app --port 8002

# ensemble-scorer (port 8004)
cd services/ensemble-scorer && python -m uvicorn main:app --port 8004

# graph-service (port 8006)
cd services/graph-service  && python -m uvicorn main:app --port 8006
```

### Run the core unit suites

Layer the test-only pins on top of the core subset, then run pytest against the
core service test files:

```bash
pip install -r scripts/dependencies/requirements-core-test.txt
pytest services/claim-intake services/roe-adapter services/ensemble-scorer services/graph-service
```

### Notes

- **ensemble-scorer** is guarded so the *service boots* without `numpy`, but
  scoring (features / SHAP paths) and its unit tests require it — which is why
  `numpy` is included in `requirements-core.txt`.
- **graph-service** imports `networkx` at import time (`community.py`), so it is
  also part of the core subset.

### Full stack

The lightweight core subset is intentionally insufficient for the ML detection
engines and the LLM / explanation service. Those require the container images or
a Python 3.11 environment with the complete `requirements.lock` installed.
