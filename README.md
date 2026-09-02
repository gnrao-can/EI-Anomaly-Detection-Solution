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
├── demo/                    # Static HTML demos (reference and pitch)
├── docs/
│   ├── architecture/        # Solution architecture documents
│   ├── specs/               # Requirements and design specs
│   └── data-sources/        # Data source integration specs and mock strategies
├── mock-data/               # Mock data fixtures for each external data source
│   ├── stats-canada/
│   ├── cra/
│   ├── provincial-registries/
│   ├── cbsa/
│   ├── geonames/
│   ├── business-registries/
│   ├── motor-vehicle/
│   └── job-bank/
└── src/                     # Application source (populated during implementation)
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

🟡 **Spec phase** — Requirements and design under review. Implementation pending approval.

## Getting Started

See [docs/specs/](docs/specs/) for requirements, design, and task breakdown.
