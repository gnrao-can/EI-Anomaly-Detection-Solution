# EI-ADS — Demo Readiness Checklist

> Prepared for the potential-client demo (federal department leading a similar effort).
> Based on the verified state of the regenerated implementation on `regen/main`.

## 1. Solution at a glance (demo narrative core)

- Real-time AI fraud detection across **Employment Insurance (EI), Old Age Security (OAS), and Canada Pension Plan (CPP)** benefit programs.
- **Six parallel detection engines** → **Ensemble Scorer** (XGBoost + Isolation Forest, SHAP) → **explainable Risk_Score / Risk_Decision** (AUTO_APPROVE / FLAG_FOR_REVIEW / INVESTIGATE / INDETERMINATE).
- End-to-end **≤ 2,000 ms** claim latency budget (p99) at 500 concurrent claims; 2,000 claims/min throughput target.
- Bilingual **English/French** output; Protected B classification; GCKey OAuth 2.0 auth; 7-year immutable audit trail; PII minimisation; WCAG 2.1 AA / bilingual content quality.

## 2. Verification evidence (what has been demonstrated / proven)

| Area | Status |
|---|---|
| All six engines composed & integration-tested over **real transports** (Kafka via testcontainers, real HTTP, real GCKey JWKS auth) | ✅ Verified — 32 integration tests passed (last verified run) |
| Engine → adapter auth (SERVICE_TOKEN, Req 19.7) & orchestrator data-flow fixes | ✅ Fixed & verified |
| Audit-log immutability (RLS + trigger, Req 2.7 / 19.3) | ✅ Fixed & verified |
| LLM Service wired for Req 6.6 / 7.8 checks (deterministic now; real Llama 3.1 model-gated) | ✅ Wired & tested |
| Portal backend / investigator dashboard (Req 22.1–22.6), live SSE feed, fraud-ring graph view | ✅ Fixed & tested |
| Property-based correctness tests P1–P15 (Hypothesis) | ✅ Present |
| **Unit suite** | ⚠️ 1,189 passed / 23 skipped / **14 failed** — all 14 in `tests/unit/test_history_inconsistency_engine_14_2.py`, which passes **30/30 in isolation** → likely cross-file interference, not a product defect (investigate before demo) |
| **Live compose verification** (Podman) | ✅ Several slices verified live (engines, adapters, LLM, orchestrator wiring) |

## 3. Demo assets

- **Full stack (live demo):** `scripts/dev/start-stack.sh` → **http://localhost:8090** (see `DEMO.md`). Requires Mac + Podman Desktop + applehv VM (4 CPU / 12 GB RAM), ~20–40 min first build. **Agent is server-guarded** — full stack must be started in a human terminal.
- **Static HTML (offline, no server):** `demo/ei-fraud-demo.html` (EI fraud executive demo — 6 interactive scenarios) and `demo/ads-dashboard-demo.html` (ADS real-time investigator dashboard). Verified fully self-contained (no external refs) — ideal offline leave-behind.

## 4. Honest limitations / known gaps (for demo credibility)

- Ensemble real ML models (XGBoost / Isolation Forest) **pending** — wiring complete, models not yet trained/retrained.
- Geographic engine: transit-route & economic signals not yet sourced from pipeline (Req 4.4 / 4.5) — honest MEDIUM "data unavailable" indicator currently reachable.
- Document Forgery: document-registry / narrative sources not yet fed from pipeline; OCR plug-in implemented but **model-gated** (no fine-tuned checkpoint shipped).
- LLM narrative inputs not yet pipeline-fed; checks skip when evidence absent.
- Unit suite has the 14 history-engine failures under full-suite run (see §2).

## 5. Readiness to work with the client's existing systems

- GCKey OAuth 2.0, Protected B, Privacy Act/PIPEDA, bilingual EN/FR, audit immutability, RBAC, monitoring — designed and verified in the regenerated implementation.
- Eight external data sources (StatsCan, CRA, provincial registries, CBSA, GeoNames, Business Registries, MVR, Job Bank) — GC secure-exchange patterns designed; mock-backed in dev.

## 6. Demo logistics

- Personas via **🔧 Demo as** switcher (Claimant / Investigator); full loop: claimant submits → pipeline scores → investigator reviews (live SSE queue, 3-column detail, SHAP, bilingual explanation, fraud-ring graph, dispositions).
- Only flagged claims appear in investigator queue (AUTO_APPROVE intentionally not surfaced).
- Client demo duration suggestion: 15–25 min live, or 10 min with static HTML.

## 7. Go / no-go signals before the client demo

- [ ] Unit suite green in full run (investigate/fix the 14 history-engine failures).
- [ ] Ensemble real models delivered (or demo explicitly framed as "wiring verified; models in training").
- [ ] Live stack run verified end-to-end (human terminal) — or static demos used for offline leave-behind.
