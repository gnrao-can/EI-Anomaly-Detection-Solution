# EI-ADS Static Demo — Offline Leave-Behind

Self-contained interactive demos for the **AI-Enabled Employment Insurance Anomaly Detection Solution (EI-ADS)**.

## Contents

| File | Purpose |
|---|---|
| `ei-fraud-demo.html` | **EI fraud detection executive demo** — interactive use-case explorer with **6 scenarios** covering the detection narrative (repetitive patterns, geographic anomaly, fraud rings, history inconsistency, forgery, regional analysis) and scoring/explanation. |
| `ads-dashboard-demo.html` | **ADS real-time investigator dashboard demo** — dark-theme investigator queue / case detail view (risk score, indicators, SHAP, decisions). |

## How to run (offline)

- Open either HTML file in a modern browser (**Chrome / Edge / Firefox / Safari**).
- **No server required** — fully self-contained (no external references, no scripts, no network calls).
- No build step, no dependencies, no internet needed.

## What's inside

- **EI fraud demo:** end-to-end detection narrative, multi-engine scenario walkthrough, risk scoring, explainability.
- **ADS dashboard demo:** investigator-facing queue and case review presentation.

## Related — full-stack live demo

For the complete end-to-end demo (claimant submits a claim → pipeline scores → investigator reviews), use the main repository:

- `EI-Anomaly-Detection-Solution/DEMO.md`
- `EI-Anomaly-Detection-Solution/scripts/dev/start-stack.sh` → **http://localhost:8090**

The static demos are the offline-ready subset of the full demo experience.
