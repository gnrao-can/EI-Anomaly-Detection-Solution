# EI-ADS — Demo Readiness Package

Deliverables prepared for the potential-client demo (federal department leading a similar effort).

## Contents

1. **Demo readiness checklist** — `CHECKLIST.md`
   - Solution characterization, verification evidence, demo assets, honest limitations, readiness, logistics, go/no-go signals.

2. **PowerPoint slide deck** — `EI-ADS_Demo_Readiness.pptx`
   - AI-Enabled Employment Insurance Fraud Detection — Solution & Readiness
   - Source: `EI-ADS-Demo-Readiness.pptx.md`

3. **Static HTML demos** — `static/`
   - Self-contained, offline-capable interactive demos (see `static/README.md`).

4. **Full-stack live demo** — see the main repository:
   - `EI-Anomaly-Detection-Solution/DEMO.md`
   - `EI-Anomaly-Detection-Solution/scripts/dev/start-stack.sh` → **http://localhost:8090**

## Package purpose

This package supports the client demo for the **AI-Enabled Employment Insurance Anomaly Detection Solution (EI-ADS)**, including the offline leave-behind (static HTML demos) and the readiness assessment used to frame the solution.

## Status

- Implementation phase — regenerated implementation verified against Requirements & Design (see `EI-Anomaly-Detection-Solution/README.md`).
- Known pending items: real ensemble ML models; Kubernetes/OpenShift deployment; production data sources (see checklist §4/§7).
