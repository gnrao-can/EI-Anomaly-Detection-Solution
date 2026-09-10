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
6. (Slice 2 — 6f65603, d88791b, 145133b) Portal-backend GCKey Bearer-forward + dev-auth
   removal + 2 Req-22 backend fixes + dashboard integration test. See "Slice 2" below.
7. (Slice 3 — 3f28cf2, 44ab31c, 03b87cb) Orchestrator↔engine data-flow fix; graduated
   determinate (degrading → AUTO_APPROVE) integration test; history + regional engines
   composed. See "Slice 3" below.
8. (Slice 3 — 5eab64d, e58764f) Engine→adapter `SERVICE_TOKEN` auth fix (Req 19.7) + the
   3 mock-data adapters composed; with-adapters determinate (→ FLAG/INVESTIGATE) integration
   test over real transports. See "Slice 3" below.
9. (Slice 3+ — e1f0153, b2c3209, cf605c5, 1ee097e) **Geographic Anomaly added as engine #4**
   with its GeoNames + MVR + CBSA adapters. See "Slice 3+ — Geographic" below.
10. (Slice 3+ — db95b7f, ef32bff, b12337d, f81fe37) **Fraud Ring added as engine #5** over the
    REAL graph relationship seam (Neo4j + Graph Service + Business Registry adapter). See
    "Slice 3+ — Fraud Ring" below.

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
  - **Evolution (DONE in Slice 3)**: now that engines 2 & 3 exist, this file carries
    THREE integration tests over real transports — the original 1-engine INDETERMINATE,
    plus two ≥3-engine determinate tests (degrading → AUTO_APPROVE, and with-adapters →
    FLAG/INVESTIGATE). See "Slice 3" below. The determinate scorer/publish path is also
    unit-covered in `tests/integration/test_orchestrator_flow.py` (in-process fakes).

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
  (last verified: **31 passed** with the runtime up — incl. the Slice 3+ Geographic 4-engine
  test and the Fraud Ring 5-engine test over REAL Neo4j; the suite skips cleanly without a
  daemon).
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
  - NOTE: the SPA's own frontend integration against a live backend (browser E2E) remains a
    later checkpoint. Task 30's determinate claimant→investigator flow is now demonstrable
    (Slice 3 composed ≥3 engines + the mock-data adapters); a live browser walkthrough still
    needs a human terminal (the agent is server-guarded) since the GCKey login has no local IdP.
- **Slice 3 (in progress): cross the ≥3-engine threshold → DETERMINATE decision.**
  Added **History Inconsistency** (8010) + **Regional Analysis** (8020) as engines 2 & 3
  (lightest, standalone-complete engines — no Neo4j/LLM/GPU), so ≥3 engines complete and
  the pipeline runs the Ensemble Scorer instead of degrading to INDETERMINATE (Req 2.2/2.5).
  - **Data-flow fix (3f28cf2)**: `claim.received` is enriched (insurable hours/earnings,
    separation/claim dates, economic_region_code, noc_code) and the orchestrator's
    `_build_engine_request` forwards the superset + derives `tax_year` (year of separation),
    `reference_month` (month before claim start), `insurable_hourly_earnings` (earnings/hours),
    and sources `roe_earnings` from the `roe.received` payload. Without this the two engines
    422'd; now they get exactly the fields Req 6.x / 8.x need.
  - **Determinate path proven over real transports — TWO graduated integration tests**
    (`tests/integration/test_slice1_end_to_end.py`):
    1. **Degrading (44ab31c)** — 3 engines, NO adapters. All engines complete but degrade
       (UNRESOLVED/DATA_UNAVAILABLE), so the heuristic scorer yields a low score →
       **AUTO_APPROVE**. Proves the ≥3 → scorer → publish path honestly (no faked completion).
    2. **With-adapters (e58764f)** — the 3 engines PLUS the CRA, Stats Canada, and Job Bank
       mock-data adapters, all on real loopback HTTP + real Kafka + real GCKey JWKS auth.
       With-signals inputs (region 59 / NOC 63201 / 2024-05 / \$15.00·h⁻¹) hit the Job Bank
       fixture (245 postings > 200 → HIGH Req 8.4; \$24.10 median wage > 15×1.15 → HIGH Req 8.6).
       Two real HIGH Indicators drive Risk_Score ≥ 30 → determinate **FLAG_FOR_REVIEW /
       INVESTIGATE** (never AUTO_APPROVE, never INDETERMINATE). The test also asserts the
       adapter serves the fixture WITH the INVESTIGATOR Bearer and 401s WITHOUT it (Req 19.7),
       and that ≥2 HIGH regional Indicators actually reached the scorer.
  - **Real gap found + fixed (5eab64d): engine→adapter authentication.** The two engines called
    the auth-gated adapters (`require_role(INVESTIGATOR)`, Req 19.7) with NO `Authorization`
    header, so every adapter call 401'd → the engines silently degraded to
    UNRESOLVED/DATA_UNAVAILABLE. Fixed by mirroring the orchestrator's `ORCHESTRATOR_SERVICE_TOKEN`
    pattern: both engines gained a `SERVICE_TOKEN` setting; their adapter clients attach
    `Authorization: Bearer <token>` when it is set (empty in unit tests → no header, so all 109
    engine unit tests are unaffected). `scripts/dev/gen-service-token.py` now also writes
    `SERVICE_TOKEN` (the same INVESTIGATOR JWT) to `.env.dev-token`.
  - **Compose (03b87cb, 5eab64d)**: history (8010) + regional (8020) engines and the CRA (8011),
    Stats Canada (8017), Job Bank (8016) adapters are all composed. New Containerfiles for
    stats-canada + job-bank mirror the cra-adapter pattern (`ENV VIRTUAL_ENV=/opt/app-root`,
    non-root, no `USER 0`). mock-data is **bind-mounted read-only** in the dev compose rather
    than baked into the images (`.containerignore` intentionally excludes `mock-data/` from
    production images — prod adapters call the live APIs). Verified: both new adapter images
    build + boot; `/healthz` reports mock mode; data endpoints 401 unauthenticated;
    `podman-compose config` lints clean.
- **Slice 3+ — Geographic (DONE)**: **Geographic Anomaly is now engine #4** (8022), composed
  with its three adapters — GeoNames (8110), MVR (8015), CBSA (8013). Four engines now reach
  a DETERMINATE decision (Req 2.2). The engine is spec-faithful and standalone-complete
  (Req 4.1–4.10); the work here was the wiring layer + two data-flow gaps, mirroring the
  engines 2 & 3 pattern. Details:
  - **Engine→adapter auth (e1f0153, gap #1 — same class as 5eab64d).** The engine's GeoNames/
    MVR/CBSA clients sent NO `Authorization` header, but all three adapters are auth-gated
    (`require_role(INVESTIGATOR)`, Req 19.7) → every call would 401 and the engine would
    silently degrade (RESOLUTION_FAILED / UNKNOWN / no absence). Added a `service_token`
    setting + a `bearer_token` param on each client (attaches `Authorization: Bearer` when
    set; empty in unit tests → no header, so the engine unit tests are unaffected). Wired from
    `settings.service_token` in the lifespan. Client-level auth tests (httpx MockTransport)
    assert the header is present with a token and absent without one.
  - **Orchestrator data-flow (b2c3209, gap #2 — geographic analog of 3f28cf2).** The engine's
    `AnalyzeRequest` REQUIRES `residential_postal_code` + `employer_work_location` (a 422
    otherwise); both are already on `claim.received` (`shared.models.Claim`) but the
    orchestrator's `_build_engine_request` pass-through list omitted them. Added both.
  - **Province derivation (b2c3209, Option A — decided with the owner, Req 15.1).**
    `claim.received` carries only the residential FSA (Protected B minimisation), NOT a
    province, but the engine keys its MVR (Req 4.3) and CBSA (Req 4.9) lookups on province.
    New `services/geographic-anomaly-engine/province.py` derives the province from the FSA's
    first letter (standard Canada Post convention; `X`→`NT`) when no explicit
    `residential_province` is supplied — the engine owns the address→province step (Req 15.1),
    so NO new event field / schema migration. Unmappable FSA → `""` (adapters treat as
    unknown; no province fabricated). Unit-tested exhaustively (all 18 first-letter mappings,
    unused letters D/F/I/O/Q/U/W/Z, case/whitespace, explicit-wins) + an endpoint test proving
    the derived province reaches the MVR lookup and drives the HIGH Indicator.
  - **/healthz 503-when-GeoNames-None concern (RESOLVED by grounding — no code change).** The
    earlier worry was a false alarm: the lifespan builds the GeoNames client unconditionally
    (only `None` if `httpx` import fails, which cannot happen in a container), so `/healthz`
    returns 200 in compose — identical to the History engine's pattern. And even if GeoNames
    resolution genuinely fails at request time, the engine returns HTTP 200 with a MEDIUM
    `RESOLUTION_FAILED` Indicator (Req 4.2), which counts as a COMPLETED engine for the ≥3
    minimum. So no special gating handling is needed; `/healthz` is left as-is (matches
    History, not the Regional engine's unconditional-200 style).
  - **Compose (cf605c5)**: engine #4 + the three adapters, mirroring the SERVICE_TOKEN/
    env_file/`GCKEY_JWKS_URL` pattern; mock-data bind-mounted read-only (`.containerignore`
    excludes `mock-data/` from prod images). Two PRE-EXISTING geonames-adapter bugs found +
    fixed while verifying the build: (1) its Containerfile `COPY mock-data/geonames/` conflicted
    with `.containerignore` → build failed (removed the COPY; the dump is bind-mounted in dev
    like the other adapters); (2) `config.py` `_REPO_ROOT = Path(__file__).parents[2]` crashed
    with `IndexError` in the flattened `/app` image (guarded with a CWD-relative fallback,
    overridden by `GEONAMES_LOCAL_DUMP_PATH` in dev). Verified LIVE over Podman: all four
    images build + boot; `/healthz` 200; each adapter 401s unauthenticated and 200s with the
    INVESTIGATOR Bearer; `POST /engine/analyze` for A1C (St. John's, NL) → M5H (Toronto),
    2108.6856 km, drove the FSA-derived province NL through the MVR adapter to a real HIGH
    no-vehicle Indicator (Req 4.3). `podman-compose config` lints clean.
  - **Integration test over real transports (1ee097e)** — `test_four_engines_with_geographic_
    adapters_yield_determinate_over_real_transports` in `tests/integration/test_slice1_end_to_
    end.py`: the geographic engine + GeoNames/MVR/CBSA adapters (real loopback HTTP + real
    Kafka + real GCKey JWKS) alongside the other three engines. Asserts all four complete
    (≥3 → determinate, not INDETERMINATE); the real scorer runs (no fallback); a HIGH
    `GEOGRAPHIC_ANOMALY` `transport_implausibility_vehicle` Indicator with `province=NL` reached
    the dispatch/scorer (proves the engine→GeoNames→MVR chain, not a degrade); the MVR adapter
    401s WITHOUT the Bearer and serves the NL=NONE fixture WITH it (Req 19.7); the decision is
    determinate and out of AUTO_APPROVE (a single HIGH → score ≥ 30, Req 9.2). Verified LIVE:
    full integration suite **30 passed** (was 26 pre-Slice-3).
  - **GROUNDING NOTE — transit (Req 4.4) + economic (Req 4.5) signals are currently UNFED.**
    The GeoNames Adapter (Req 14) implements only `resolve` + `compute-distance`; there is NO
    transit-route or fare endpoint, and `claim.received` carries no `transit_route_exists` /
    `transport_mode_costs` / `weekly_benefit_rate` fields. The engine ACCEPTS those inputs and
    handles them correctly (e.g. it emits the Req 4.6 MEDIUM "transport cost data unavailable"
    when no fare data is present — observed in the live run), but nothing in the current stack
    SOURCES them. This is a real pre-existing implementation-vs-design gap, deliberately NOT
    papered over: the reachable real HIGH signal today is Req 4.3 (no vehicle > 100 km) via MVR.
    A future step should add a fare/transit data source (or event fields) to feed 4.4/4.5.
  - **Harness note**: the engine's `analyze()` does NOT rebuild adapter clients when
    `app.state.{geonames,mvr,cbsa}` is `None` (unlike History/Regional's `_get_client`); the
    lifespan builds them only when the attribute is ABSENT. The integration helper therefore
    `delattr`s the app.state clients (not sets them to `None`) so the lifespan rebuilds them
    against the test URLs + SERVICE_TOKEN. (Left as a note; not a production defect — the
    lifespan path is correct for a normally-started service.)
- **Slice 3+ — Fraud Ring (DONE)**: **Fraud Ring is now engine #5** (8023), composed with the
  Business Registry adapter (8012) and the Graph Service (8006) backed by **REAL Neo4j** — the
  graph relationship is the value proposition, so it is validated against a real graph DB
  (Req 5.1/5.5/5.10), not the in-memory fake. Five engines now reach a DETERMINATE decision.
  The engine + graph-service are spec-faithful and need NO torch/GNN and NO GraphSAGE weights:
  the heavy ML lives only in graph-service's `community.py`, fully guarded, with Louvain
  (networkx) + a deterministic hash-embedding fallback as the authoritative partitioner.
  Details:
  - **Engine→dependency auth (db95b7f, gap #1 — same class as 5eab64d).** The engine's Business
    Registry + Graph Service clients (and `HttpCohortSource`) sent no `Authorization` header,
    but both dependencies are auth-gated (`require_role(INVESTIGATOR)`, Req 19.7) → every call
    would 401 and the engine would degrade (shell-employer skipped, no graph cohort). Added a
    `service_token` setting + a `bearer_token` param on each client (attaches
    `Authorization: Bearer` when set; empty in unit tests → no header). Client-level auth tests
    assert the header is present with a token and absent without one.
  - **Graph-sourced cohort (ef32bff, gap #2 — the value-proposition fix, Req 5.1).** The
    employer's 24-month cohort lives in the graph (Neo4j), NOT on a single `claim.received`
    event. Previously the engine drove coordinated-layoff (5.4) + community cluster (5.5) from a
    PAYLOAD cohort and used community-detect only for a `num_communities` count — the graph was
    not the source of the relationship evidence. Now:
      * Graph Service gained `GraphRepository.fetch_employer_cohort(bns)` +
        `FETCH_EMPLOYER_COHORT_CYPHER` (returns each cohort member WITH its edge/node attributes:
        `sin_hash, postal_fsa, surname_hash, insurable_hours, separation_date, roe_submission_ts,
        claim_id`) and a `GET /graph/cohort/{employer_bn}` endpoint (auth-gated). The
        `FakeAsyncDriver` mirrors it; the same Cypher runs against real Neo4j.
      * The engine's `graph_client.fetch_cohort(bn)` reads that cohort, and `analyze()` builds
        BOTH the coordinated-layoff `CohortMember` list (5.4, from edge
        `separation_date + roe_submission_ts`) and the community `CohortClaimant` list
        (5.5/5.6, from `postal_fsa/surname_hash/insurable_hours`) from it. `community-detect` now
        uses `employer_bns` mode (server reads the edges from Neo4j) when graph-sourced. The
        payload cohort/graph_cohort remains the fallback when the graph is UNAVAILABLE (Req 5.9
        degrade-gracefully). The response reports `graph_cohort_source`.
  - **Compose (b12337d)**: engine #5 + `business-registry-adapter` (APP_ENV=development → federal
    + 13 provincial fixtures, mock-data bind-mounted read-only) + `graph-service`
    (`NEO4J_TEST_MODE=false` + `NEO4J_URI=bolt://neo4j:7687` → the LIVE Neo4j already in the infra
    block). Orchestrator gained `ORCHESTRATOR_ENGINE_FRAUD_RING_URL`.
    - **Dev-mode client gap fixed**: the engine lifespan built its dependency clients only when
      NOT is_development, leaving them `None` in the dev compose. Now builds them whenever httpx
      is available (the engine has no synthetic mode — it always calls the real dependencies).
      Unit tests are unaffected: they use `TestClient(app)` WITHOUT its context manager, so the
      lifespan never runs and test-injected fakes on `app.state` are preserved.
    - **Two port mismatches fixed**: engine `graph_service_url` default `:8009` → `:8006` (matches
      the graph-service Containerfile); dispatch `FRAUD_RING` default `:8000` → `:8023`.
    - Verified LIVE over real Neo4j: 3 images build + boot; `/healthz` 200 for all (graph-service
      200 proves live Neo4j connectivity); 401 unauthenticated / 200 with the INVESTIGATOR Bearer
      on graph + registry. Seeded a 3-claimant ring via `/graph/construct`, read it back via
      `/graph/cohort/{bn}`, and `/engine/analyze` (`graph_cohort_source=true`) produced HIGH
      shell_employer_no_record (5.3) + CRITICAL coordinated_layoff (5.4) + CRITICAL
      community_shared_attribute K1A (5.5) + HIGH cohort_hours_band (5.6), num_communities=1
      (5.10). `podman-compose config` lints clean.
  - **Integration test over real transports (f81fe37)** —
    `test_five_engines_with_fraud_ring_graph_seam_over_real_neo4j` in
    `tests/integration/test_slice1_end_to_end.py`: the Fraud Ring engine wired to a **REAL Neo4j**
    (testcontainers `Neo4jContainer` 5.26.0-community) behind the Graph Service, plus the Business
    Registry adapter, over real Kafka + loopback HTTP + real GCKey JWKS auth. Seeds a 3-claimant
    ring into Neo4j via `/graph/construct`, runs the 5-engine pipeline; the engine reads the
    cohort BACK from the graph and produces CRITICAL community_shared_attribute (5.5, FSA K1A) +
    CRITICAL coordinated_layoff (5.4) + HIGH shell_employer_no_record (5.3), with the Louvain
    partition covering all 3 ring claimants (5.10). The determinate decision is NON-AUTO_APPROVE.
    Asserts the graph cohort + registry 401 unauthenticated / 200 with the Bearer (Req 19.7).
    Verified LIVE: stable across 3 consecutive runs (~33 s each); full integration suite
    **31 passed** (was 30).
  - **GROUNDING NOTE — cohort source.** Req 5.1 assigns the 24-month bipartite graph to the
    Graph Service ("retrieve or construct... for the employer's BN"). The `/graph/community-detect`
    `employer_bns` mode + the new `/graph/cohort` endpoint read that cohort from Neo4j, so the
    engine sources the relationship evidence from the accumulated graph, NOT from a single claim
    event — nothing is invented. Community detection uses Louvain (deterministic); GraphSAGE/torch
    remain optional and guarded (a future step could add trained weights, but Louvain is
    authoritative per the design).
- **Slice 3+ remaining**: the last heavy-ML engine — document-forgery (LayoutLM), with an
  integration test; then LLM services.
- **Heavy-ML last**: Document Forgery (LayoutLM), Fraud Ring (GNN), LLM (Llama), Ensemble
  real models (XGBoost/IsolationForest) — need full stack / containers, not .venv-core.
- **Final**: CP37-style k8s deploy + load (p99 ≤ 2000 ms) as the TRUE integration checkpoint.

## Task 30 — local full-stack compose (Option 1: the verified slice)
`podman-compose.yml` composes the SLICE built + verified so far (not all 18 services):
postgres (+ migrations 001–004 applied on init) + kafka/redis/otel, and the app services
claim-intake, roe-adapter, pipeline-orchestrator, **five engines** (repetitive-pattern,
history-inconsistency, regional-analysis, geographic-anomaly, fraud-ring), **seven adapters**
(cra, stats-canada, job-bank, geonames, mvr, cbsa, business-registry), the **Graph Service
backed by REAL Neo4j** (the Fraud Ring graph relationship seam, Req 5.1/5.5/5.10),
ensemble-scorer, portal-backend, claimant-portal — behind a Caddy single-origin proxy, with a
local-dev JWKS + signed INVESTIGATOR service token for internal GCKey Bearer validation
(Req 19.7 faithful; no bypass). `podman-compose config` lints clean; the engine, adapter, and
portal-backend images were verified to BUILD and BOOT.

**Honest behaviour (updated for Slice 3 — now VERIFIED LIVE over compose):** THREE engines are
composed, so a submitted claim crosses the ≥3-engine threshold and reaches a **DETERMINATE**
decision (Req 2.2/2.5). The full flow was exercised end-to-end on the running stack:
intake `POST /api/v1/claims` (202) → `claim.received` (real Kafka) → ROE `POST /roe/rest` (202) →
`roe.received` → orchestrator **pairs claim+ROE by claim_id** (Redis) → dispatches to engines →
**ensemble scorer** → `claim.scored` published → **persisted to Postgres `risk_assessments`**.
Observed real decisions: a paired claim scored **`FLAG_FOR_REVIEW` (risk_score 44)** (verified row
in `risk_assessments`), and a run where <3 engines completed correctly yielded **INDETERMINATE**
(payment halted). Both determinate paths are ALSO covered by integration tests over real transports
(see "Slice 3").

**Investigator-action half closed too (Req 22.1/22.3/22.4/22.6 — verified live):** with a dev
session cookie (`scripts/dev/mint-session-cookie.py`), `GET /portal/investigator/claims` returns the
`FLAG_FOR_REVIEW` claim with server-side PII masking (SIN `***-**-426`); `PUT /api/v1/claims/{id}/disposition`
records a disposition to `claim_dispositions` attributed to the GCKey subject (append-only — a
`CONFIRMED_FRAUD` then a `FALSE_POSITIVE` produced two rows, latest = current), and the
CONFIRMED_FRAUD/FALSE_POSITIVE cases published an `outcome.feedback` Kafka event for model
retraining. So the full claimant→pipeline→decision→investigator-review→disposition→feedback loop is
demonstrable end-to-end locally.

**Three real defects were found ONLY by running the live compose walkthrough** (the integration
tests inject events directly / use loopback, so they didn't surface these) — all fixed (29053b7):
1. `roe-adapter` was missing `GCKEY_JWKS_URL` → `POST /roe/rest` returned 401 "Unable to retrieve
   GCKey signing keys" (Req 19.7: the ROE endpoint must validate a GCKey Bearer).
2. `ensemble-scorer` had no `EI_ADS_PG_DSN` → it published `claim.scored` but SILENTLY skipped
   Postgres persistence (no-op path), so `risk_assessments` stayed empty and the investigator queue
   could never populate.
3. Schema mismatch: `ensemble-scorer/repository.py` inserted `claim_id` + `scored_at` into
   `shap_attributions`, but that table (migration 002) has only
   `assessment_id, indicator_id, feature_name, contribution, base_value`. The asyncpg
   `UndefinedColumnError` rolled back the whole persist → scorer 500 → orchestrator fell back to a
   spurious INDETERMINATE. Fixed the INSERT + row tuples to match the schema.

**Still blocked locally — the GCKey browser session.** The portal-backend `/portal/*` and write
`/api/*` endpoints authenticate via a **signed HttpOnly session cookie** minted by the GCKey OAuth
code exchange. There is no local GCKey IdP, so that cookie cannot be obtained through the browser.
The data IS present and correct (verified in Postgres) and the read APIs work given a valid session.
For local dev, `scripts/dev/mint-session-cookie.py` mints a valid signed session cookie (same
HMAC-SHA256 over `SESSION_SECRET` the BFF uses) so the dashboard IS browsable without a GCKey IdP —
see the recipe below. Claim SUBMISSION through the portal still needs a real GCKey access token in
the session (Bearer-forward to intake); submit directly to `claim-intake` with a minted CLAIMANT
Bearer instead (as in the recipe).

**Bring-up recipe (run in a human terminal — the agent is server-guarded). This is the recipe that
ACTUALLY WORKS on macOS + Podman 6 after a long debugging session; deviations reintroduce the
failures noted in "Task 30 — compose gotchas" below:**
1. `export CONTAINERS_MACHINE_PROVIDER=applehv` — use Apple's Virtualization.framework backend, NOT
   the default libkrun/krunkit (which failed to boot: `krunkit exited unexpectedly with exit code 2`).
   Add it to `~/.zshrc` so every shell uses it.
2. `podman machine start podman-machine-default` (150 GiB disk; the default ~80 GiB fills up because
   each Python image installs the full heavy-ML pyproject — see "Deferred/known").
3. `.venv-core/bin/python scripts/dev/gen-service-token.py` — writes gitignored `.env.dev-token`
   (`ORCHESTRATOR_SERVICE_TOKEN` + `SERVICE_TOKEN`, both INVESTIGATOR), the signing key
   `scripts/dev/jwks/signing-key.pem`, and the public JWKS. Run with the venv python (it has
   `python-jose`); a bare `python` gives `ModuleNotFoundError: jose`.
4. Build images SEQUENTIALLY once (parallel `podman-compose --build` OOMs the fresh 8 GiB VM):
   for each service `s`, `podman build -f services/$s/Containerfile -t "ei-anomaly-detection-solution_${s}:latest" .`
   (quote the tag as one token so the shell does not mangle `$s:latest`).
5. `podman-compose --in-pod false up -d` — `--in-pod false` runs standalone containers; the shared
   pod model + `depends_on` (removed from the compose) caused phantom "address already in use"
   port collisions. NEVER set `DOCKER_HOST` — `podman`/`podman-compose` use the DEFAULT connection;
   a `DOCKER_HOST` set to a forwarded socket operates on a different view and your teardowns miss the
   real containers.
6. Browse `http://localhost:8090` (proxy maps host 8090 → container 8080; host 8080 can get stuck
   held by a leftover gvproxy forward). Only `8090` (proxy) and `5432` (postgres) publish to the host.
7. Drive the pipeline (submit from INSIDE the network — internal services publish no host ports):
   mint a CLAIMANT Bearer with `scripts/dev/mint-token.py`, POST the claim to `claim-intake:8001`,
   capture its `claim_id`, then POST a matching ROE (same `claimId`) to `roe-adapter:8002/roe/rest`,
   both via a throwaway `curl` container on network `ei-anomaly-detection-solution_default`. Inspect
   the decision: `podman exec ei-ads-postgres psql -U ei_ads_app -d ei_ads -c "select claim_id,risk_score,risk_decision from risk_assessments order by scored_at desc limit 5;"`
8. Browse the investigator dashboard: `.venv-core/bin/python scripts/dev/mint-session-cookie.py --role INVESTIGATOR`
   prints a `Cookie: ei_ads_session=...` header and a ready `curl` for `/portal/investigator/claims`
   via `http://localhost:8090`. In a browser, set that cookie for `localhost:8090` (devtools →
   Application → Cookies) and load the dashboard.

**If a bring-up fails, RESET cleanly (never `up` on top of partial state; no inline `#` comments in
a pasted zsh block):** `unset DOCKER_HOST; podman stop -t 3 $(podman ps -aq); podman rm -f $(podman ps -aq)`.

### Task 30 — compose gotchas (macOS + Podman 6) — captured so nobody loses an afternoon again
- **VM backend:** default libkrun/krunkit failed to boot (`exit code 2`); switching to `applehv`
  (`CONTAINERS_MACHINE_PROVIDER=applehv`, uses `vfkit` + Virtualization.framework) fixed it.
- **VM disk:** the VM's disk is separate from the Mac's; heavy-ML images fill the default ~80 GiB.
  Use `--disk-size 150` on `machine init`, and `podman image prune -a -f` to reclaim.
- **Parallel builds OOM** the fresh 8 GiB VM → build sequentially (step 4) or `machine set --memory 12288`.
- **Port publishing:** publishing ~18 host ports tripped a Podman rootless/applehv port-allocator
  bug (spurious "address already in use" on FREE ports). Fixed by publishing ONLY proxy(8090)+postgres(5432);
  all other services are internal-only and reached by service name.
- **`depends_on`** → podman-compose hard `--requires` links → out-of-order-removal deadlocks + netns
  coupling. Removed from the compose; services degrade-and-retry instead.
- **Kafka advertised listener** was `localhost:9092` → in-network clients were redirected to their own
  localhost. Set to `kafka:9092`. (Only "worked" before because 9092 was host-published.)
- **`--in-pod false`** — run standalone containers, not the shared pod.
- **NEVER set `DOCKER_HOST`** — use the default connection; a forwarded socket is a different view.
- **`podman-compose down` is unreliable** (tries to remove running containers) — use the stop+rm reset.
- **Stuck gvproxy host-port forward** survives container removal; remap the host port (e.g. 8080→8090)
  or `podman machine stop/start` to clear all forwards.
- **`.venv-core/bin/python`** for the dev scripts (has `python-jose`); bare `python` lacks it.

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
