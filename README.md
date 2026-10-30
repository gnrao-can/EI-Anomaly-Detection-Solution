# EI Anomaly Detection Solution (EI-ADS)

AI-Enabled Employment Insurance Anomaly Detection Solution — real-time fraud detection across Employment Insurance (EI), Old Age Security (OAS), and Canada Pension Plan (CPP) programs.

## Overview

EI-ADS is a production-grade implementation of the AI-enabled fraud detection system described in the [Solution Architecture](docs/architecture/FraudAI-Solution-Architecture.docx). It detects fraudulent and anomalous benefit claims in real time using a combination of machine learning, rule-based engines, graph analytics, and large language models.

## Key Capabilities

- **Repetitive Claim Pattern Detection** — Sequential pattern mining + LLM narrative analysis
- **Geographic Anomaly Detection** — Geospatial analysis with external data source validation
- **Employer Fraud Ring Detection** — Graph Neural Networks on the claimant-employer network
- **Employment History Inconsistency Detection** — CRA cross-reference + LLM consistency analysis
- **Document Forgery Detection** — LayoutLMv3 + forensic OCR + metadata analysis
- **Regional Employment Rate Analysis** — Time-series anomaly detection with Statistics Canada data

## Reference Demo

The interactive executive demo that guided this implementation lives in `demo/`:
- `demo/ei-fraud-demo.html` — EI fraud detection use-case explorer (6 interactive scenarios)
- `demo/ads-dashboard-demo.html` — ADS real-time investigator dashboard

Open either file in a browser — no server required.

## Project Structure

```
EI-Anomaly-Detection-Solution/
├── demo/                    # Static HTML demos (reference/pitch) + DEMO.md (local demo guide)
├── docs/
│   ├── architecture/        # Solution architecture (FraudAI-Solution-Architecture.docx)
│   ├── data-sources/        # External data source notes
│   ├── regeneration/        # STATUS.md — regeneration status & roadmap
│   ├── security/             # SBOM / security notes
│   └── specs/                # Requirements/design — authoritative specs live in .kiro/specs/
├── .kiro/
│   ├── specs/ei-anomaly-detection-solution/   # Requirements, Design, Tasks — authoritative
│   └── steering/regeneration-grounding.md
├── services/                # Detection engines, adapters, orchestrator, portal, LLM, scorer (18 services)
├── shared/                  # Shared Python package (models, auth, cache, metrics, telemetry, validation, retry)
├── tests/                   # Unit / contract / property / integration / load tests
├── k8s/                     # Kubernetes/OpenShift deployment manifests
├── mock-data/               # Mock data fixtures per external data source
├── scripts/                 # Build/deploy/test/migration scripts
├── src/                     # Placeholder only — application source lives in services/ + shared/
├── .env.example              # Environment template
├── .env · .env.dev-token    # Local/dev secrets (gitignored in prod)
├── podman-compose.yml       # Local full-stack compose
├── Containerfiles/           # Rootless OCI images per service
├── pyproject.toml · requirements.lock
├── data/ · sbom/ · .github/ · .tekton/ · .pre-commit-config.yaml · .gitignore · .containerignore
├── DEMO.md · README.md
```

## Data Sources

Eight external data sources are required. Each has a mock strategy for development and a production integration specification. See [docs/data-sources/](docs/data-sources/) for per-source specs.

| Data Source | Mock Strategy | Production Integration |
|---|---|---|
| Statistics Canada | JSON fixtures from open LFS data | REST API (statcan.gc.ca) |
| CRA Tax Data | Synthetic T4/ROE data generator | Secure government data exchange |
| Provincial Registries | Static fixture JSON per province | Provincial REST APIs |
| CBSA Border Data | Synthetic travel records | CBSA secure API |
| GeoNames | Real GeoNames dump (open data) | GeoNames REST API |
| Business Registries | Synthetic corporate records | Corporations Canada API |
| Motor Vehicle Registry | Synthetic provincial records | Provincial MVR APIs |
| Job Bank Canada | Real Job Bank open data snapshot | Job Bank REST API |

## Status

🟢 **Implementation phase** — Regenerated implementation verified against the Requirements and Design.

- All six Detection_Engines are composed and exercised over real transports (Kafka, real HTTP, GCKey JWKS auth) via integration tests; the Ensemble Scorer wiring is complete (real ML models pending).
- Live local demo stack available — `scripts/dev/start-stack.sh` → http://localhost:8090 (see `DEMO.md`).

**Next:** real ensemble models (XGBoost / Isolation Forest); Kubernetes/OpenShift deployment; production data sources.

Authoritative sources: [Requirements](.kiro/specs/ei-anomaly-detection-solution/requirements.md) · [Design](.kiro/specs/ei-anomaly-detection-solution/design.md) · [Tasks](.kiro/specs/ei-anomaly-detection-solution/tasks.md); living regeneration log: [docs/regeneration/STATUS.md](docs/regeneration/STATUS.md).

## Getting Started

See [`.kiro/specs/ei-anomaly-detection-solution/`](.kiro/specs/ei-anomaly-detection-solution/) for requirements, design, and task breakdown (authoritative); see [docs/regeneration/STATUS.md](docs/regeneration/STATUS.md) for implementation status.
