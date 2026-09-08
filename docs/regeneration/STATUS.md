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
- **claim-intake unit test rot**: RESOLVED (d88791b). The 5 failures were NOT an auth-removal
  problem — the tests POSTed `json=` to `/api/v1/claims`, which accepts multipart/form-data
  (design line 145; Req 1.2), so FastAPI returned 422. Intake has no dev-auth code; it uses
  `require_role(Role.CLAIMANT)` (GCKey Bearer/JWKS) only. Fixed the tests to post multipart
  form data (`data=`) and corrected a stale `AUTH_MODE=dev` docstring line in intake. Full unit
  suite now 1121 passed, 22 skipped, 0 failed.
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
- **Slice 2**: Investigator Dashboard (React) + portal backend reading
  risk_assessments. DONE.
  - portal-backend reworked to the design role — GCKey-only auth + Bearer-forward,
    dashboard read APIs retained (6f65603).
  - React investigator dashboard SPA (`services/claimant-portal`) was REVIEWED against
    Req 22 (not just trusting its 131 green tests): 22.1–22.5 genuinely met by the
    implementation (flagged queue fetch+filter+sort+SSE merge; detail with severity
    badges + SHAP recharts chart + bilingual explanation via selectBilingual; SHAP
    chart role=img + per-bar sr-only labels + hidden data table + EN/FR i18next).
  - The review surfaced TWO real BACKEND gaps that green SPA unit tests could not catch,
    both fixed (145133b):
    1. **Req 22.6 access control** — the detail endpoint admitted REVIEWER (masked 200)
       instead of denying; tightened to `require_roles(INVESTIGATOR, ADMINISTRATOR)`.
    2. **Req 22.3 disposition persistence** — `record_disposition` targeted
       nonexistent `risk_assessments.disposition/case_notes` columns (silent no-op)
       while migration 004's `claim_dispositions` table was unused. Repointed to
       INSERT into `claim_dispositions` (investigator_id = GCKey subject), detail reads
       the current disposition via LEFT JOIN LATERAL; the write now raises on real DB
       errors. Threaded the GCKey `sub` through Principal.subject + the session.
  - Integration test over REAL transports (`tests/integration/test_slice2_dashboard_backend.py`):
    testcontainers Postgres seeded via migrations 001–004 + real Kafka, portal-backend on
    real loopback HTTP. Proves 22.1 (sorted flagged-only queue), 22.6 (REVIEWER/CLAIMANT
    detail → 403; REVIEWER queue masked), 22.2 (detail w/ real indicators + SHAP),
    22.3 (disposition persisted, detail reflects it), 22.4 (real outcome.feedback Kafka
    event for CONFIRMED_FRAUD; none for NEEDS_FURTHER_REVIEW), 22.1 SSE frame.
  - Verified: portal unit 50 pass; full unit 1122 pass/22 skip; full integration 27 pass.
  - NOTE: the SPA's own frontend integration against a live backend (browser E2E) and
    task 30's podman-compose full claimant→investigator flow remain as a later checkpoint.
- **Slice 3+**: add the other 5 engines one at a time (each with an integration test);
  then the 8 adapters (mock-data fixtures locally); then LLM + graph services.
- **Heavy-ML last**: Document Forgery (LayoutLM), Fraud Ring (GNN), LLM (Llama), Ensemble
  real models (XGBoost/IsolationForest) — need full stack / containers, not .venv-core.
- **Final**: CP37-style k8s deploy + load (p99 ≤ 2000 ms) as the TRUE integration checkpoint.

## Task 30 — local full-stack compose (Option 1: the verified slice)
`podman-compose.yml` now composes the SLICE built + verified so far (not all 18 services):
postgres (+ migrations 001–004 applied on init) + kafka/redis/otel, and the app services
claim-intake, roe-adapter, pipeline-orchestrator, repetitive-pattern-engine, ensemble-scorer,
portal-backend, claimant-portal — behind a Caddy single-origin proxy, with a local-dev JWKS +
signed INVESTIGATOR service token for internal GCKey Bearer validation (Req 19.7 faithful; no
bypass). `podman-compose config` lints clean; the engine + portal-backend images were verified
to BUILD and BOOT.

**Honest behaviour (hold until Slice 3):** only the Repetitive Pattern engine is composed, so a
submitted claim correctly reaches the <3-engine **INDETERMINATE** decision (Req 2.5) — a claim
flows intake → orchestrator → INDETERMINATE `claim.scored` → the investigator queue. The
determinate scored flow (AUTO_APPROVE/FLAG/INVESTIGATE) is NOT yet walkable and that claim is
deferred until Slice 3 adds ≥3 engines. The GCKey login flow is also not walkable locally (no
IdP); the dashboard queue/detail/disposition read flow IS.

**Bring-up recipe (run in a human terminal — the agent is server-guarded):**
1. `podman machine start` (ensure a reachable daemon).
2. `python scripts/dev/gen-service-token.py` (writes gitignored `.env.dev-token` + `scripts/dev/jwks/`).
3. `podman-compose up --build` (first build is slow — services install the full pyproject incl. heavy ML).
4. Browse `http://localhost:8080` (Caddy single origin → SPA + portal-backend).

**Chain of pre-existing gaps found + fixed while doing task 30** (all "marked-done but never run"):
- Orchestrator SERVICE entrypoint dispatched via a logging-only placeholder → wired the real
  EngineDispatcher→ScoringClient→ResultPublisher chain (1270041).
- `podman-compose.yml` had ZERO app services (infra only) → composed the verified slice (d048065).
- All 18 service Containerfiles were unbuildable/non-running on the ubi9/python-311 base: build
  RUN steps don't activate the `/opt/app-root` venv, so poetry installed where the runtime python
  couldn't import. Fix: `ENV VIRTUAL_ENV=/opt/app-root` + runtime `COPY /opt/app-root` + drop the
  bogus `pip install -e ./shared`; NO `USER 0` (d048065). Added the missing portal-backend Containerfile.
- `pyproject.toml` was missing 6 runtime deps the code imports at load (python-jose, pydantic-settings,
  python-multipart, structlog, python-json-logger, numpy) — versions pinned to RESOLVE against the
  full graph incl. langchain-community (pydantic-settings 2.7.0, numpy 1.26.4) (d048065).

**Deferred/known (not blocking task 30):** `requirements.lock` (tracked) has drifted from the
poetry `pyproject.toml` the images build from — reconcile later. Services install the FULL pyproject
(incl. torch/transformers/llama-cpp) even when they don't need ML — no per-service dependency groups,
so images are large; a future cleanup could add poetry extras/groups per service.

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
