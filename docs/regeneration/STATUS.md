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

## Key finding (scope correction)
The prior "systemic integration failure" was over-stated. The real pipeline-core defect
was localized: ONE request-body shape + TWO missing auth headers. The correlator, dispatch,
scoring, engine algorithms, intake, and validation were sound. The design is coherent; the
damage was in a thin wiring layer + duplicated/retrofitted services.

## Genuine deviations still to resolve (from design)
- **Claimant Portal backend**: design shows "React + FastAPI" portal doing GCKey code
  exchange + Bearer-forward. Current `services/portal-backend` is a session retrofit carrying
  dev-auth/session-cookie complexity; the dev-auth stub was already removed from claim-intake.
  Portal-backend to be reworked to the strict design role (GCKey exchange + session cookie +
  Bearer-forward) OR regenerated in the UI slice.
- **claim-intake**: NOW multipart (design line 145) + postal_code optional (Req 1.2) — CORRECT,
  keep. GCKey Bearer is the only auth (dev-auth accretion removed). 202 unit tests pass.
- **Internal auth in tests**: engines + scorer require GCKey Bearer (Req 19.7). Integration
  tests need a seam: a test JWKS/keypair the shared validator trusts, or a documented test
  override. DECISION PENDING — required before the Slice 1 integration test.

## Slice plan (verified vertical slices)
- **Slice 1 (in progress): thin end-to-end thread, no heavy ML.**
  Intake (multipart+GCKey) → claim.received (Kafka) → ROE Adapter → roe.received →
  Orchestrator pairs + dispatches over HTTP → Repetitive Pattern Engine → Ensemble Scorer
  (heuristic, no torch) → claim.scored. 
  - DONE: orchestrator wiring + auth headers.
  - REMAINING: (a) decide test-auth seam; (b) write ONE integration test (testcontainers
    Kafka + real HTTP) asserting paired events → claim.scored with a Risk_Decision.
- **Slice 2**: Investigator Dashboard (React, demo look-and-feel) + portal backend
  (GCKey exchange + Bearer-forward) reading claim.scored / risk_assessments.
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
