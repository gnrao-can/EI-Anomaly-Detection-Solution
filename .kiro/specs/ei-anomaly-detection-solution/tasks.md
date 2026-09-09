# Implementation Plan: AI-Enabled Employment Insurance Anomaly Detection Solution (EI-ADS)

## Overview

This plan converts the EI-ADS design into an ordered sequence of coding tasks grouped into eight phases: Foundation, Core Services, Detection Engines, External Data Adapters, Frontend, Observability, Testing, and Deployment. Each task builds on the previous so that no code is left orphaned. All implementation uses Python (FastAPI, asyncio, Hypothesis) as specified in the design document. All services are packaged as OCI-compliant container images built with **Podman** (rootless) using `Containerfile` descriptors, and pushed to an internal container registry for deployment on OpenShift/Kubernetes.

All open-source libraries are pinned to their latest stable versions as of September 2026 with no known CVEs. A CycloneDX Software Bill of Materials (SBOM) is generated for every container image at build time using **Syft** and validated against the **OSV vulnerability database** using **Grype**, satisfying Government of Canada supply-chain security requirements.

---

## Tasks

---

### Phase 1: Foundation — Project Scaffolding and Infrastructure

- [x] 1. Set up monorepo project structure and shared Python configuration
  - [x] 1.1 Create top-level directory layout: `services/`, `shared/`, `tests/`, `k8s/`, `mock-data/`, `scripts/`
    - Create `pyproject.toml` (Poetry workspace) at the root with all dependencies pinned to exact versions (no `^` or `~` ranges); shared dev dependencies: `pytest==8.3.4`, `hypothesis==6.115.0`, `black==24.10.0`, `ruff==0.8.4`, `mypy==1.13.0`; run `pip-audit` and `safety check` against this file before committing and fail if any CVE with CVSS ≥ 7.0 is found
    - Create `.env.example` and `podman-compose.yml` for local development with Kafka, Redis, PostgreSQL, ClickHouse, Neo4j — using `podman-compose` (pip install podman-compose); all containers run rootless via Podman
    - Write a `Containerfile` (not Dockerfile) for each service in `services/<service-name>/Containerfile`; all images use `registry.access.redhat.com/ubi9/python-311` as the base to ensure OpenShift compatibility and rootless execution
    - Create `shared/` Python package with `__init__.py`; install as editable dependency across all services
    - _Requirements: 1.11, 17.5_

  - [x] 1.2 Define all canonical data models in `shared/models.py`
    - Implement `Claim`, `Indicator`, `RiskAssessment`, `ShapAttribution`, `LabourForceData` dataclasses exactly as specified in the design
    - Implement `ClaimStatus`, `EngineType`, `Severity`, `RiskDecision` enumerations
    - Add `__eq__`, `__hash__`, and `to_dict()` / `from_dict()` methods to all dataclasses
    - _Requirements: 1.4, 2.6, 9.1_

  - [x] 1.3 Implement SIN validation utilities in `shared/validation.py`
    - Implement Luhn checksum for SIN
    - Implement BN format validation (9 digits + RT + 4 digits)
    - Implement `validate_claim_fields()` that checks mandatory fields and returns structured errors
    - _Requirements: 1.2, 1.3_

  - [x] 1.4 Create a pinned dependency manifest for all Python services in `scripts/dependencies/`
    - Write `scripts/dependencies/python-versions.toml` listing every Python library used across all 13 services at its exact pinned version (no ranges):
      - **Runtime — FastAPI stack:** `fastapi==0.115.6`, `uvicorn[standard]==0.32.1`, `pydantic==2.10.3`, `httpx==0.28.1`, `asyncpg==0.30.0`, `aiokafka==0.12.0`, `redis==5.2.1`
      - **Runtime — ML/AI:** `xgboost==2.1.3`, `shap==0.46.0`, `scikit-learn==1.6.0`, `torch==2.5.1`, `torch-geometric==2.6.1`, `transformers==4.47.1`, `langchain==0.3.12`, `langchain-community==0.3.12`, `llama-cpp-python==0.3.4`, `sentence-transformers==3.3.1`, `prophet==1.1.6`, `networkx==3.4.2`
      - **Runtime — Document AI:** `pypdf==5.1.0`, `pdfminer.six==20231228`, `cryptography==44.0.0`, `pillow==11.0.0`
      - **Runtime — Data adapters:** `zeep==4.3.1`, `lxml==5.3.0`, `clickhouse-connect==0.8.12`, `neo4j==5.27.0`
      - **Dev/Test:** `pytest==8.3.4`, `hypothesis==6.115.0`, `testcontainers==4.9.0`, `locust==2.32.3`, `schemathesis==3.39.4`, `pip-audit==2.7.3`, `safety==3.2.11`
    - Write `scripts/dependencies/frontend-versions.json` listing every npm package at its exact pinned version:
      - `"react": "18.3.1"`, `"react-dom": "18.3.1"`, `"typescript": "5.7.2"`, `"vite": "6.0.5"`, `"react-router-dom": "7.1.1"`, `"i18next": "24.2.0"`, `"react-i18next": "15.2.0"`, `"recharts": "2.15.0"`, `"@testing-library/react": "16.1.0"`, `"jest-axe": "9.0.0"`, `"axe-core": "4.10.2"`
    - After writing both files, run `pip-audit --requirement scripts/dependencies/python-versions.toml` and `npm audit --audit-level=high --prefix services/claimant-portal`; fail if any finding has CVSS ≥ 7.0 — document any accepted exceptions in `scripts/dependencies/EXCEPTIONS.md` with rationale and planned remediation date
    - _Requirements: 19.6_

- [x] 2. Provision infrastructure configuration files
  - [x] 2.1 Create PostgreSQL schema migration scripts in `scripts/migrations/`
    - Write `001_create_claims.sql`, `002_create_risk_assessments.sql`, `003_create_indicators.sql`, `004_create_audit_log.sql` using the exact DDL from the design
    - Add row-level security policy to `audit_log` (INSERT only)
    - Add all indexes specified in the design
    - _Requirements: 1.4, 2.6, 2.7, 19.3_

  - [x] 2.2 Create ClickHouse schema scripts in `scripts/clickhouse/`
    - Write DDL for `claims_analytics`, `indicators_analytics`, `risk_assessments_analytics`, `shap_attributions`, `adapter_audit_log`
    - Configure MergeTree engines, TTL expressions (7 years), and partition keys as specified in the design
    - _Requirements: 2.6, 19.3_

  - [x] 2.3 Create Kafka topic configuration in `scripts/kafka/`
    - Write a topic-creation script that provisions all 11 topics with the exact partition counts, retention, and replication factors from the design
    - Include `min.insync.replicas=2` for all topics
    - _Requirements: 2.1, 23.4_

  - [x] 2.4 Create Redis cluster configuration in `scripts/redis/`
    - Write `redis-cluster.conf` for 3 primary + 2 replica topology
    - Implement `shared/cache.py` with a `RedisCache` class wrapping `redis-py` that exposes `get`, `set_with_ttl`, and `invalidate` methods
    - Add all cache key patterns from the design as named constants in `shared/cache_keys.py`
    - _Requirements: 17.4, 23.5_

- [x] 3. Checkpoint — Verify infrastructure scripts and shared models
  - Ensure all migration scripts apply cleanly against a local `podman-compose` stack. Ensure all data model dataclasses import and instantiate without error. Run `pip-audit` against the root `pyproject.toml` lock file and assert zero HIGH/CRITICAL CVEs before proceeding. Ask the user if questions arise.

---

### Phase 2: Core Services — Intake, Orchestration, and Scoring

- [x] 4. Implement the Claim Intake Service in `services/claim-intake/`
  - [x] 4.1 Create the FastAPI application skeleton with GCKey OAuth 2.0 middleware
    - Set up the FastAPI app, lifespan handler for DB and Kafka connections, and a `POST /api/v1/claims` route stub
    - Implement the GCKey JWKS token validation dependency (`shared/auth.py`) that returns 401/403 as specified
    - Implement the RBAC scope checker for CLAIMANT, REVIEWER, INVESTIGATOR, ADMINISTRATOR roles
    - _Requirements: 1.1, 19.7, 22.6_

  - [x] 4.2 Implement claim validation and persistence logic
    - Wire `validate_claim_fields()` into the intake endpoint; return structured error JSON on failure within 200 ms
    - Implement `ClaimRepository` using asyncpg to insert claims with status RECEIVED
    - Hash raw SIN with SHA-256 + per-environment salt immediately on receipt; never persist plaintext SIN
    - _Requirements: 1.2, 1.3, 1.4, 1.11, 19.1_

  - [x] 4.3 Implement Kafka publish and transactional rollback
    - Implement `KafkaProducer` wrapper around `aiokafka` with `claim_id` as the partition key
    - Implement two-phase commit: persist to PG, publish to Kafka; roll back PG write if Kafka publish fails within 500 ms
    - Return structured error to claimant if either PG write or Kafka publish fails
    - _Requirements: 1.4, 1.5_

  - [x] 4.4 Write unit tests for claim intake validation and persistence
    - Test all mandatory field validations, SIN Luhn, BN format, earnings > 0, hours > 0, separation date ≤ today
    - Test rollback: simulate Kafka failure → assert no Claim_ID and no PG row
    - _Requirements: 1.2, 1.3, 1.4, 1.5_

- [x] 5. Implement the ROE Adapter in `services/roe-adapter/`
  - [x] 5.1 Create the FastAPI app with REST (JSON/XML) and SOAP 1.1/1.2 endpoints
    - Implement `/roe/rest` endpoint accepting JSON and XML payloads
    - Implement SOAP handler using `zeep` or `spyne`; parse WSDL from ROE Web
    - Normalise all ROE field names to canonical `Claim` model; detect and convert UTF-8 / ISO-8859-1 encoding
    - Publish `roe.received` event to Kafka with `claim_id` partition key
    - Return structured error and suppress Kafka publish on normalisation failure
    - _Requirements: 1.6, 1.7, 1.8_

  - [x] 5.2 Write unit tests for ROE normalisation
    - Test REST JSON, REST XML, and SOAP payloads normalising to identical `Claim` objects
    - Test encoding detection for ISO-8859-1 input
    - Test that normalisation failure returns error and publishes nothing
    - _Requirements: 1.6, 1.7, 1.8_

- [x] 6. Implement the Detection Pipeline Orchestrator in `services/pipeline-orchestrator/`
  - [x] 6.1 Create the Kafka consumer that correlates `claim.received` and `roe.received` events
    - Implement event correlation with a 60-second timeout using asyncio and Redis as the pairing store
    - On pairing, trigger parallel engine dispatch within 100 ms of the later event timestamp
    - _Requirements: 2.1_

  - [x] 6.2 Implement parallel engine dispatch using `asyncio.TaskGroup`
    - Implement async HTTP calls to all six engine service endpoints, each wrapped in `asyncio.wait_for` with per-engine timeouts from the design
    - Collect results; on timeout emit PARTIAL_RESULT Indicator with engine ID and elapsed time
    - If < 3 engines complete, assign INDETERMINATE decision and halt payment; publish to Kafka
    - _Requirements: 2.2, 2.3, 2.4, 2.5_

  - [x] 6.3 Wire Ensemble Scorer call and final event publication
    - After engine collection, POST Indicator list to Ensemble Scorer service within 300 ms budget
    - Publish `claim.scored` event and, if INVESTIGATE, `claim.escalated` event within 500 ms of decision
    - Write complete audit trail entry to ClickHouse within 500 ms for every adapter call and intermediate output
    - _Requirements: 2.6, 2.7, 2.8_

  - [x] 6.4 Write integration tests for the orchestrator event flow
    - Use `testcontainers` to spin up Kafka; produce a paired `claim.received` + `roe.received`; assert all six engine endpoints are called concurrently (mock engines return instantly)
    - Simulate two engine timeouts; assert PARTIAL_RESULT Indicators are produced and scoring still completes
    - Simulate four engine timeouts; assert INDETERMINATE decision
    - _Requirements: 2.1, 2.3, 2.4, 2.5_

- [x] 7. Implement the Ensemble Scorer in `services/ensemble-scorer/`
  - [x] 7.1 Implement XGBoost and Isolation Forest feature engineering and scoring
    - Implement `FeatureEncoder` that converts an `Indicator` list to the XGBoost feature vector (one-hot engine types, ordinal severity, completion flags)
    - Load pre-trained XGBoost model from `models/xgboost_v1.joblib`; scale probability [0,1] to [0,100] integer Risk_Score
    - Load Isolation Forest model; compute anomaly bonus in [0, 30]; compute `min(100, round(xgb_score * 70 + if_score * 30))`
    - Apply decision thresholds: [0,29] → AUTO_APPROVE, [30,69] → FLAG_FOR_REVIEW, [70,100] → INVESTIGATE
    - _Requirements: 9.1, 9.2_

  - [x] 7.2 Implement SHAP TreeExplainer attribution
    - Use `shap.TreeExplainer(xgboost_model)` to produce `shap_values` for each scoring request
    - Build `ShapAttribution` list (one per Indicator) and validate additivity: `|sum(contributions) - (risk_score - base_value)| < 1e-4`
    - Persist `RiskAssessment` to PostgreSQL and ClickHouse (`risk_assessments`, `shap_attributions` tables)
    - _Requirements: 9.3_

  - [x] 7.3 Write property test for SHAP additivity (P7)
    - **Property 7: SHAP additivity invariant**
    - **Validates: Requirements 9.3**
    - Use Hypothesis `@given(indicator_count=st.integers(min_value=0, max_value=20))` to generate random Indicator sets and assert sum of contributions equals Risk_Score minus base value within 1e-4
    - _Requirements: 9.3_

  - [x] 7.4 Write property test for zero Indicators (P8)
    - **Property 8: Zero Indicators produces zero score**
    - **Validates: Requirements 9.7**
    - Assert that `score([])` returns Risk_Score = 0 and Risk_Decision = AUTO_APPROVE
    - _Requirements: 9.7_

  - [x] 7.5 Write property test for score monotonicity (P9)
    - **Property 9: Risk Score monotonicity**
    - **Validates: Requirements 9.8**
    - Use Hypothesis to generate pairs (I1 ⊃ I2) and assert `score(I1) >= score(I2)` for all pairs
    - _Requirements: 9.8_

- [x] 8. Implement the LLM Service in `services/llm-service/`
  - [x] 8.1 Implement LangChain + Llama 3.1 chain with RAG pipeline
    - Configure LangChain with Llama 3.1 70B (GGUF 4-bit) via `llama-cpp-python`; set up Chroma vector store in `data/chroma/`
    - Implement `POST /llm/similarity` endpoint: embed two narratives with `sentence-transformers` (all-MiniLM-L6-v2), return cosine similarity [0,1]
    - _Requirements: 3.5, 6.6_

  - [x] 8.2 Implement bilingual explanation generation endpoint
    - Implement `POST /llm/explain`: rank Indicators by |SHAP contribution| descending; select top 3; build chain-of-thought prompt with EN/FR system role
    - Return `{en: str, fr: str, word_count_en: int, word_count_fr: int}`; enforce 500-char limit per language; enforce 5-second timeout
    - On timeout, return `EXPLANATION_UNAVAILABLE` flag; on malformed LLM response, fall back to template-based explanation
    - _Requirements: 9.4, 9.5, 9.9, 20.2, 20.3_

  - [x] 8.3 Write property test for bilingual word count balance (P15)
    - **Property 15: Bilingual word count balance**
    - **Validates: Requirements 20.6**
    - Use Hypothesis `@given(indicator_set=st.lists(...))` with a mock LLM that generates proportional EN/FR pairs; assert `|en_words - fr_words| / max(en_words, fr_words) <= 0.20`
    - _Requirements: 20.6_

- [x] 9. Implement the Graph Service in `services/graph-service/`
  - [x] 9.1 Implement Neo4j graph construction and upsert logic
    - Connect to Neo4j via `neo4j` Python driver; implement `upsert_claimant`, `upsert_employer`, `add_employed_by_edge` using the Cypher schema from the design
    - Implement `POST /graph/construct` endpoint that upserts nodes/edges for the current claim
    - _Requirements: 5.1_

  - [x] 9.2 Implement community detection with Louvain + GraphSAGE GNN
    - Implement Louvain community detection on the projected claimant-only graph (shared employer = edge) using `networkx`
    - Implement GraphSAGE (2-layer, hidden dim 64) in PyTorch Geometric; load pre-trained weights from `models/graphsage_v1.pt`
    - Implement `POST /graph/community-detect` endpoint returning community partition as `{claimant_id: community_id}` dict
    - _Requirements: 5.5, 5.10_

  - [x] 9.3 Write property test for community partition completeness (P3)
    - **Property 3: Community partition completeness**
    - **Validates: Requirements 5.10**
    - Use Hypothesis to generate random graphs of N nodes; assert partition covers all N nodes with no overlaps and sum of community sizes = N
    - _Requirements: 5.10_

- [x] 10. Checkpoint — Verify all core services start and respond to health checks
  - Run `podman-compose up` with all services; assert each `/healthz` endpoint returns 200. Run unit tests for intake, ROE adapter, and ensemble scorer. Ask the user if questions arise.

---

### Phase 3: Detection Engines

- [x] 11. Implement the Repetitive Pattern Engine in `services/repetitive-pattern-engine/`
  - [x] 11.1 Implement 60-month claim history retrieval and sequential pattern mining
    - Implement `ClaimHistoryRepository` querying PostgreSQL by `sin_hash` over 60-month rolling window
    - Implement `check_employer_repetition()`: flag ≥ 3 claims from same employer BN in any 36-month window → HIGH Indicator
    - Implement `PrefixSpan` sequential pattern mining over claim date sequences; flag inter-claim intervals within ±14 days → HIGH Indicator
    - On DB unavailable, produce DATA_UNAVAILABLE Indicator and return without further analysis
    - _Requirements: 3.1, 3.2, 3.3, 3.8_

  - [x] 11.2 Implement hours-clustering and LLM narrative similarity checks
    - Implement `check_hours_clustering()`: compare insurable hours to regional minimum threshold; flag ±5 hours in ≥ 2 prior claims → HIGH Indicator
    - Implement async LLM similarity call via `httpx` to LLM Service `/llm/similarity`; 5-second timeout; on timeout write NARRATIVE_ANALYSIS_TIMEOUT flag
    - Implement `POST /engine/analyze` endpoint; enforce 400 ms wall-clock budget (excluding LLM wait) via asyncio timeout
    - Publish all Indicators to Kafka `indicators.repetitive-pattern` topic
    - _Requirements: 3.4, 3.5, 3.6, 3.7_

  - [x] 11.3 Write property test for sequential pattern detection completeness (P1)
    - **Property 1: Sequential pattern detection completeness**
    - **Validates: Requirements 3.8**
    - Use Hypothesis `@given(n, base_date, intervals)` to generate N ≥ 3 claims with inter-claim intervals within ±14 days; assert at least one pattern Indicator is produced
    - _Requirements: 3.8_

  - [x] 11.4 Write unit tests for Repetitive Pattern Engine
    - Test DATA_UNAVAILABLE indicator on DB failure
    - Test exactly 2 same-employer claims (no flag), exactly 3 (flag)
    - Test hours clustering at threshold ±5 boundary
    - Test NARRATIVE_ANALYSIS_TIMEOUT on LLM timeout
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.6_

- [x] 12. Implement the Geographic Anomaly Engine in `services/geographic-anomaly-engine/`
  - [x] 12.1 Implement Haversine distance computation and GeoNames resolution
    - Implement `haversine(lat1, lon1, lat2, lon2)` exactly as specified (returns float rounded to 4 dp)
    - Implement async calls to GeoNames Adapter to resolve residential and work-location coordinates
    - On resolution failure, generate MEDIUM Indicator and skip distance-dependent analysis
    - _Requirements: 4.1, 4.2, 4.10_

  - [x] 12.2 Implement transport implausibility, MVR, and CBSA checks
    - If distance > 100 km: query MVR Adapter; no vehicle → HIGH Indicator; no transit → HIGH Indicator
    - Compute weekly transport cost vs weekly EI benefit; if cost > benefit → CRITICAL Indicator with CAD amounts to 2 dp and ratio
    - Query CBSA Adapter; ≥ 10 consecutive days outside Canada → HIGH Indicator
    - Check 2 prior employer locations > 100 km; add employer names, distances, dates to Indicator
    - Enforce 600 ms wall-clock budget; publish all Indicators to Kafka `indicators.geographic-anomaly`
    - _Requirements: 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9_

  - [x] 12.3 Write property test for geodesic distance symmetry and self-distance (P2)
    - **Property 2: Geodesic distance symmetry and self-distance**
    - **Validates: Requirements 4.10**
    - Use Hypothesis `@given(lat1, lon1, lat2, lon2)` with floats in valid bounds; assert `distance(A,B) == distance(B,A)` within 0.001 and `distance(A,A) == 0`
    - _Requirements: 4.10_

  - [x] 12.4 Write unit tests for Geographic Anomaly Engine
    - Test exactly 100 km (no flag), 100.001 km (flag triggers)
    - Test CBSA 9 consecutive days (no flag), 10 days (flag)
    - Test MVR UNAVAILABLE → treat ownership as UNKNOWN (no HIGH Indicator)
    - _Requirements: 4.3, 4.9, 15.2, 15.3_

- [x] 13. Implement the Fraud Ring Engine in `services/fraud-ring-engine/`
  - [x] 13.1 Implement shell employer detection and coordinated layoff detection
    - Query Business Registry Adapter for employer BN; if registered within 12 months of separation AND residential address → CRITICAL Indicator; if no record → HIGH Indicator
    - Load all employees separated in 90-day window; if ≥ 80% share same separation date AND all ROEs in any 24-hour window → CRITICAL Indicator
    - _Requirements: 5.2, 5.3, 5.4_

  - [x] 13.2 Implement GNN community detection and hours-band analysis
    - Call Graph Service `/graph/construct` and `/graph/community-detect` for current claim's employer cohort
    - If ≥ 40% of cohort share same postal FSA or surname_hash → CRITICAL Indicator with matched attribute type and claimant list
    - If cohort ≥ 3 AND all insurable hours within 10-hour band at or above min qualifying threshold → HIGH Indicator; if cohort < 3 → NOT_EVALUATED
    - Enforce 800 ms wall-clock budget; publish all Indicators to Kafka `indicators.fraud-ring`
    - _Requirements: 5.5, 5.6, 5.7, 5.8, 5.9_

  - [x] 13.3 Write property test for community partition completeness (P3) — wire to Graph Service
    - **Property 3: Community partition completeness (integrated)**
    - **Validates: Requirements 5.10**
    - Generate random employer graphs via Hypothesis; call `/graph/community-detect`; assert every node in exactly one community and community sizes sum to N
    - _Requirements: 5.10_

  - [x] 13.4 Write unit tests for Fraud Ring Engine
    - Test cohort < 3 → NOT_EVALUATED for hours-band check
    - Test 79% same-date layoff (no flag), 80% (flag)
    - Test no Business Registry record → HIGH Indicator
    - _Requirements: 5.3, 5.4, 5.6, 5.7_

- [x] 14. Implement the Employment History Inconsistency Engine in `services/history-inconsistency-engine/`
  - [x] 14.1 Implement CRA T4 retrieval and earnings discrepancy check
    - Implement async call to CRA Adapter (5-second timeout); on unavailable → UNRESOLVED Indicator; on missing/non-numeric fields → UNRESOLVED Indicator
    - Implement `compute_discrepancy(roe_earnings, t4_earnings)` using exact `Decimal` arithmetic; if |difference| > $2,000 → CRITICAL Indicator
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.10_

  - [x] 14.2 Implement NOC validation, concurrent employment, and LLM contradiction checks
    - Map CRA employer industry category to NOC major group; if declared NOC major group != CRA industry NOC → HIGH Indicator
    - Query CRA for all BNs active during claim period; if second employer overlaps → CRITICAL Indicator with BN, start/end dates
    - Async LLM contradiction check (10-second timeout); on YES → CRITICAL Indicator; on timeout → NARRATIVE_ANALYSIS_TIMEOUT flag
    - Enforce 500 ms wall-clock budget (excluding LLM wait); publish all Indicators to Kafka `indicators.history-inconsistency`
    - _Requirements: 6.5, 6.6, 6.7, 6.8, 6.9_

  - [x] 14.3 Write property test for zero earnings discrepancy invariant (P4)
    - **Property 4: Zero earnings discrepancy invariant**
    - **Validates: Requirements 6.10**
    - Use Hypothesis `@given(earnings=st.decimals(...))` with equal ROE and T4 values; assert discrepancy == Decimal("0") and no CRITICAL discrepancy Indicator
    - _Requirements: 6.10_

  - [x] 14.4 Write unit tests for History Inconsistency Engine
    - Test $2,000 discrepancy (no flag), $2,000.01 (flag)
    - Test CRA timeout → UNRESOLVED Indicator
    - Test concurrent employment with same-day start → CRITICAL Indicator
    - _Requirements: 6.1, 6.2, 6.4, 6.8_

- [x] 15. Implement the Document Forgery Engine in `services/document-forgery-engine/`
  - [x] 15.1 Implement LayoutLMv3 OCR extraction and internal consistency checks
    - Load LayoutLMv3 model via `transformers`; extract all 11 mandatory ROE fields with confidence score per field
    - Flag fields with confidence < 0.85 as EXTRACTION_LOW_CONFIDENCE
    - Check `hours × hourly_rate vs earnings` (> 2% deviation → CRITICAL); check `last_day_paid < first_day_worked` → CRITICAL
    - _Requirements: 7.1, 7.2_

  - [x] 15.2 Implement PDF metadata forensics and font fingerprinting
    - Use `pypdf` to extract PDF creation_timestamp; if creation_timestamp > issue_date + 24h → CRITICAL Indicator with both timestamps
    - Use `pdfminer.six` to extract font family and point size; compare to employer font profile in document registry; if mismatch → CRITICAL Indicator
    - Skip font check if employer has < 3 prior authenticated ROEs
    - _Requirements: 7.3, 7.4_

  - [x] 15.3 Implement digital signature verification, payroll cross-reference, and LLM style analysis
    - Extract PKCS#7 / CAdES signature using `cryptography` library; verify against employer X.509 certificate hash
    - If no signature → CRITICAL Indicator; if hash mismatch → CRITICAL Indicator
    - Query CRA Adapter for SIN in payroll during employment period; if not found → CRITICAL Indicator
    - If employer has ≥ 5 authenticated ROEs: async LLM style check; on YES → MEDIUM Indicator
    - Enforce 700 ms wall-clock budget; publish all Indicators to Kafka `indicators.document-forgery`
    - _Requirements: 7.5, 7.6, 7.7, 7.8, 7.9, 7.10, 7.11_

  - [x] 15.4 Write property test for authenticated document zero indicators (P5)
    - **Property 5: Authenticated document zero indicators**
    - **Validates: Requirements 7.12**
    - Use Hypothesis to generate ROE documents already in the authenticated registry; assert zero forgery Indicators
    - _Requirements: 7.12_

  - [x] 15.5 Write property test for OCR round-trip fidelity (P6)
    - **Property 6: OCR round-trip fidelity**
    - **Validates: Requirements 7.13**
    - Use Hypothesis to generate `ROEFields` objects; call `parse_from_canonical(render_to_canonical(fields))`; assert exact value matches on numerics/dates and ≤ 1 Levenshtein distance on free-text fields ≥ 20 chars
    - _Requirements: 7.13_

  - [x] 15.6 Write unit tests for Document Forgery Engine
    - Test no digital signature → CRITICAL Indicator
    - Test creation_timestamp within 24h of issue_date (no flag), 24h+1s (flag)
    - Test font match (no flag), font mismatch (flag)
    - Test employer < 3 ROEs → font check skipped; employer < 5 ROEs → LLM style check skipped
    - _Requirements: 7.4, 7.5, 7.6, 7.9_

- [x] 16. Implement the Regional Analysis Engine in `services/regional-analysis-engine/`
  - [x] 16.1 Implement Stats Canada and Job Bank data retrieval and rate deviation check
    - Async call to Stats Canada Adapter for regional and national unemployment rates for reference month; on unavailable → annotate Indicators with DATA_UNAVAILABLE
    - Compute deviation = regional_rate − national_rate; if deviation < −1.5 pp → MEDIUM Indicator with both rates and deviation
    - Query Job Bank Adapter for active postings (NOC code + region + claim period); if > 200 → HIGH Indicator; if median_wage > hourly_earnings × 1.15 → HIGH Indicator
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6_

  - [x] 16.2 Implement seasonality annotation, recurring claim detection, and Prophet forecast
    - Query EI-ADS seasonal pattern database for sector employment index; if index > 12-month rolling avg × 1.10 → annotate all Indicators with SEASONAL flag
    - Check claim history for same calendar quarter in prior 3 years; if ≥ 2 of 3 → MEDIUM Indicator with matching years and quarters
    - Fit Prophet model on 24-month regional unemployment history (config: multiplicative, annual seasonality, changepoint_prior_scale=0.05, interval_width=0.95); record forecast for monitoring (not an Indicator trigger)
    - Enforce 500 ms wall-clock budget; publish all Indicators to Kafka `indicators.regional-analysis`
    - _Requirements: 8.7, 8.8, 8.9, 8.11_

  - [x] 16.3 Write property test for zero rate deviation invariant (P13)
    - **Property 13: Zero rate deviation invariant**
    - **Validates: Requirements 8.10**
    - Use Hypothesis `@given(rate=st.decimals(...))` with equal regional and national rates; assert deviation == Decimal("0") and no regional unemployment Indicator
    - _Requirements: 8.10_

  - [x] 16.4 Write property test for time-series forecast calibration (P14)
    - **Property 14: Time-series forecast calibration**
    - **Validates: Requirements 8.11**
    - Use Hypothesis to generate historical unemployment series of N ≥ 12 monthly observations; fit Prophet; assert actual value falls within 95% PI across ≥ 95% of generated series
    - _Requirements: 8.11_

  - [x] 16.5 Write unit tests for Regional Analysis Engine
    - Test deviation exactly −1.5 pp (no flag), −1.5001 pp (flag)
    - Test exactly 200 postings (no flag), 201 (flag)
    - Test median wage exactly 15% above hourly earnings (no flag), 15.01% above (flag)
    - Test recurring claim in only 1 of 3 prior years (no flag), 2 of 3 (flag)
    - _Requirements: 8.3, 8.4, 8.6, 8.8_

- [x] 17. Checkpoint — All six detection engines produce Indicators for test claims
  - Run each engine's unit tests and property tests. Feed a synthetic claim through the full pipeline via `podman-compose` and assert Risk_Decision is produced. Ask the user if questions arise.

---

### Phase 4: External Data Adapters

- [x] 18. Implement the Stats Canada Adapter in `services/stats-canada-adapter/`
  - [x] 18.1 Implement LFS API fetch, canonical parsing, and Redis caching
    - Implement `GET /statscan/lfs/{region_code}/{year_month}` endpoint
    - Fetch from `https://www150.statcan.gc.ca/t1/tbl1/en/dtbl/download/98100424.json` with 10 s connect / 30 s read timeouts
    - Parse to `list[LabourForceData]` retaining values to ≥ 4 decimal places via `Decimal(str(value))`
    - Write to Redis key `statscan:lfs:{region_code}:{year_month}` with 30-day TTL
    - On 5xx: fall back to Redis cache with DATA_STALE flag; if cache > 30 days old or empty → DATA_UNAVAILABLE
    - In dev/test: serve from `mock-data/stats-canada/` fixtures
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6_

  - [x] 18.2 Write property test for Stats Canada adapter round-trip (P10)
    - **Property 10: Stats Canada adapter round-trip**
    - **Validates: Requirements 10.7**
    - Use Hypothesis to generate `StatCanLFSResponse` objects; assert `parse(serialise(parse(raw)))` covers all ESDC regions and preserves rates to ≥ 4 decimal places
    - _Requirements: 10.7_

- [x] 19. Implement the CRA Adapter in `services/cra-adapter/`
  - [x] 19.1 Implement mTLS data exchange, data minimisation, and audit logging
    - Implement `GET /cra/t4/{sin_hash}/{tax_year}/{employer_bn}` with mTLS 1.3 client certificate configuration
    - Filter response to `ALLOWED_FIELDS = {sin, tax_year, employer_bn, employment_income, noc_category}` only
    - 5-second total timeout; return structured UNAVAILABLE within 200 ms of timeout threshold
    - Discard T4 records > 10 years old; log warning
    - Log every access to ClickHouse `adapter_audit_log` within 1 second with Claim_ID, component, fields_accessed, timestamp
    - In dev/test: generate synthetic T4 records with same field structure
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 11.8_

- [x] 20. Implement the Business Registry Adapter in `services/business-registry-adapter/`
  - [x] 20.1 Implement federal and provincial registry queries with PARTIAL_RESULT handling
    - Query Corporations Canada API + 13 provincial/territorial APIs concurrently via `asyncio.gather`
    - On unavailable province: continue and collect available; return PARTIAL_RESULT listing unreachable provinces
    - If all fail: return SERVICE_UNAVAILABLE
    - Classify registered address as residential/commercial using GeoNames Adapter (fallback to postal FSA ranges if GeoNames unavailable; flag UNVERIFIED_ZONING)
    - In dev/test: serve from `mock-data/business-registries/` and `mock-data/provincial-registries/` (13 fixtures)
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6_

  - [x] 20.2 Write unit tests for Business Registry Adapter round-trip serialisation
    - Serialise and re-parse a canonical `EmployerRegistration`; assert byte-equivalent JSON structure
    - _Requirements: 12.7_

- [x] 21. Implement the CBSA Adapter in `services/cbsa-adapter/`
  - [x] 21.1 Implement CBSA secure-channel fetch and absence detection
    - Implement `GET /cbsa/travel/{sin_hash}` with mTLS over CBSA Protected B channel
    - 5-second timeout; preserve partial records on timeout; no auto-retry; return failure result
    - Implement `detect_absence_periods()` as specified in the design (EXIT→ENTRY gaps ≥ 10 days)
    - Do NOT log travel record contents to any general-purpose log
    - Respond within 300 ms for responses with ≤ 500 travel records
    - In dev/test: generate from `mock-data/cbsa/` deterministic generator based on province of employment
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6_

- [x] 22. Implement the GeoNames Adapter in `services/geonames-adapter/`
  - [x] 22.1 Implement coordinate resolution, Haversine computation, and local fallback
    - Implement `GET /geonames/resolve/{postal_code}` using GeoNames REST API; return RESOLUTION_FAILED (never default coordinates) on failure
    - Validate coordinates: if lat ∉ [-90, 90] or lon ∉ [-180, 180] → return INVALID_COORDINATE without computing distance
    - Implement `POST /geonames/distance` computing Haversine distance rounded to 3 dp
    - On GeoNames API unavailable: fall back to `mock-data/geonames/` local dump (updated ≤ 90 days)
    - Redis cache key `geonames:coord:{postal_code}` (90-day TTL) and `geonames:dist:{hash(A,B)}` (7-day TTL)
    - Rate-limit: Redis counter per hour; backoff on 429
    - In dev/test: always use local dump
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5, 14.6_

  - [x] 22.2 Write property test for GeoNames geodesic invariants (P11)
    - **Property 11: GeoNames adapter geodesic invariants**
    - **Validates: Requirements 14.7, 14.8**
    - Use Hypothesis to generate valid and out-of-bounds coordinate pairs; assert symmetry within 0.001 km, self-distance = 0, and INVALID_COORDINATE on out-of-bounds
    - _Requirements: 14.7, 14.8_

- [x] 23. Implement the MVR Adapter in `services/mvr-adapter/`
  - [x] 23.1 Implement provincial MVR API queries with data minimisation
    - Query provincial MVR API for the claimant's residential province
    - Return only vehicle_ownership_status and vehicle_class (no history, insurance, or financial data)
    - 3-second timeout; return UNAVAILABLE on timeout or error
    - In dev/test: serve from `mock-data/motor-vehicle/` (10 province fixtures)
    - _Requirements: 15.1, 15.2, 15.3, 15.4, 15.5, 15.6_

- [x] 24. Implement the Job Bank Adapter in `services/job-bank-adapter/`
  - [x] 24.1 Implement Job Bank API queries with caching and round-trip serialisation
    - Query Job Bank Canada REST API by NOC code and ESDC region for postings active during claim period
    - Implement `serialise_job_bank_data()` and `parse_job_bank_data()` exactly as specified in the design (Decimal serialised as string)
    - On API unavailable: serve from `mock-data/job-bank/` snapshot (max 7 days old; DATA_STALE flag)
    - Redis cache key `job_bank:postings:{noc}:{region}:{ym}` with 7-day TTL
    - In dev/test: always serve from open data snapshot
    - _Requirements: 16.1, 16.2, 16.3, 16.4_

  - [x] 24.2 Write property test for Job Bank adapter round-trip (P12)
    - **Property 12: Job Bank adapter round-trip**
    - **Validates: Requirements 16.5**
    - Use Hypothesis to generate `JobBankData` instances; assert `parse(serialise(data))` produces identical posting counts and median wage values
    - _Requirements: 16.5_

- [x] 25. Checkpoint — All adapters return canonical responses; mock-data fixtures are in place
  - Run each adapter's unit and property tests. Verify mock-data directory contains all required fixture files (10 MVR, 13 registry, cbsa, geonames, stats-canada, job-bank). Ask the user if questions arise.

---

### Phase 5: Frontend — Claimant Portal and Investigator Dashboard

- [x] 26. Scaffold the React + TypeScript frontend in `services/claimant-portal/`
  - [x] 26.1 Create React/TypeScript project with routing and i18n setup
    - Bootstrap with Vite + React + TypeScript; install `react-router-dom`, `i18next`, `react-i18next`
    - Set up EN/FR translation files; configure language detection from user preference (GCKey profile default)
    - Implement GCKey OAuth 2.0 PKCE flow: redirect to GCKey authorize, handle callback, store HttpOnly session cookie
    - Implement route guards by RBAC role
    - _Requirements: 1.1, 1.9, 20.1, 22.6_

  - [x] 26.2 Implement the claim submission form with WCAG 2.1 Level AA compliance
    - Build multi-field claim submission form: SIN, employer BN, insurable hours, insurable earnings, separation date, reason for separation, ROE file upload
    - Implement client-side validation with accessible, bilingual inline error messages (aria-describedby, role="alert")
    - POST to Claim Intake Service `/api/v1/claims`; display confirmation with Claim_ID or structured error
    - _Requirements: 1.9, 1.10_

  - [x] 26.3 Implement the claim status view for claimants
    - Build `GET /portal/claims/{claim_id}` page showing claim status, Risk_Decision, and LLM explanation in selected language
    - Implement Privacy Act data access request button (calls `/api/v1/privacy/access`)
    - Implement Privacy Act data deletion request button (calls `/api/v1/privacy/delete`)
    - _Requirements: 19.4, 19.5, 22.1_

- [x] 27. Implement the Investigator Dashboard
  - [x] 27.1 Build the flagged claims list with real-time updates
    - Implement `GET /portal/investigator/claims` page; fetch claims with Risk_Decision FLAG_FOR_REVIEW or INVESTIGATE, sorted by Risk_Score descending
    - Connect via Server-Sent Events (SSE) to `GET /portal/events/claims` for real-time updates on new flagged claims
    - Enforce INVESTIGATOR/ADMINISTRATOR role gate; mask PII fields for REVIEWER role
    - _Requirements: 22.1, 22.6_

  - [x] 27.2 Build the claim detail view with SHAP chart and case management
    - Display Risk_Score, Risk_Decision, all Indicators with severity badges, LLM explanation in selected language
    - Render SHAP attribution bar chart using `recharts`; implement aria-label per bar and a hidden data table as screen-reader alternative; provide EN/FR text for chart title and axis labels
    - Implement disposition form: CONFIRMED_FRAUD, FALSE_POSITIVE, NEEDS_FURTHER_REVIEW + case notes textarea
    - On disposition submit: PUT to `/api/v1/claims/{claim_id}/disposition`; publish outcome event to Kafka `outcome.feedback`
    - _Requirements: 22.2, 22.3, 22.4, 22.5_

  - [x] 27.3 Write accessibility tests for the investigator dashboard
    - Use `axe-core` via `jest-axe` to assert WCAG 2.1 Level AA on the claim submission form, SHAP chart component, and flagged claims list
    - Assert SHAP chart has text alternative in both EN and FR
    - _Requirements: 1.10, 22.5_

- [x] 28. Implement the Privacy Act compliance API endpoints in `services/claim-intake/`
  - [x] 28.1 Implement `GET /api/v1/privacy/access` and `DELETE /api/v1/privacy/delete`
    - `GET /api/v1/privacy/access`: query PostgreSQL and ClickHouse for all records tied to the authenticated claimant's SIN; return machine-readable JSON within 30 days SLA
    - `DELETE /api/v1/privacy/delete`: anonymise identifying fields in claims, indicators, and risk_assessments for the given SIN; preserve aggregate/statistical records; complete within 30 days SLA
    - Both endpoints require CLAIMANT role (own data only) or ADMINISTRATOR role
    - _Requirements: 19.4, 19.5_

- [x] 29. Implement GCKey OAuth 2.0 backend token validation and RBAC enforcement
  - [x] 29.1 Implement token introspection and scope validation across all FastAPI services
    - Implement `validate_gckey_token(token)` in `shared/auth.py` using GCKey JWKS endpoint
    - Apply FastAPI dependency to all API routes in all services; return 401 for missing/invalid token, 403 for insufficient scope
    - Implement `require_role(*roles)` dependency used by each service to enforce CLAIMANT, REVIEWER, INVESTIGATOR, ADMINISTRATOR
    - _Requirements: 19.7, 22.6_

- [x] 30. Checkpoint — Claimant Portal loads, submits a synthetic claim, and shows it in the Investigator Dashboard
  - VERIFIED LIVE over `podman-compose` (macOS + Podman 6 / applehv). The full flow was exercised
    end-to-end on the running stack and each hop confirmed:
    intake `POST /api/v1/claims` (202) → `claim.received` (real Kafka) → ROE `POST /roe/rest` (202) →
    `roe.received` → orchestrator pairs claim+ROE by claim_id (Redis) → dispatches to the 3 engines →
    ensemble scorer → `claim.scored` (DETERMINATE **FLAG_FOR_REVIEW, risk_score 44**) → persisted to
    Postgres `risk_assessments`. A run where <3 engines completed correctly yielded **INDETERMINATE**
    (payment halted). Investigator half: `GET /portal/investigator/claims` returns the flagged claim
    with server-side PII masking (Req 22.6); `PUT /api/v1/claims/{id}/disposition` persists to
    `claim_dispositions` attributed to the GCKey subject (append-only, Req 22.3) and publishes
    `outcome.feedback` for CONFIRMED_FRAUD/FALSE_POSITIVE (Req 22.4).
  - COMPOSE: the verified slice — THREE engines (repetitive-pattern, history-inconsistency,
    regional-analysis) + THREE mock-data adapters (cra, stats-canada, job-bank) + postgres+migrations,
    kafka/redis/otel, claim-intake, roe-adapter, pipeline-orchestrator, ensemble-scorer, portal-backend,
    claimant-portal — behind a Caddy single-origin proxy (host 8090) with a local-dev JWKS + signed
    INVESTIGATOR service token (Req 19.7; engines forward SERVICE_TOKEN to the auth-gated adapters).
  - THREE real defects were found ONLY by this live run (integration tests inject events / use loopback):
    roe-adapter missing GCKEY_JWKS_URL; ensemble-scorer missing EI_ADS_PG_DSN (persist was a silent
    no-op); and a `shap_attributions` INSERT/schema mismatch (claim_id/scored_at columns don't exist)
    that rolled back the persist → spurious INDETERMINATE. All fixed (29053b7). Both determinate paths
    are also covered by integration tests (`tests/integration/test_slice1_end_to_end.py`).
  - DONE VIA DEV SHIMS (real GCKey IdP still absent locally): there is no local GCKey identity provider,
    so the browser OAuth LOGIN itself is not walkable. The checkpoint's substance — submit a claim and
    see it reach the investigator dashboard — is demonstrated using local-dev credential shims that run
    the services' REAL auth (nothing bypassed): `scripts/dev/mint-token.py` (GCKey Bearer for
    intake/ROE, validated via JWKS) and `scripts/dev/mint-session-cookie.py` (portal session cookie,
    same HMAC-over-SESSION_SECRET the BFF verifies). A production deployment substitutes a real GCKey
    IdP with no contract change. Working bring-up recipe + compose gotchas: docs/regeneration/STATUS.md
    ("Task 30").

---

### Phase 6: Observability

- [x] 31. Implement Prometheus metrics instrumentation across all services
  - [x] 31.1 Add Prometheus metrics to all Detection Engines and adapters
    - Add `prometheus-fastapi-instrumentator` to each service; expose `/metrics` endpoint
    - Register all metrics defined in the design: `ei_ads_engine_requests_total`, `ei_ads_engine_latency_seconds`, `ei_ads_engine_indicators_generated_total`, `ei_ads_engine_errors_total`
    - Register adapter metrics: `ei_ads_adapter_requests_total`, `ei_ads_adapter_latency_seconds`, `ei_ads_adapter_cache_hits_total`, `ei_ads_adapter_cache_misses_total`, `ei_ads_adapter_error_rate`
    - Register model quality gauges: `ei_ads_model_fraud_detection_rate`, `ei_ads_model_false_positive_rate`, `ei_ads_model_brier_score`, `ei_ads_model_drift_detected`
    - _Requirements: 21.1, 21.2, 21.3_

  - [x] 31.2 Implement `/healthz` endpoints and Grafana alert rules
    - Add `GET /healthz` to each service returning 200/503 based on DB/Kafka/Redis connectivity
    - Write `k8s/grafana/alerts.yaml` defining all four alert rules from the design (p99 > 3 s, adapter error rate > 5%, fraud_detection_rate < 80%, 10% deviation from 30-day baseline)
    - _Requirements: 21.2, 21.3, 21.4, 21.6_

- [x] 32. Implement OpenTelemetry distributed tracing
  - [x] 32.1 Instrument all services with OpenTelemetry spans
    - Add `opentelemetry-sdk`, `opentelemetry-instrumentation-fastapi`, `opentelemetry-instrumentation-asyncpg`, `opentelemetry-instrumentation-aiohttp` to each service
    - Implement root span `claim.received` in the Pipeline Orchestrator; propagate trace context via Kafka headers to all engines
    - Create child spans for each engine call, adapter call, SHAP explanation, and LLM call as specified in the design trace structure
    - Export traces to OpenTelemetry Collector (configured in `podman-compose.yml` and `k8s/otel-collector.yaml`)
    - Ensure no raw PII (SIN, name, address) appears in any span attribute; use `sin_hash` only
    - _Requirements: 21.5_

---

### Phase 7: Integration Tests and Load Tests

- [x] 33. Write end-to-end integration tests
  - [x] 33.1 Implement happy-path and INVESTIGATE-path integration tests
    - Use `testcontainers` to start Kafka, Redis, PostgreSQL, ClickHouse, Neo4j, and all six engine mock servers
    - Test 1 (happy path): submit claim → assert AUTO_APPROVE within 2,000 ms → assert ClickHouse audit record written
    - Test 2 (INVESTIGATE path): submit claim with synthetic fraud signals → assert INVESTIGATE decision → assert `claim.escalated` event on Kafka → assert SIU notification
    - Test 3 (PARTIAL_RESULT): kill 2 engines → assert PARTIAL_RESULT Indicators and scoring still completes
    - Test 4 (INDETERMINATE): kill 4 engines → assert INDETERMINATE decision and payment halted
    - _Requirements: 2.2, 2.4, 2.5, 2.8_

  - [x] 33.2 Implement Kafka event-flow and audit-trail integration tests
    - Assert `claim.received` event is consumed within 100 ms of publication
    - Assert audit log entry written to ClickHouse within 500 ms for each adapter call
    - Assert immutable audit log: attempt UPDATE on `audit_log` → assert permission denied
    - _Requirements: 2.7, 19.3_

  - [x] 33.3 Implement bilingual output integration tests
    - Submit claims that trigger LLM explanation; assert both `explanation_en` and `explanation_fr` are populated and ≤ 500 characters
    - Assert both versions stored in ClickHouse `risk_assessments_analytics`
    - _Requirements: 20.2, 20.4_

- [x] 34. Write Locust load tests in `tests/load/`
  - [x] 34.1 Implement Locust load test for 500 concurrent claims at 2,000 claims/min
    - Write `tests/load/locustfile.py` with a `ClaimUser` class simulating the full claim submission flow
    - Target: p99 latency ≤ 2,000 ms; throughput ≥ 2,000 claims/min; assert no latency degradation
    - Assert Claimant Portal p99 ≤ 3,000 ms at 1,000 concurrent users
    - _Requirements: 17.1, 17.2, 17.3_

  - [x] 34.2 Implement schemathesis contract tests for all OpenAPI endpoints
    - Use `schemathesis` to auto-generate and run property-based API contract tests against all FastAPI services' OpenAPI schemas
    - Assert no 5xx responses on any generated valid input
    - _Requirements: 17.1_

---

### Phase 8: Kubernetes / OpenShift Deployment Manifests

- [x] 35. Build and publish all container images with Podman
  - [x] 35.1 Write a `Containerfile` for every service
    - For each of the 13 services in `services/`, create `services/<name>/Containerfile` using `registry.access.redhat.com/ubi9/python-311` as the base image (or `registry.access.redhat.com/ubi9/nodejs-20` for the React frontend)
    - Stage 1 (builder): copy `pyproject.toml` / `package.json`, install dependencies; Stage 2 (runtime): copy only the installed packages and application source — no dev tools, no secrets
    - Set `USER 1001` (non-root) in the final stage; expose only the required service port; set `ENTRYPOINT` and `CMD` as specified in the design
    - Add `.containerignore` per service to exclude `__pycache__`, `.pytest_cache`, `tests/`, `mock-data/`, `.env*`, and any `*.key` or `*.pem` files
    - _Requirements: 17.5, 19.1_

  - [x] 35.2 Write a `build-images.sh` script in `scripts/`
    - Iterate over all service directories; run `podman build --no-cache -t ei-ads/<service-name>:$(git rev-parse --short HEAD) -f services/<service-name>/Containerfile .` from the monorepo root so all services share the `shared/` package context
    - Tag each image with both the Git SHA and `latest`; fail fast on any build error
    - Verify each built image runs `podman run --rm ei-ads/<service-name>:latest /healthz` successfully before tagging as `latest`
    - _Requirements: 17.5_

  - [x] 35.3 Write a `push-images.sh` script in `scripts/`
    - Login to the internal OpenShift image registry using `podman login` with credentials from environment variables `OCP_REGISTRY`, `OCP_USER`, `OCP_TOKEN`
    - Push all images tagged with the Git SHA to the registry: `podman push ei-ads/<service-name>:<sha> ${OCP_REGISTRY}/ei-ads/<service-name>:<sha>`
    - Tag and push `latest` only when running on the `main` branch (checked via `$GITHUB_REF` or `$CI_COMMIT_BRANCH`)
    - _Requirements: 17.5_

  - [x] 35.4 Add Podman build steps to the CI pipeline in `.github/workflows/ci.yml` (or `.tekton/`)
    - Add a `build-and-push` job that runs `scripts/build-images.sh` followed by `scripts/push-images.sh`
    - Run builds in parallel per service using a matrix strategy; fail the pipeline if any image fails to build or push
    - Cache Podman layer store between runs using the CI cache mechanism to reduce build times
    - For OpenShift-native CI: write equivalent Tekton `Pipeline` and `Task` YAMLs in `.tekton/build-pipeline.yaml` as an alternative to GitHub Actions
    - _Requirements: 17.5_

- [x] 36. Write Kubernetes manifests for all microservices
  - [x] 36.1 Create Deployment and HPA manifests for all 13 services
    - Write `k8s/deployments/` YAML files for all 13 deployments with min/max pod counts from the design
    - Configure HPA on CPU > 70% and Kafka consumer lag > 500 messages
    - Annotate GPU-requiring deployments (fraud-ring, document-forgery, llm-service) with NVIDIA T4 / A100 node selectors
    - Set resource requests/limits; configure liveness and readiness probes pointing to `/healthz`
    - _Requirements: 17.5_

  - [x] 36.2 Create StatefulSet manifests for Neo4j, Redis Cluster, and Kafka
    - Write StatefulSet YAML for `neo4j-cluster` (3 nodes), `redis-cluster` (3 primary + 2 replica), `kafka-cluster` (6 brokers, RF=3, min.insync.replicas=2)
    - Write Service and PodDisruptionBudget YAMLs for each
    - _Requirements: 23.4, 23.5_

  - [x] 36.3 Create Kubernetes Secrets manifests and ConfigMaps
    - Write `k8s/secrets/` templates for all API keys (provincial registry, GeoNames, MVR, Job Bank), mTLS certificates (CRA, CBSA), and GCKey JWKS config
    - Write ConfigMaps for Kafka topic names, Redis key patterns, and engine timeout budgets
    - _Requirements: 19.1, 19.2_

  - [x] 36.4 Create network policies and ingress manifests
    - Write NetworkPolicy YAMLs: restrict CBSA adapter to CBSA Protected B channel only; restrict CRA adapter to GC secure data exchange; deny egress to public internet from all detection engines
    - Write Ingress YAML for Claimant Portal with TLS 1.3 termination and GCKey OAuth 2.0 redirect URIs
    - _Requirements: 13.4, 19.1_

- [ ] 37. Final checkpoint — Full system test against Kubernetes local cluster
  - Deploy all manifests to a local `kind` cluster or OpenShift Local (CRC); build images with `podman-compose build` first; run integration test suite; assert all `/healthz` endpoints return 200; assert end-to-end latency p99 ≤ 2,000 ms under synthetic load. Ask the user if questions arise.

---

### Phase 9: Dependency Security and SBOM

- [x] 38. Establish continuous dependency vulnerability scanning
  - [x] 38.1 Configure `pip-audit` and `safety` as pre-commit hooks and CI gates
    - Install `pip-pip-audit==2.7.3` as a pre-commit hook: `pip-audit --strict --vulnerability-service osv -r requirements.lock` — block commits if any CVE with CVSS ≥ 7.0 is found
    - Install `safety==3.2.11` as a second gate using the Safety DB: `safety check --full-report --policy-file scripts/dependencies/safety-policy.yml`
    - Write `scripts/dependencies/safety-policy.yml` to define the accepted exceptions list (initially empty); any deviation from zero-CVE policy must be documented in `EXCEPTIONS.md` with a GitHub issue reference and a remediation date no more than 30 days out
    - Add both checks to `.github/workflows/ci.yml` as a required `dependency-security` job that runs before any build job; also add to `.tekton/security-scan-task.yaml` for OpenShift-native CI
    - _Requirements: 19.6_

  - [x] 38.2 Configure `npm audit` and `Snyk` for the React frontend
    - Add `npm audit --audit-level=high` as a CI step for `services/claimant-portal`; fail on any HIGH or CRITICAL finding
    - Integrate `snyk test --severity-threshold=high` using the `snyk/actions/node@master` GitHub Action (pinned to a specific SHA, not `master`, to prevent supply-chain attacks on the action itself)
    - Generate `services/claimant-portal/npm-audit-report.json` as a CI artifact on every PR
    - _Requirements: 19.6_

  - [x] 38.3 Set up automated dependency update PRs with Dependabot
    - Write `.github/dependabot.yml` enabling Dependabot for `pip` (root `pyproject.toml`), `npm` (`services/claimant-portal/package.json`), and `github-actions` (`.github/workflows/`)
    - Configure update schedule: weekly, target branch `main`, auto-label with `dependency-update`
    - Set `open-pull-requests-limit: 5` per ecosystem to prevent PR flood
    - All Dependabot PRs must pass the `dependency-security` CI job before merge

- [x] 39. Generate and publish Software Bill of Materials (SBOM)
  - [x] 39.1 Install and configure Syft for per-image SBOM generation
    - Pin `syft==1.18.1` in `scripts/dependencies/python-versions.toml` under `[tool.sbom]`
    - In `scripts/build-images.sh`, after each successful `podman build`, run:
      `syft ei-ads/<service-name>:<sha> -o cyclonedx-json > sbom/<service-name>-<sha>.cyclonedx.json`
    - Also generate SPDX format: `syft ei-ads/<service-name>:<sha> -o spdx-json > sbom/<service-name>-<sha>.spdx.json`
    - Store all SBOM files in `sbom/` at the monorepo root; commit the SBOMs alongside the Git SHA in a `sbom/` directory so every release tag has a corresponding SBOM snapshot
    - _Requirements: 19.6_

  - [x] 39.2 Validate all SBOMs against the OSV vulnerability database using Grype
    - Pin `grype==0.87.0` in the dependency manifest
    - In `scripts/build-images.sh`, after generating each SBOM, run:
      `grype sbom:sbom/<service-name>-<sha>.cyclonedx.json --fail-on high`
    - Grype must report zero HIGH or CRITICAL CVEs before `build-images.sh` tags the image as `latest` or pushes to the registry; a non-zero exit code from Grype aborts the push
    - Save `grype` scan results as `sbom/<service-name>-<sha>-grype-report.json`; publish as CI artifacts so they are available for audit
    - _Requirements: 19.6_

  - [x] 39.3 Embed the SBOM digest into every container image as an OCI annotation
    - Compute the SHA-256 digest of each CycloneDX SBOM file: `sha256sum sbom/<service-name>-<sha>.cyclonedx.json`
    - Re-tag the built image with the SBOM digest as an OCI annotation using:
      `podman image tag` + `podman image inspect` + `skopeo copy --dest-oci-accept-uncompressed-layers` with the `org.opencontainers.image.ref.name`, `org.cyclonedx.sbom.digest`, and `org.cyclonedx.sbom.url` labels
    - This allows OpenShift's image admission controller to verify SBOM provenance at deploy time
    - _Requirements: 19.6_

  - [x] 39.4 Publish SBOMs to the OpenShift internal registry and artifact store
    - Push each CycloneDX SBOM JSON as a separate OCI artifact using `oras push`:
      `oras push ${OCP_REGISTRY}/ei-ads/<service-name>-sbom:<sha> sbom/<service-name>-<sha>.cyclonedx.json:application/vnd.cyclonedx+json`
    - Also publish to `sbom/` in the project's artifact repository (Nexus or Artifactory); include a `sbom/manifest.json` index file listing all SBOMs by service name, Git SHA, image digest, and generation timestamp
    - Write `scripts/publish-sboms.sh` that performs both the OCI artifact push and the artifact repository upload; call this script from `scripts/push-images.sh` after each successful image push
    - _Requirements: 19.6_

  - [x] 39.5 Generate a merged aggregate SBOM for the entire EI-ADS release
    - After all per-service SBOMs are generated, run:
      `syft --merge sbom/*.cyclonedx.json -o cyclonedx-json > sbom/ei-ads-<sha>-aggregate.cyclonedx.json`
    - Run Grype against the aggregate SBOM: `grype sbom:sbom/ei-ads-<sha>-aggregate.cyclonedx.json --fail-on high`
    - The aggregate SBOM is the authoritative supply-chain artifact submitted with the Government of Canada Security Assessment and Authorisation (SA&A) package; store it in `docs/security/sbom/` and reference it in the Privacy Impact Assessment document
    - Add a `README` to `sbom/` explaining the SBOM lifecycle, format choices (CycloneDX 1.6 + SPDX 2.3), tooling versions, and how to re-generate SBOMs for a given Git SHA
    - _Requirements: 19.6, 19.8_

- [x] 40. Final security gate — Zero-CVE sign-off before OpenShift deployment
  - [x] 40.1 Run all security scans in sequence and produce a deployment readiness report
    - Execute in order: (1) `pip-audit` on all Python lock files, (2) `npm audit` on the frontend, (3) Grype on all per-service SBOMs, (4) Grype on the aggregate SBOM
    - Write `scripts/security-gate.sh` that runs all four checks and produces `docs/security/deployment-readiness-<sha>.md` summarising: total packages scanned, zero CVEs confirmed (or documented exceptions with issue references), SBOM digest per service, and sign-off date
    - This report is a mandatory pre-condition for the task 37 final deployment checkpoint — the checkpoint SHALL NOT proceed if `security-gate.sh` exits non-zero
    - _Requirements: 19.6_

---

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP; all property tests and most unit tests are optional
- All mandatory implementation tasks (no `*`) must be completed for a deployable system
- The design uses Python throughout: FastAPI, asyncio, Hypothesis, XGBoost, PyTorch Geometric, Prophet, LangChain
- All 15 correctness properties from the design are covered by property-based tests (P1–P15)
- Hypothesis is configured for CI with `max_examples=200` and `settings.register_profile("ci", ...)`
- Checkpoints at tasks 3, 10, 17, 25, 30, 37 provide incremental validation gates; task 40 is the mandatory security gate before deployment
- All external adapter tests use mock-data fixtures; no production API calls during CI
- All library versions are pinned exactly (no ranges); the canonical version list lives in `scripts/dependencies/python-versions.toml` and `scripts/dependencies/frontend-versions.json`; Dependabot keeps them current
- SBOM format: CycloneDX 1.6 JSON (primary) + SPDX 2.3 JSON (secondary); generated by Syft 1.18.1; validated by Grype 0.87.0 against the OSV database; zero HIGH/CRITICAL CVEs required before any image is pushed

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "1.3", "1.4"] },
    { "id": 1, "tasks": ["2.1", "2.2", "2.3", "2.4"] },
    { "id": 2, "tasks": ["4.1", "5.1", "7.1", "8.1", "9.1"] },
    { "id": 3, "tasks": ["4.2", "5.2", "7.2", "8.2", "9.2"] },
    { "id": 4, "tasks": ["4.3", "4.4", "7.3", "7.4", "7.5", "8.3", "9.3"] },
    { "id": 5, "tasks": ["6.1"] },
    { "id": 6, "tasks": ["6.2", "6.3"] },
    { "id": 7, "tasks": ["6.4", "18.1", "19.1", "20.1", "21.1", "22.1", "23.1", "24.1"] },
    { "id": 8, "tasks": ["11.1", "12.1", "13.1", "14.1", "15.1", "16.1", "18.2", "20.2", "22.2", "24.2"] },
    { "id": 9, "tasks": ["11.2", "12.2", "13.2", "14.2", "15.2", "16.2"] },
    { "id": 10, "tasks": ["11.3", "11.4", "12.3", "12.4", "13.3", "13.4", "14.3", "14.4", "15.3", "16.3", "16.4"] },
    { "id": 11, "tasks": ["15.4", "15.5", "15.6", "16.5", "26.1"] },
    { "id": 12, "tasks": ["26.2", "26.3", "27.1", "29.1"] },
    { "id": 13, "tasks": ["27.2", "28.1"] },
    { "id": 14, "tasks": ["27.3", "31.1", "32.1"] },
    { "id": 15, "tasks": ["31.2"] },
    { "id": 16, "tasks": ["33.1", "33.2", "33.3", "34.2", "35.1", "35.2"] },
    { "id": 17, "tasks": ["34.1", "35.3", "35.4", "36.1", "36.2"] },
    { "id": 18, "tasks": ["36.3", "36.4"] },
    { "id": 19, "tasks": ["38.1", "38.2", "38.3"] },
    { "id": 20, "tasks": ["39.1", "39.2", "39.3"] },
    { "id": 21, "tasks": ["39.4", "39.5", "40.1"] }
  ]
}
```
