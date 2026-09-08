# EI-ADS Regeneration — Status & Roadmap

Clean regeneration of the EI-ADS implementation from the authoritative inputs,
built as VERIFIED VERTICAL SLICES over real transports (not task-by-task with
mocked seams, which caused the prior fragility).

## Authoritative inputs (source of truth)
- `.kiro/specs/ei-anomaly-detection-solution/design.md`
- `.kiro/specs/ei-anomaly-detection-solution/requirements.md` (acceptance criteria + 15 properties)
- `.kiro/specs/ei-anomaly-detection-solution/tasks.md`
- `docs/architecture/FraudAI-Solution-Architecture.docx` (C4 context/component views; consistent with design.md)
- `demo/ads-dashboard-demo.html` + `demo/ei-fraud-demo.html` (UI look-and-feel + scenarios)

## Branches
- `archive/pre-regeneration-2026` (commit e59bba5) — FULL pre-regen snapshot. Safety net.
  Recover any old file: `git checkout archive/pre-regeneration-2026 -- <path>`.
- `main` (dd30258) — original scaffold, untouched.
- `regen/main` — active regeneration branch (built off the archive so preserved assets are present).

## Preserved assets (design-conforming — DO NOT regenerate)
- `tests/property/` — property tests P1–P15
- `shared/` — validation.py, models.py, auth.py, cache.py, cache_keys.py, telemetry.py, metrics.py
- `scripts/migrations/` — Postgres schema (claims, risk_assessments, indicators, audit_log)
- `mock-data/` — adapter fixtures (10 MVR, 13 registry, cbsa, geonames, stats-canada, job-bank)
- engine algorithm modules + adapter core logic (verify per slice; mostly correct)

## Done on regen/main
1. (662d39a) Removed 4 legacy duplicate service stubs: `orchestrator`, `roe-intake`,
   `ml-engine`, `explanation-engine` (each a 1-file stub duplicating a canonical service).
   Canonical set per tasks.md retained.
2. (0e10cae) Fixed the orchestrator→engine/scorer wiring — the core prior defect:
   - `pipeline-orchestrator/engine_dispatch.py`: `_default_engine_caller` now builds the
     engine's FLAT `AnalyzeRequest` (sin_hash + optional claim_id/employer_bn/insurable_hours/
     economic_region_code/separation_date) from the paired claim.received/roe.received
     payloads (was an incorrect `{claim,roe}` envelope). Added `_build_engine_request` +
     `_service_auth_headers`.
   - `pipeline-orchestrator/scoring.py`: scorer call now presents a Bearer token.
   - Both use `ORCHESTRATOR_SERVICE_TOKEN` (Req 19.7). Correlator/dispatch/scoring logic
     were already sound and unchanged.
   - Verified: orchestrator/dispatch/scoring/correlator tests = 33 passed, 2 skipped.
3. (a1b5c63) Test-auth seam (Option A) — see "Test-auth seam" below.
4. (dd37e97) **Audit-log immutability fix (Req 2.7, 19.3).** Found while grounding Slice 1
   against a real Postgres: migration 004's INSERT-only RLS policy did NOT make `audit_log`
   immutable — an UPDATE/DELETE silently reported `UPDATE 0` / `DELETE 0` (tamper tolerance,
   not denial). Verified against postgres:16-alpine. Fix: RLS enabled + INSERT/SELECT plus
   UPDATE/DELETE *visibility* policies (`USING (true)`) so rows are visible to a mutation
   attempt, + a `BEFORE UPDATE OR DELETE` trigger (`audit_log_reject_mutation`) raising
   `insufficient_privilege` (42501). Fires even for a role holding UPDATE/DELETE grants;
   INSERT (append) still works. Corrected the design.md "Audit Log Immutability" note (it
   claimed INSERT-only was sufficient) and the file's own SECURITY NOTE/COMMENT. The static
   test that encoded the wrong premise was rewritten (`test_audit_log_migration_enforces_
   immutability`) to assert the trigger contract; the container runtime test now passes.
5. (cbfd9be) **Slice 1 integration test — honest INDETERMINATE assertion (Req 2.5).** See
   "Slice 1" below. Removed an earlier six-from-one wiring hack.

## Key finding (scope correction)
The prior "systemic integration failure" was over-stated. The real pipeline-core defect
was localized: ONE request-body shape + TWO missing auth headers. The correlator, dispatch,
scoring, engine algorithms, intake, and validation were sound. The design is coherent; the
damage was in a thin wiring layer + duplicated/retrofitted services.

## Genuine deviations still to resolve (from design)
- **Claimant Portal backend**: RESOLVED (6f65603). Reworked to the design role:
  (A) real GCKey Bearer-forward — the GCKey access token is held in the signed HttpOnly
  session and attached as `Authorization: Bearer` on downstream Claim Intake calls
  (create_claim + Privacy Act proxies); token-less session → 401; (B) the dev-auth stub
  (`AUTH_MODE`, `/auth/dev/login`, `X-Dev-*` fabricated headers) removed — GCKey is the only
  auth mode, session cookie stays as the browser↔portal credential; (C) the investigator
  dashboard read APIs (`/portal/investigator/*`, SSE, disposition, `outcome.feedback`) were
  KEPT — they satisfy Req 22 + tasks 27.x. design.md gained a "Claimant Portal Backend (BFF)"
  section documenting the full role (auth gateway + read APIs), reconciling the design diagram
  (submission path only) with the tasks. Tests reworked off dev-auth onto the GCKey callback
  path (faked token exchange), asserting a real Bearer is forwarded. Portal unit tests: 49 pass.
- **claim-intake dev-auth removal fallout (NEW, out of scope this session)**: 5 PRE-EXISTING
  unit failures — `test_auth.py::TestClaimIntakeAppWiring::test_post_claims_as_claimant_returns_202`
  and the 4 `test_claim_kafka_rollback.py::TestTwoPhaseCommit` cases — fail identically on
  baseline (verified via `git stash`), independent of the portal work. Likely the tests still
  expect intake's removed dev-auth/trusted-header path. Needs its own grounded fix (rework the
  intake tests onto GCKey Bearer, same pattern as portal-backend). NOT addressed here.
- **claim-intake**: NOW multipart (design line 145) + postal_code optional (Req 1.2) — CORRECT,
  keep. GCKey Bearer is the only auth (dev-auth accretion removed). 202 unit tests pass.
- **Internal auth in tests**: engines + scorer require GCKey Bearer (Req 19.7). Integration
  tests need a seam: a test JWKS/keypair the shared validator trusts, or a documented test
  override. RESOLVED (Option A) — test JWKS + signed service JWT; no production change.
  See "Test-auth seam" below.

## Test-auth seam (Option A — resolved)
Internal endpoints (engines + Ensemble Scorer) gate on
`require_role(Role.INVESTIGATOR)` → `shared.auth.validate_gckey_token`, which fetches a
JWKS and verifies an RS256 GCKey Bearer. The engines/scorer set no
`GCKEY_AUDIENCE`/`GCKEY_CLIENT_ID`/`GCKEY_ISSUER`, so aud/iss are not enforced; a trusted
token needs only a valid RS256 signature, matching `kid`, live `exp`, and an INVESTIGATOR
role claim.

Chosen seam: a TEST JWKS + a signed service JWT (no production code change, faithful to
Req 19.7 — the real `shared.auth` path runs).
- `tests/integration/service_auth.py` — `TestServiceAuth`: ephemeral RSA keypair, JWKS
  builder, `mint_token(roles=[...], ttl_seconds=..., audience=?, issuer=?, extra_claims=?)`.
  Also `installed_service_auth(...)` (file:// install) and `write_jwks(path)`.
- `tests/integration/conftest.py` — session fixture `itest_service_auth`: serves the JWKS
  over REAL local HTTP (`127.0.0.1:<ephemeral>/.well-known/jwks.json`), sets
  `GCKEY_JWKS_URL` + clears the shared JWKS cache, and exposes `auth.auth_server_url` so an
  out-of-process/container service can be pointed at the same JWKS. Mint the orchestrator's
  `ORCHESTRATOR_SERVICE_TOKEN` from `auth.mint_token()`.
- `tests/integration/test_service_auth_seam.py` — proves it: 200 valid INVESTIGATOR,
  401 missing, 403 wrong role, 401 expired, 401 untrusted key. (Runs in-process, no daemon.)

Usage in integration tests: request the `itest_service_auth` fixture, set the engine (and
scorer, when in scope) env `GCKEY_JWKS_URL=auth.auth_server_url`, and set the orchestrator's
`ORCHESTRATOR_SERVICE_TOKEN=auth.mint_token(roles=["INVESTIGATOR"])`. Loopback services are
run via `tests/integration/loopback_service.py` (`LoopbackService` — uvicorn on a real
127.0.0.1 ephemeral port).

## Slice plan (verified vertical slices)
- **Slice 1 (in progress): thin end-to-end thread, no heavy ML.**
  Intake (multipart+GCKey) → claim.received (Kafka) → ROE Adapter → roe.received →
  Orchestrator pairs + dispatches over HTTP → engines → Ensemble Scorer → claim.scored.
  - DONE: orchestrator wiring + auth headers.
  - DONE: (a) test-auth seam (Option A) — see "Test-auth seam" below.
  - DONE: (b) ONE integration test over REAL transports (testcontainers Kafka + real HTTP
    + real GCKey Bearer/JWKS). `tests/integration/test_slice1_end_to_end.py`.
    **It asserts the HONEST current behaviour, not a forced happy path.** Only the
    Repetitive Pattern Engine exists as a service today, so the requirement-faithful
    outcome is the <3-engine degradation: 1 engine completes < MIN_ENGINES_REQUIRED (3) →
    **INDETERMINATE**, payment halted, scorer NOT called (proven via a scorer spy asserting
    0 calls), `claim.scored` published with `risk_decision=INDETERMINATE`, no
    `claim.escalated` (Req 2.1/2.3/2.5). The five unimplemented engines are pointed at a
    CLOSED loopback port so their calls fail (PARTIAL_RESULT) exactly as a down engine would.
  - **REMOVED anti-pattern**: an earlier draft pointed all six engine URLs at the one engine
    to force ≥3 completions and a determinate score. That violated Req 2.3/2.5 and the
    regeneration anti-fragility rule (do not reshape wiring to force green). Do not reintroduce.
  - **Evolution**: as engines are added (Slice 3+), this test graduates to assert the
    determinate scored path (AUTO_APPROVE / FLAG_FOR_REVIEW / INVESTIGATE) once ≥3 engines
    genuinely complete. The determinate scorer/publish path is already unit-covered in
    `tests/integration/test_orchestrator_flow.py` (in-process fakes).

### How to run the container-backed integration tests (macOS + Podman)
- Venv: `.venv-itest` = `.venv-core` subset + `testcontainers[kafka]==4.9.0` (gitignored).
  Rebuild: `python -m venv .venv-itest && .venv-itest/bin/pip install -r
  scripts/dependencies/requirements-core.txt -r scripts/dependencies/requirements-core-test.txt
  "testcontainers[kafka]==4.9.0"`.
- Daemon: `podman machine start` (a Docker-compatible daemon must be reachable).
- Env: `scripts/itest-env.sh` resolves the live Podman socket and exports `DOCKER_HOST`,
  `TESTCONTAINERS_RYUK_DISABLED=true`, `TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE`. Ryuk is
  disabled because it is unreliable under Podman; testcontainers' own context managers clean up.
- Run: `scripts/itest-env.sh .venv-itest/bin/python -m pytest tests/integration -q`
  (last verified: **26 passed** with the runtime up; the suite skips cleanly without a daemon).
- Server-guard: the agent cannot launch the daemon/servers; `podman machine start` and any
  live service bring-up must be run in a human terminal. testcontainers containers are
  started/stopped WITHIN the pytest process, so the agent can drive the tests once the
  daemon is up.
- **Slice 2**: Investigator Dashboard (React, demo look-and-feel) + portal backend
  (GCKey exchange + Bearer-forward) reading claim.scored / risk_assessments.
  - DONE (prereq): portal-backend reworked to the design role — GCKey-only auth +
    Bearer-forward, dashboard read APIs retained (6f65603). See deviations above.
  - REMAINING: the React investigator dashboard (demo look-and-feel) against the
    corrected backend: flagged queue (SSE), claim detail + SHAP chart (a11y, EN/FR),
    disposition form → outcome.feedback (Req 22.1–22.6, tasks 27.x); then an
    integration test over real transports.
- **Slice 3+**: add the other 5 engines one at a time (each with an integration test);
  then the 8 adapters (mock-data fixtures locally); then LLM + graph services.
- **Heavy-ML last**: Document Forgery (LayoutLM), Fraud Ring (GNN), LLM (Llama), Ensemble
  real models (XGBoost/IsolationForest) — need full stack / containers, not .venv-core.
- **Final**: CP37-style k8s deploy + load (p99 ≤ 2000 ms) as the TRUE integration checkpoint.

## Process rules (to prevent re-fragility)
- No seam is "done" until an integration test exercises it over REAL transports.
- Do changes in small verified steps; do NOT delegate large multi-service tasks to a
  subagent (the runner timed out repeatedly this session and left partial state).
- Keep the archive branch as the recovery point; commit each verified increment.

## Environment notes
- `.venv-core` = Python 3.11 core-subset venv (no heavy ML). See
  `scripts/dependencies/README.md`. Heavy-ML slices need the full requirements.lock env or containers.
- Tool constraint this session: the agent could not launch servers (server-guard); live
  bring-up steps must be run in a human terminal.
