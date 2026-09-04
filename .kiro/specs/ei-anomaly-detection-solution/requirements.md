# Requirements Document

## Introduction

The AI-Enabled Employment Insurance Anomaly Detection Solution (EI-ADS) is a real-time fraud detection platform for the Government of Canada's Employment Insurance (EI) program. EI-ADS processes every new claim submission through six parallel AI detection engines, cross-references eight external data sources, produces an explainable risk score, and routes each claim to the appropriate decision pathway — auto-approve, flag for human review, or escalate for investigation — within two seconds of claim receipt.

The system must comply with the Government of Canada's Protected B data classification, the _Privacy Act_, and _PIPEDA_, and must deliver all investigator-facing outputs in both English and French. Target outcomes are an 85%+ fraud detection rate, a false positive rate below 5%, and annual savings of $100M+.

---

## Glossary

- **EI-ADS**: The AI-Enabled Employment Insurance Anomaly Detection Solution — the system being specified.
- **Claim**: A claimant's application for Employment Insurance benefits, accompanied by a Record of Employment from the employer.
- **Claimant**: An individual who submits a claim for EI benefits.
- **ROE**: Record of Employment — an official Service Canada document issued by an employer that records insurable hours and earnings for a separation period.
- **SIN**: Social Insurance Number — the claimant's unique government identifier.
- **NOC**: National Occupational Classification code — the standardized Canadian occupational taxonomy used to classify job titles.
- **Detection_Engine**: The EI-ADS subsystem that runs one or more AI algorithms against a single claim and produces a partial risk score and set of indicators for that detection domain.
- **Risk_Score**: A numeric value in the range [0, 100] representing the aggregated probability of fraud for a given claim. Higher values indicate higher fraud risk.
- **Risk_Decision**: The routing outcome assigned to a claim based on its Risk_Score: AUTO_APPROVE (score < 30), FLAG_FOR_REVIEW (score 30–69), or INVESTIGATE (score ≥ 70).
- **Indicator**: A structured record produced by a Detection_Engine that identifies one anomalous signal, its severity (LOW, MEDIUM, HIGH, CRITICAL), the algorithm that produced it, and a natural-language explanation.
- **Ensemble_Scorer**: The EI-ADS component that aggregates Indicators from all Detection_Engines into a single Risk_Score using XGBoost and Isolation Forest models with SHAP explainability.
- **Claim_Intake_Service**: The EI-ADS component that receives claim submissions via the Claimant_Portal and ROE_Adapter, validates inputs, and routes claims to the Detection_Pipeline.
- **Detection_Pipeline**: The orchestrated sequence of Detection_Engines and the Ensemble_Scorer that processes a single claim end-to-end.
- **Claimant_Portal**: The React/FastAPI web application through which claimants submit EI applications and through which investigators review flagged claims.
- **ROE_Adapter**: The EI-ADS integration component that normalises Records of Employment received from ROE Web via REST or SOAP into the EI-ADS canonical claim model.
- **External_Data_Adapter**: A per-source integration component that queries an external data source and normalises the response into the EI-ADS canonical data model.
- **LLM_Service**: The EI-ADS component that wraps the Llama 3.1 large language model via a LangChain RAG pipeline and provides narrative analysis capabilities to Detection_Engines.
- **Graph_Service**: The EI-ADS component that maintains the claimant-employer graph in Neo4j and exposes community detection and link analysis for the Fraud_Ring_Engine.
- **Kafka_Bus**: The Apache Kafka event streaming backbone over which Detection_Engines receive claim events and publish Indicator events.
- **Redis_Cache**: The Redis in-memory cache used to store External_Data_Adapter responses during the lifetime of a single claim's detection run.
- **Stats_Canada_Source**: The Statistics Canada Labour Force Survey REST API providing regional unemployment rates and labour market data.
- **CRA_Source**: The Canada Revenue Agency tax data exchange providing T4 slips, payroll records, and NOC employer category data.
- **Provincial_Registry_Source**: Provincial corporate and business registry APIs providing registered business information for all Canadian provinces and territories.
- **CBSA_Source**: The Canada Border Services Agency API providing cross-border travel records for claimants.
- **GeoNames_Source**: The GeoNames REST API providing geographic coordinates, postal codes, distance calculations, and transit route data.
- **Business_Registry_Source**: The Corporations Canada API providing federal corporate registration records.
- **MVR_Source**: Provincial Motor Vehicle Registry APIs providing vehicle ownership records linked to SIN or provincial identifier.
- **Job_Bank_Source**: The Job Bank Canada REST API providing active job postings by NOC code and economic region.
- **Protected_B**: The Government of Canada data sensitivity classification for information whose compromise could cause serious injury — applies to all personal and financial data in EI-ADS.
- **GCKey**: The Government of Canada federated identity service used for claimant authentication.
- **SHAP**: SHapley Additive exPlanations — the model interpretability method used to attribute Risk_Score contributions to individual features.
- **Bilingual_Output**: Any user-facing text, label, or message rendered in both English and French as required by the _Official Languages Act_.
- **Investigation_Unit**: The ESDC Special Investigation Unit (SIU) that receives escalated high-risk claims.

---

## Requirements

---

### Requirement 1: Claim Intake and Validation

**User Story:** As a claimant, I want to submit my EI application through a secure portal, so that my claim is received, validated, and processed without delay.

#### Acceptance Criteria

1. THE Claimant_Portal SHALL accept EI claim submissions authenticated via GCKey OAuth 2.0.
2. WHEN a claimant submits a claim, THE Claim_Intake_Service SHALL validate that all mandatory fields — SIN, employer Business Number, insurable hours, insurable earnings, separation date, and reason for separation — are present and structurally well-formed within 200 milliseconds of receipt.
3. IF a mandatory field is absent or structurally invalid, THEN THE Claim_Intake_Service SHALL return a structured error response identifying each invalid field by name and providing a correction hint per field, without storing the incomplete record.
4. WHEN a structurally valid claim is received, THE Claim_Intake_Service SHALL assign a unique Claim_ID, persist the claim to the PostgreSQL transactional store with a status of RECEIVED, and publish a `claim.received` event to the Kafka_Bus within 500 milliseconds.
5. IF the Claim_Intake_Service fails to persist the claim or publish the `claim.received` event within 500 milliseconds, THEN no Claim_ID SHALL be assigned, the data SHALL remain unpersisted, and the Claim_Intake_Service SHALL return a structured error response to the claimant.
6. THE Claim_Intake_Service SHALL accept ROE documents from employers via the ROE_Adapter using either REST or SOAP protocols as supported by ROE Web.
7. WHEN the ROE_Adapter receives a ROE document, THE ROE_Adapter SHALL normalise the document into the EI-ADS canonical claim model and publish a `roe.received` event to the Kafka_Bus within 500 milliseconds.
8. IF the ROE_Adapter fails to normalise a ROE document or publish the `roe.received` event, THEN THE ROE_Adapter SHALL return a structured error response to the submitting employer and SHALL NOT publish a partial event to the Kafka_Bus.
9. THE Claimant_Portal SHALL render all user-facing labels, instructions, and error messages as Bilingual_Output.
10. THE Claimant_Portal SHALL satisfy WCAG 2.1 Level AA accessibility criteria for all interactive components.
11. THE Claim_Intake_Service SHALL classify all claim records as Protected_B and encrypt data at rest using AES-256 and in transit using TLS 1.3.

---

### Requirement 2: Detection Pipeline Orchestration

**User Story:** As an investigator, I want all detection algorithms to run automatically on every claim, so that no fraud signal is missed due to incomplete analysis.

#### Acceptance Criteria

1. WHEN a `claim.received` event and its associated `roe.received` event are both present on the Kafka_Bus, measured from the later of the two event timestamps, THE Detection_Pipeline SHALL initiate parallel execution of all six Detection_Engines within 100 milliseconds.
2. THE Detection_Pipeline SHALL complete all Detection_Engine runs, aggregate Indicators through the Ensemble_Scorer, produce a final Risk_Score and Risk_Decision, and publish the result to the Kafka_Bus within 2,000 milliseconds of the `claim.received` event timestamp.
3. THE Detection_Pipeline SHALL execute all six Detection_Engines concurrently and SHALL NOT execute them sequentially.
4. IF a Detection_Engine fails to produce a result within its allocated time budget, THEN THE Detection_Pipeline SHALL record a PARTIAL_RESULT indicator including the engine identifier and elapsed time for that engine and proceed to Ensemble_Scorer aggregation with the available results.
5. IF fewer than three Detection_Engines produce a result within their allocated time budgets, THEN THE Detection_Pipeline SHALL assign a Risk_Decision of INDETERMINATE, halt payment processing for the claim, and queue the claim for manual review.
6. THE Detection_Pipeline SHALL store each claim's final Risk_Score, Risk_Decision, all Indicators, and SHAP feature attributions in ClickHouse for a minimum of 7 years for audit and analytical purposes.
7. THE Detection_Pipeline SHALL log a complete, immutable audit trail — including all External_Data_Adapter calls, their responses, and the intermediate outputs of each Detection_Engine — for every processed claim, and each log entry SHALL be written within 500 milliseconds of the event it records.
8. WHEN a claim's Risk_Decision is INVESTIGATE, THE Detection_Pipeline SHALL publish a `claim.escalated` event to the Kafka_Bus and notify the Investigation_Unit via secure government messaging within 500 milliseconds of the Risk_Decision being produced.

---

### Requirement 3: Repetitive Claim Pattern Detection

**User Story:** As an investigator, I want the system to detect claimants who repeatedly file EI claims in a coordinated pattern with the same employer, so that systematic benefit gaming is identified before payment is issued.

#### Acceptance Criteria

1. WHEN the Repetitive_Pattern_Engine processes a claim, THE Repetitive_Pattern_Engine SHALL retrieve all prior EI claims for the same SIN from the EI claim history database covering a rolling 60-month lookback window.
2. IF the EI claim history database is unavailable, THEN THE Repetitive_Pattern_Engine SHALL produce a DATA_UNAVAILABLE Indicator and publish it to the Kafka_Bus, and SHALL NOT attempt pattern analysis without historical data.
3. WHEN three or more claims are found within a 36-month window from the same employer identified by employer registration number, THE Repetitive_Pattern_Engine SHALL generate a HIGH-severity Indicator with the claim dates, employer name, and inter-claim interval.
4. WHEN a claim's insurable hours are within 5 hours of the minimum qualifying threshold anchored to the minimum qualifying threshold for the claimant's economic region and the same pattern appears in two or more prior claims from the same SIN, THE Repetitive_Pattern_Engine SHALL generate a HIGH-severity Indicator describing the hours-clustering pattern.
5. WHEN the LLM_Service analyses the reason-for-separation narratives across two or more claims from the same SIN, THE LLM_Service SHALL classify narratives with a cosine similarity above 0.92 as verbatim-equivalent and THE Repetitive_Pattern_Engine SHALL generate a MEDIUM-severity Indicator and SHALL include the cosine similarity score in the Indicator payload.
6. IF the LLM_Service does not return a narrative classification within 5 seconds, THE Repetitive_Pattern_Engine SHALL record a NARRATIVE_ANALYSIS_TIMEOUT flag in the Indicator and proceed without the narrative result.
7. THE Repetitive_Pattern_Engine SHALL complete its analysis and publish all Indicators to the Kafka_Bus within 400 milliseconds, excluding any LLM_Service wait time, of receiving the `claim.received` event.
8. FOR ALL claim sequences of length N ≥ 3 drawn from the same SIN, THE Repetitive_Pattern_Engine SHALL detect at least one pattern Indicator when the inter-claim intervals are within ±14 calendar days of each other across all consecutive pairs (sequential pattern invariant).

---

### Requirement 4: Geographic Anomaly Detection

**User Story:** As an investigator, I want the system to flag claims where the claimant's residence is geographically implausible relative to the declared place of employment, so that ghost employment arrangements are detected.

#### Acceptance Criteria

1. WHEN the Geographic_Anomaly_Engine processes a claim, THE Geographic_Anomaly_Engine SHALL compute the geodesic distance between the claimant's registered residential address and the employer's declared work location using the GeoNames_Source to a precision of 4 decimal places.
2. IF the GeoNames_Source cannot resolve the claimant's address or the employer's work location to coordinates, THEN THE Geographic_Anomaly_Engine SHALL generate a MEDIUM-severity Indicator noting the unresolvable address and SHALL skip distance-dependent analysis for that claim.
3. WHEN the computed distance exceeds 100 kilometres and the claimant has no vehicle registered in any provincial MVR_Source, THE Geographic_Anomaly_Engine SHALL generate a HIGH-severity Indicator.
4. WHEN the computed distance exceeds 100 kilometres and no public transit route connecting the two locations exists in the GeoNames_Source, THE Geographic_Anomaly_Engine SHALL generate a HIGH-severity Indicator.
5. WHEN the estimated weekly cost of the lowest-cost available transportation between the claimant's residence and work location exceeds the claimant's weekly EI benefit rate, THE Geographic_Anomaly_Engine SHALL generate a CRITICAL-severity Indicator stating the weekly transportation cost in CAD (to 2 decimal places), the weekly EI benefit rate, and the cost-to-benefit ratio.
6. IF transportation cost data is unavailable from all sources, THEN THE Geographic_Anomaly_Engine SHALL generate a MEDIUM-severity Indicator noting data unavailability and SHALL NOT produce the CRITICAL economic implausibility Indicator.
7. WHEN the claimant's two most recent prior employers are also located more than 100 kilometres from the claimant's residential address, THE Geographic_Anomaly_Engine SHALL include in the Indicator the employer names, distances from residence, and employment period dates for both prior employers.
8. IF fewer than 2 prior employers exist in the claimant's history, THEN THE Geographic_Anomaly_Engine SHALL omit the historical employer pattern analysis without generating an Indicator.
9. THE Geographic_Anomaly_Engine SHALL complete its analysis and publish all Indicators to the Kafka_Bus within 600 milliseconds, measured from the `claim.received` event timestamp to the final Indicator published timestamp.
10. FOR ALL pairs of (claimant_address, work_location) coordinates, THE Geographic_Anomaly_Engine SHALL return a distance value D such that D is symmetric — distance(A, B) equals distance(B, A) — and D is non-negative (distance invariant).

---

### Requirement 5: Employer Fraud Ring Detection

**User Story:** As an investigator, I want the system to identify shell employers and coordinated multi-claimant fraud rings, so that organised fraud can be escalated to the Special Investigation Unit before any payments are issued.

#### Acceptance Criteria

1. WHEN the Fraud_Ring_Engine processes a claim, THE Graph_Service SHALL retrieve or construct the bipartite graph connecting all claimants and employers linked within the prior 24-month window for the employer's Business Number, with each graph edge recording claimant node, employer node, separation date, and ROE submission timestamp.
2. WHEN the employer's business was registered within 12 months of the claim's separation date and the employer's registered address is a residential address per the Business_Registry_Source or Provincial_Registry_Source, THE Fraud_Ring_Engine SHALL generate a CRITICAL-severity Indicator. The Indicator SHALL include the employer Business Number, registration date, separation date, and the address classification source.
3. IF the Business_Registry_Source or Provincial_Registry_Source returns no record for the employer Business Number, THEN THE Fraud_Ring_Engine SHALL generate a HIGH-severity Indicator noting that no registration record was found.
4. WHEN 80% or more of an employer's employees within the 90-day period preceding the claim's separation date were separated on the same calendar date and all associated ROEs were submitted within any rolling 24-hour window, THE Fraud_Ring_Engine SHALL generate a CRITICAL-severity Indicator. The Indicator SHALL include the separation date, total employee count, and the ROE submission timestamp range.
5. WHEN the Graph_Service's community detection algorithm identifies that 40% or more of an employer's claimants in the current claim cohort share the first 3 characters of their postal code or a declared surname, THE Fraud_Ring_Engine SHALL generate a CRITICAL-severity Indicator identifying the connected claimant cluster. The Indicator SHALL identify the matched attribute type, matched value, and the list of claimant identifiers in the cluster.
6. WHEN all claimants in a cohort associated with a single employer have insurable hours within a 10-hour band at or above the minimum qualifying threshold for the active benefit period configuration, THE Fraud_Ring_Engine SHALL generate a HIGH-severity Indicator. The Indicator SHALL state the hour band range and the threshold value.
7. IF the claim cohort associated with a single employer contains fewer than 3 claimants, THE Fraud_Ring_Engine SHALL record a NOT_EVALUATED disposition for the cohort-based hours-band check.
8. THE Fraud_Ring_Engine SHALL complete its analysis and publish all Indicators to the Kafka_Bus within 800 milliseconds, measured from the `claim.received` event timestamp to the Kafka acknowledgement timestamp.
9. IF the Fraud_Ring_Engine does not complete within 800 milliseconds, THE Detection_Pipeline SHALL emit a latency-breach alert and SHALL use the partial results computed up to that point without restarting computation.
10. FOR ALL employer graphs G with N claimant nodes, the community detection algorithm SHALL return a partition of N nodes into communities such that every node belongs to exactly one community and no node is omitted, where the sum of all community member counts equals N (community partition completeness invariant).

---

### Requirement 6: Employment History Inconsistency Detection

**User Story:** As an investigator, I want the system to cross-reference ROE declarations against CRA tax records, so that earnings misrepresentations and concurrent employment are detected.

#### Acceptance Criteria

1. WHEN the History_Inconsistency_Engine processes a claim, THE History_Inconsistency_Engine SHALL retrieve the claimant's most recent T4 slip from the CRA_Source for the corresponding tax year and compare the declared insurable earnings on the ROE to the T4 employment income for the same employer identified by matching Business Number.
2. IF the CRA_Source is unreachable or returns no T4 record for the employer and tax year within 5 seconds, THEN THE History_Inconsistency_Engine SHALL produce an UNRESOLVED-severity Indicator and SHALL proceed without T4 cross-reference analysis.
3. IF the ROE insurable earnings or T4 employment income fields are absent or non-numeric, THEN THE History_Inconsistency_Engine SHALL generate an UNRESOLVED-severity Indicator identifying the missing field and SHALL NOT compute a discrepancy value.
4. WHEN the absolute difference between ROE insurable earnings and CRA T4 employment income for the same employer and period exceeds $2,000, THE History_Inconsistency_Engine SHALL generate a CRITICAL-severity Indicator stating the discrepancy amount in Canadian dollars.
5. WHEN the ROE's declared NOC code does not match the employer industry category on file with the CRA_Source, THE History_Inconsistency_Engine SHALL generate a HIGH-severity Indicator citing both the declared and actual classifications.
6. WHEN the LLM_Service analyses the ROE's reason-for-separation narrative against CRA employment records showing continued employment for the same SIN at the same employer during the claimed separation period, THE LLM_Service SHALL classify the narrative as contradicted and THE History_Inconsistency_Engine SHALL generate a CRITICAL-severity Indicator.
7. IF the LLM_Service does not return a narrative classification within 10 seconds, THE History_Inconsistency_Engine SHALL record a NARRATIVE_ANALYSIS_TIMEOUT flag in the Indicator and proceed without the narrative result.
8. WHEN the CRA_Source or provincial employment records indicate concurrent employment for the claimant at a second employer during any portion of the claim period, THE History_Inconsistency_Engine SHALL generate a CRITICAL-severity Indicator identifying the overlapping employer and period expressed as a start date, end date in days, and the overlapping employer identified by Business Number.
9. THE History_Inconsistency_Engine SHALL complete its analysis and publish all Indicators to the Kafka_Bus within 500 milliseconds, excluding LLM_Service wait time, of receiving the `claim.received` event.
10. FOR ALL (ROE_earnings, T4_earnings) pairs where the two values are identical, THE History_Inconsistency_Engine SHALL produce a discrepancy value of exactly 0 and SHALL NOT generate a discrepancy Indicator (zero-discrepancy invariant).

---

### Requirement 7: Document Forgery Detection

**User Story:** As an investigator, I want the system to detect forged or altered ROE documents, so that fabricated employment records are identified before benefits are paid.

#### Acceptance Criteria

1. WHEN the Document_Forgery_Engine processes a submitted ROE document, THE Document_Forgery_Engine SHALL extract all structured fields using LayoutLMv3 OCR and validate that all 11 mandatory ROE fields are present and internally consistent: employer name, employer Business Number, employee name, employee SIN, first day worked, last day paid, total insurable hours, total insurable earnings, reason for issuance, issue date, and authorised signatory.
2. IF any two internally consistent fields produce a mathematical contradiction — specifically: (a) total insurable hours × declared hourly rate deviates from total insurable earnings by more than 2%, or (b) last day paid is earlier than first day worked — THEN THE Document_Forgery_Engine SHALL generate a CRITICAL-severity Indicator identifying the inconsistent fields and their values.
3. WHEN the PDF metadata creation timestamp of a submitted ROE document post-dates the ROE's issue date field by more than 24 hours, THE Document_Forgery_Engine SHALL generate a CRITICAL-severity Indicator specifying both timestamps.
4. WHEN the font family or point size used in a submitted ROE document differs from the font profile established from the employer's prior authenticated ROE documents in the EI-ADS employer document registry, THE Document_Forgery_Engine SHALL generate a CRITICAL-severity Indicator identifying the expected font family and point size and the observed font family and point size.
5. WHEN the digital signature embedded in a submitted ROE document does not match the employer's registered certificate hash in the EI-ADS employer signature registry, THE Document_Forgery_Engine SHALL generate a CRITICAL-severity Indicator.
6. IF a submitted ROE document contains no embedded digital signature, THEN THE Document_Forgery_Engine SHALL generate a CRITICAL-severity Indicator noting the absent signature.
7. WHEN a submitted ROE document identifies an employee by SIN and that SIN does not appear in the employer's payroll records as returned by the CRA_Source, THE Document_Forgery_Engine SHALL generate a CRITICAL-severity Indicator.
8. WHEN the LLM_Service analyses the termination narrative in a submitted ROE and identifies language that is statistically inconsistent with the employer's historical ROE communications style based on a minimum corpus of 5 prior authenticated ROEs from the same employer, THE Document_Forgery_Engine SHALL generate a MEDIUM-severity Indicator.
9. IF the employer has fewer than 5 prior authenticated ROEs in the EI-ADS registry, THEN THE Document_Forgery_Engine SHALL skip the LLM style analysis and SHALL NOT generate a style-based Indicator.
10. THE Document_Forgery_Engine SHALL complete its analysis and publish all Indicators to the Kafka_Bus within 700 milliseconds, measured from the `claim.received` event receipt timestamp to the final Indicator published timestamp.
11. IF the Document_Forgery_Engine does not complete within 700 milliseconds, THE Detection_Pipeline SHALL emit a PROCESSING_TIMEOUT Indicator for this engine.
12. FOR ALL ROE documents D that are identical byte-for-byte to a previously authenticated ROE in the employer document registry, THE Document_Forgery_Engine SHALL produce zero forgery Indicators (authenticated document invariant).
13. THE Document_Forgery_Engine SHALL implement round-trip OCR fidelity: FOR ALL ROE documents D, parsing the document to structured fields and then rendering those fields back to a canonical ROE text representation and re-parsing SHALL produce a structured field set with exact value matches for all numeric and date fields and at most 1 character difference per free-text field of 20 or more characters (OCR round-trip property).

---

### Requirement 8: Regional Employment Rate Analysis

**User Story:** As an investigator, I want the system to compare a claim against current regional labour market conditions, so that claims filed during periods of high local job availability are flagged for additional scrutiny.

#### Acceptance Criteria

1. WHEN the Regional_Analysis_Engine processes a claim, THE Regional_Analysis_Engine SHALL retrieve the regional unemployment rate for the calendar month immediately preceding the claim start date for the claimant's ESDC economic region from the Stats_Canada_Source.
2. IF the Stats_Canada_Source is unavailable or does not return data within 5 seconds, THEN THE Regional_Analysis_Engine SHALL annotate all Indicators from this engine with DATA_UNAVAILABLE and SHALL proceed with remaining analysis that does not depend on regional unemployment data.
3. WHEN the regional unemployment rate for the claimant's economic region is more than 1.5 percentage points below the national average for the same reference month, THE Regional_Analysis_Engine SHALL generate a MEDIUM-severity Indicator stating the regional rate, national rate, and calculated deviation in percentage points.
4. WHEN the Job_Bank_Source reports more than 200 job postings whose closing date falls within or overlaps the claim period for the claimant's NOC code within the claimant's economic region during the claim period, THE Regional_Analysis_Engine SHALL generate a HIGH-severity Indicator stating the number of active postings.
5. IF the Job_Bank_Source is unavailable or does not return data within 5 seconds, THEN THE Regional_Analysis_Engine SHALL annotate all Job_Bank-dependent Indicators with DATA_UNAVAILABLE.
6. WHEN the average advertised wage for the claimant's NOC code in the region as reported by the Job_Bank_Source exceeds the claimant's insurable hourly earnings from the claimant's last completed pay period, calculated as total insurable earnings divided by total insurable hours, by more than 15%, THE Regional_Analysis_Engine SHALL generate a HIGH-severity Indicator.
7. WHEN the claim period falls within months where the sector employment index exceeds the 12-month rolling average for that sector by more than 10% for the claimant's industry sector as determined by the EI-ADS seasonal employment pattern database, THE Regional_Analysis_Engine SHALL include a seasonality annotation in all Indicators produced by this engine.
8. WHEN the claimant has filed EI claims in the same calendar quarter in two or more of the three prior calendar years, THE Regional_Analysis_Engine SHALL generate a MEDIUM-severity Indicator describing the recurring seasonal claim cycle, identifying the matching years and quarters in the Indicator.
9. THE Regional_Analysis_Engine SHALL complete its analysis and publish all Indicators to the Kafka_Bus within 500 milliseconds, measured from the `claim.received` timestamp to the last Indicator published timestamp.
10. FOR ALL (regional_rate, national_rate) pairs where regional_rate equals national_rate, THE Regional_Analysis_Engine SHALL produce a deviation value of exactly 0 and SHALL NOT generate a regional unemployment Indicator (zero-deviation invariant).
11. FOR ALL time series of regional unemployment rates with N monthly observations, THE Regional_Analysis_Engine's time-series anomaly model SHALL produce a forecast for month N+1 that falls within the 95% prediction interval established by the model (time-series forecast calibration property).

---

### Requirement 9: Risk Scoring and Explainability

**User Story:** As an investigator, I want each claim to receive a single explainable risk score with natural-language reasons in English and French, so that I can understand why a claim was flagged and prioritise my review workload efficiently.

#### Acceptance Criteria

1. WHEN the Ensemble_Scorer receives the set of Indicators from all Detection_Engines, THE Ensemble_Scorer SHALL compute a Risk_Score as an integer in the range [0, 100] using XGBoost and Isolation Forest ensemble models with SHAP feature attribution.
2. THE Ensemble_Scorer SHALL assign Risk_Decisions as follows: Risk_Score in [0, 29] maps to AUTO_APPROVE; Risk_Score in [30, 69] maps to FLAG_FOR_REVIEW; Risk_Score in [70, 100] maps to INVESTIGATE.
3. WHEN the Ensemble_Scorer computes a Risk_Score, THE Ensemble_Scorer SHALL produce a SHAP attribution vector that assigns a numeric contribution value to each Indicator, such that the sum of all SHAP contributions equals the Risk_Score minus the model's base value (SHAP additivity invariant).
4. WHEN a Risk_Decision is assigned, THE LLM_Service SHALL generate a natural-language explanation of no more than 500 characters per language, citing up to the top three Indicators ranked by absolute SHAP contribution value in descending order, with ties broken by Indicator arrival order, in both English and French.
5. WHEN a claim is assigned the AUTO_APPROVE decision, THE Ensemble_Scorer SHALL include in the explanation the three Indicators with the largest negative SHAP contributions, or all available Indicators if fewer than three exist.
6. WHEN the Ensemble_Scorer receives all Detection_Engine Indicator events for a claim, THE Ensemble_Scorer SHALL complete Risk_Score computation and publish the result within 300 milliseconds.
7. IF all Detection_Engines return zero Indicators for a claim, THEN THE Ensemble_Scorer SHALL produce a Risk_Score of 0 and a Risk_Decision of AUTO_APPROVE.
8. THE Ensemble_Scorer SHALL produce Risk_Score values such that for any two claims where one claim's Indicators are a strict superset of the other's, the claim with more Indicators receives a Risk_Score greater than or equal to the claim with fewer Indicators (monotonicity property).
9. IF the LLM_Service fails to generate an explanation within 5 seconds of Risk_Decision assignment, THEN THE Ensemble_Scorer SHALL publish the Risk_Score and Risk_Decision without an explanation, and SHALL include an indication that the explanation is unavailable.

---

### Requirement 10: External Data Source Integration — Statistics Canada

**User Story:** As a developer, I want the Stats_Canada_Source integration to provide reliable regional labour force data, so that the Regional_Analysis_Engine has accurate inputs regardless of the production API's availability.

#### Acceptance Criteria

1. THE Stats_Canada_Adapter SHALL query the Statistics Canada Labour Force Survey REST API at `https://www150.statcan.gc.ca/t1/tbl1/en/dtbl/download/98100424.json` to retrieve monthly unemployment rates by ESDC economic region, with a 10-second connection timeout and 30-second read timeout.
2. WHEN the Stats_Canada_Source API returns an HTTP 5xx error, connection timeout, or connection refusal, THE Stats_Canada_Adapter SHALL fall back to cached data in Redis_Cache with a maximum age of 30 days and SHALL include a `DATA_STALE` flag in the response.
3. IF the Redis_Cache is empty or the cached data is older than 30 days, THEN THE Stats_Canada_Adapter SHALL return a DATA_UNAVAILABLE response and SHALL NOT serve stale data beyond the 30-day threshold.
4. THE Stats_Canada_Adapter SHALL refresh its Redis_Cache on the first calendar day of each month between 00:00 and 06:00 UTC.
5. WHERE the deployment environment is development or test, THE Stats_Canada_Adapter SHALL serve data from JSON fixture files in `mock-data/stats-canada/` rather than calling the production API.
6. THE Stats_Canada_Adapter SHALL parse the Statistics Canada JSON response into the EI-ADS canonical LabourForceData model, retaining values to no fewer than 4 decimal places.
7. THE Stats_Canada_Adapter SHALL produce a JSON response from the canonical LabourForceData model that covers all ESDC economic regions present in the original API response and preserves unemployment rate values to no fewer than 4 decimal places (round-trip parse property).

---

### Requirement 11: External Data Source Integration — CRA Tax Data

**User Story:** As a developer, I want the CRA_Source integration to provide authoritative T4 and payroll records, so that the History_Inconsistency_Engine and Document_Forgery_Engine can cross-reference employment declarations against tax records.

#### Acceptance Criteria

1. THE CRA_Adapter SHALL retrieve T4 slip data, payroll records, and employer NOC category data via the Government of Canada secure data exchange using mutually authenticated TLS 1.3 with client certificates.
2. THE CRA_Adapter SHALL transmit only the minimum necessary fields — SIN, tax year, employer BN, employment income, and NOC category — per the Privacy Act data minimisation principle.
3. IF the CRA_Source does not respond within 5 seconds, THE CRA_Adapter SHALL treat the connection as failed and SHALL return the structured UNAVAILABLE response.
4. IF the CRA_Source returns an error or is unavailable, THEN THE CRA_Adapter SHALL return a structured UNAVAILABLE response to the calling Detection_Engine within 200 milliseconds of the 5-second timeout threshold being reached and THE Detection_Pipeline SHALL continue with a PARTIAL_RESULT indicator for the affected engine.
5. WHERE the deployment environment is development or test, THE CRA_Adapter SHALL generate synthetic T4 records that include the same minimum fields as production responses: SIN, tax year, employer BN, employment income, and NOC category.
6. THE CRA_Adapter SHALL log every data access with the Claim_ID, requesting component identifier, fields accessed, and timestamp to the immutable audit trail within 1 second of the access occurring, in compliance with the Privacy Act access logging requirement.
7. WHEN the CRA_Source returns a T4 record for a tax year that is more than 10 years before the current year, THE CRA_Adapter SHALL discard that record, log a warning, and SHALL NOT pass it to the calling Detection_Engine.
8. WHEN the CRA_Adapter receives a successful response, THE CRA_Adapter SHALL return the data to the calling Detection_Engine within 5 seconds of initiating the request.

---

### Requirement 12: External Data Source Integration — Provincial and Business Registries

**User Story:** As a developer, I want the Provincial_Registry_Source and Business_Registry_Source integrations to provide employer registration data, so that the Fraud_Ring_Engine can verify employer legitimacy and detect shell companies.

#### Acceptance Criteria

1. THE Business_Registry_Adapter SHALL query the Corporations Canada API and each provincial registry API to retrieve employer registration date, registered address type (residential vs commercial), and corporate status for a given Business Number (BN).
2. WHEN a provincial registry API is unavailable, THE Business_Registry_Adapter SHALL attempt the remaining provincial APIs and SHALL return a PARTIAL_RESULT with the available provinces' data, and the PARTIAL_RESULT SHALL identify which provinces could not be reached.
3. IF all provincial registry APIs and the Corporations Canada API are simultaneously unavailable, THEN THE Business_Registry_Adapter SHALL return a SERVICE_UNAVAILABLE response to the calling engine.
4. THE Business_Registry_Adapter SHALL classify a registered address as residential when the address matches a residential zoning indicator from the GeoNames_Source or when the address appears in a residential postal code range, and SHALL classify it as commercial otherwise.
5. IF the GeoNames_Source is unavailable during address classification, THE Business_Registry_Adapter SHALL fall back to postal code range classification only and SHALL flag the result as UNVERIFIED_ZONING.
6. WHERE the deployment environment is development or test, THE Business_Registry_Adapter SHALL serve synthetic corporate records from `mock-data/business-registries/` and `mock-data/provincial-registries/`, with at least one fixture for each of the 10 provinces and 3 territories (13 total).
7. FOR ALL employer Business Numbers BN, parsing the Business_Registry_Adapter's canonical response and re-serialising it to JSON SHALL produce a byte-equivalent structure with no fields added, removed, or reordered (registry round-trip property).

---

### Requirement 13: External Data Source Integration — CBSA Border Data

**User Story:** As a developer, I want the CBSA_Source integration to provide cross-border travel records, so that the Geographic_Anomaly_Engine can identify claimants who were outside Canada during their claimed employment period.

#### Acceptance Criteria

1. THE CBSA_Adapter SHALL retrieve cross-border entry and exit records for a claimant SIN from the CBSA secure API, restricted to the claim's employment period. Each record SHALL include at minimum: crossing direction (entry/exit), crossing date, and port of entry identifier.
2. WHEN the CBSA_Source records indicate that a claimant was outside Canada for 10 or more consecutive calendar days during the declared employment period, THE Geographic_Anomaly_Engine SHALL generate a HIGH-severity Indicator. The HIGH-severity Indicator SHALL state the claimant SIN, the absence start date, and the total consecutive day count.
3. IF the CBSA API returns an error or does not respond within 5 seconds, THE CBSA_Adapter SHALL return a failure result to the Geographic_Anomaly_Engine, preserve any partially received records, and SHALL NOT auto-retry.
4. THE CBSA_Adapter SHALL transmit data exclusively over the CBSA's Protected B secure channel and SHALL NOT log travel record contents to any general-purpose application log.
5. WHERE the deployment environment is development or test, THE CBSA_Adapter SHALL generate synthetic travel records from `mock-data/cbsa/` using a deterministic generator that produces travel patterns consistent with the claimant's declared province of employment.
6. THE CBSA_Adapter SHALL respond within 300 milliseconds for parsing and delivery after response receipt, where normal operating conditions are defined as responses containing no more than 500 travel records.

---

### Requirement 14: External Data Source Integration — GeoNames

**User Story:** As a developer, I want the GeoNames_Source integration to provide accurate geographic data, so that the Geographic_Anomaly_Engine can compute distances and evaluate transit feasibility.

#### Acceptance Criteria

1. THE GeoNames_Adapter SHALL resolve Canadian postal codes and addresses to latitude/longitude coordinates using the GeoNames REST API and SHALL return coordinates bounded within latitude [-90.0, 90.0] and longitude [-180.0, 180.0].
2. IF the GeoNames REST API or local data dump cannot resolve a postal code or address to coordinates, THE GeoNames_Adapter SHALL return a RESOLUTION_FAILED response and SHALL NOT return default or approximate coordinates.
3. THE GeoNames_Adapter SHALL compute geodesic distances between two coordinate pairs using the Haversine formula and return the result in kilometres rounded to 3 decimal places.
4. WHEN the GeoNames_Source REST API is unavailable, THE GeoNames_Adapter SHALL fall back to a local GeoNames data dump stored in `mock-data/geonames/`, which SHALL be updated no less frequently than every 90 calendar days.
5. WHERE the deployment environment is development or test, THE GeoNames_Adapter SHALL serve coordinates and distance computations from the local GeoNames data dump.
6. IF either input coordinate is outside the bounds latitude [-90.0, 90.0] or longitude [-180.0, 180.0], THE GeoNames_Adapter SHALL return an INVALID_COORDINATE error and SHALL NOT compute a distance.
7. FOR ALL coordinate pairs (A, B), THE GeoNames_Adapter SHALL return distance(A, B) equal to distance(B, A) within a tolerance of 0.001 kilometres (geodesic symmetry property).
8. FOR ALL coordinate pairs (A, A), THE GeoNames_Adapter SHALL return a distance of 0.0 kilometres (zero self-distance property).

---

### Requirement 15: External Data Source Integration — Motor Vehicle Registry

**User Story:** As a developer, I want the MVR_Source integration to provide vehicle ownership data, so that the Geographic_Anomaly_Engine can confirm or deny a claimant's access to personal transportation.

#### Acceptance Criteria

1. THE MVR_Adapter SHALL query the provincial Motor Vehicle Registry API for the province matching the claimant's residential address and return all vehicles registered to the claimant's SIN or provincial driver identifier.
2. WHEN the provincial MVR API is unavailable, THE MVR_Adapter SHALL return an UNAVAILABLE response and THE Geographic_Anomaly_Engine SHALL treat vehicle ownership as unknown rather than absent.
3. IF the MVR_Adapter does not receive a response within 3 seconds, it SHALL return an UNAVAILABLE response and THE Geographic_Anomaly_Engine SHALL treat vehicle ownership as unknown.
4. THE MVR_Adapter SHALL request only vehicle ownership status and vehicle class — no vehicle history, financial, or insurance data — per the Privacy Act data minimisation principle.
5. THE MVR_Adapter SHALL respond within 3 seconds under normal operating conditions, where normal is defined as a single-province lookup.
6. WHERE the deployment environment is development or test, THE MVR_Adapter SHALL generate synthetic provincial vehicle records from `mock-data/motor-vehicle/`, with at least one fixture for each of the ten provinces.

---

### Requirement 16: External Data Source Integration — Job Bank Canada

**User Story:** As a developer, I want the Job_Bank_Source integration to provide real-time job posting data, so that the Regional_Analysis_Engine can identify claim periods with high local job availability.

#### Acceptance Criteria

1. THE Job_Bank_Adapter SHALL query the Job Bank Canada REST API to retrieve active job postings by NOC code and ESDC economic region, filtered to postings active during the claim period.
2. THE Job_Bank_Adapter SHALL extract the count of active postings and the median advertised hourly wage for the specified NOC code and region.
3. WHEN the Job_Bank_Source API is unavailable, THE Job_Bank_Adapter SHALL fall back to a cached Job Bank open data snapshot in `mock-data/job-bank/` with a maximum age of 7 days and SHALL include a `DATA_STALE` flag.
4. WHERE the deployment environment is development or test, THE Job_Bank_Adapter SHALL serve data from the Job Bank open data snapshot in `mock-data/job-bank/`.
5. FOR ALL (NOC_code, region) pairs, parsing the Job_Bank_Adapter canonical response and re-serialising it to JSON SHALL produce an equivalent structure with identical posting counts and wage values (job-bank round-trip property).

---

### Requirement 17: Performance Requirements

**User Story:** As ESDC, I want the EI-ADS to meet strict throughput and latency targets, so that fraud detection does not delay the processing of legitimate claims.

#### Acceptance Criteria

1. THE Detection_Pipeline SHALL produce a Risk_Score and Risk_Decision for 99% of claims within 2,000 milliseconds of the `claim.received` event under a sustained load of 500 concurrent claims.
2. THE Detection_Pipeline SHALL sustain a throughput of at least 2,000 claims per minute without degradation in latency percentiles.
3. THE Claimant_Portal SHALL render the claim submission form and return submission confirmation within 3,000 milliseconds under a sustained load of 1,000 concurrent users.
4. THE Redis_Cache SHALL achieve a cache hit rate of at least 90% for External_Data_Adapter responses during peak claim submission periods.
5. THE EI-ADS SHALL be deployed on Kubernetes / OpenShift on Government Cloud (GCP or Azure Government) and SHALL support horizontal pod autoscaling to maintain latency targets during load spikes of up to 3× baseline traffic.
6. THE Prometheus_Grafana_Monitor SHALL alert the operations team within 60 seconds when the 99th-percentile end-to-end claim processing latency exceeds 3,000 milliseconds.

---

### Requirement 18: Fraud Detection Accuracy Requirements

**User Story:** As ESDC, I want the EI-ADS to achieve measurable accuracy targets, so that the system delivers the promised return on investment and does not harm legitimate claimants.

#### Acceptance Criteria

1. THE Ensemble_Scorer's fraud classification model SHALL achieve a fraud detection rate (true positive rate) of at least 85% on the labelled historical claims validation dataset.
2. THE Ensemble_Scorer's fraud classification model SHALL achieve a false positive rate no greater than 5% on the labelled historical claims validation dataset.
3. THE Ensemble_Scorer SHALL be retrained on new labelled data no less frequently than quarterly to maintain accuracy targets as fraud patterns evolve.
4. WHEN a claim that was AUTO_APPROVED is subsequently confirmed as fraudulent by an investigation outcome, THE EI-ADS SHALL ingest that outcome as a training signal for the next model retraining cycle.
5. THE Ensemble_Scorer SHALL report a calibration score (Brier score) of no greater than 0.10 on the validation dataset, ensuring that probability estimates are meaningful and not merely rank-ordered.

---

### Requirement 19: Privacy, Security, and Compliance

**User Story:** As the Government of Canada Privacy Office, I want EI-ADS to handle Protected B personal data in full compliance with the Privacy Act and PIPEDA, so that claimant rights are protected and the department meets its legal obligations.

#### Acceptance Criteria

1. THE EI-ADS SHALL classify all claimant PII — including SIN, name, address, employment records, and benefit amounts — as Protected_B and SHALL enforce role-based access control such that only authorised ESDC personnel can access raw PII fields.
2. THE EI-ADS SHALL implement data minimisation: each External_Data_Adapter SHALL request only the fields required for its corresponding Detection_Engine's analysis and SHALL NOT store any additional fields returned by the external source.
3. THE EI-ADS SHALL retain claim records, Indicators, Risk_Scores, audit logs, and SHAP attributions for a minimum of 7 years in accordance with Government of Canada records retention schedules.
4. THE EI-ADS SHALL provide a claimant data access API that returns all records held for a given SIN in a machine-readable format within 30 days of a Privacy Act access request.
5. THE EI-ADS SHALL provide a claimant data deletion API that anonymises identifying fields in all records for a given SIN within 30 days of a Privacy Act correction or erasure request, while preserving the statistical and aggregate records required for audit.
6. THE EI-ADS SHALL conduct and pass a Security Assessment and Authorisation (SA&A) review under the Government of Canada Directive on Security Management before processing live claimant data.
7. ALL EI-ADS API endpoints SHALL reject requests that do not present a valid OAuth 2.0 Bearer token issued by GCKey, returning HTTP 401 for unauthenticated requests and HTTP 403 for requests with insufficient scope.
8. THE EI-ADS SHALL generate a complete Privacy Impact Assessment (PIA) document and maintain it current with each major system change.

---

### Requirement 20: Bilingual Output Requirements

**User Story:** As a claimant or investigator, I want all system outputs to be available in both English and French, so that I can interact with EI-ADS in my preferred official language.

#### Acceptance Criteria

1. THE Claimant_Portal SHALL render all user-facing text — including field labels, instructions, validation messages, and risk notifications — in the language selected by the user, defaulting to the language preference set in the user's GCKey profile.
2. THE LLM_Service SHALL generate all natural-language Indicator explanations and Risk_Decision summaries as parallel English and French text.
3. THE LLM_Service SHALL produce French translations that are semantically equivalent to the English source text and SHALL NOT use machine translation that produces grammatically incorrect or semantically misleading French output.
4. THE EI-ADS SHALL store both the English and French versions of all LLM_Service-generated explanations in the ClickHouse analytics store alongside each claim record.
5. THE Claimant_Portal SHALL pass the Government of Canada's bilingual web content quality assessment for both English and French versions of all pages.
6. WHEN a bilingual explanation is generated for a claim, the English explanation length and French explanation length SHALL each fall within 20% of each other in character length, ensuring proportional coverage of the risk factors in both languages (bilingual coverage balance property).

---

### Requirement 21: Monitoring, Alerting, and Observability

**User Story:** As an EI-ADS operations engineer, I want comprehensive monitoring and alerting, so that system degradation, model drift, and integration failures are detected and resolved before they affect fraud detection accuracy.

#### Acceptance Criteria

1. THE Prometheus_Grafana_Monitor SHALL collect and expose the following metrics for every Detection_Engine: request count, error count, P50 and P99 latency, and Indicator generation rate.
2. THE Prometheus_Grafana_Monitor SHALL track the daily fraud detection rate, false positive rate, and average Risk_Score distribution and SHALL alert the operations team when any metric deviates by more than 10% from its 30-day rolling baseline.
3. THE Prometheus_Grafana_Monitor SHALL alert within 60 seconds when any External_Data_Adapter reports an error rate above 5% over a 5-minute window.
4. THE Prometheus_Grafana_Monitor SHALL expose a `/healthz` endpoint for each microservice that returns HTTP 200 when the service is operating normally and HTTP 503 when the service is degraded.
5. THE EI-ADS SHALL implement distributed tracing using OpenTelemetry, capturing a complete trace from `claim.received` to Risk_Decision publication for every processed claim.
6. WHEN the Ensemble_Scorer detects model drift — defined as the fraud detection rate dropping below 80% on a rolling 7-day validation window — THE Prometheus_Grafana_Monitor SHALL trigger an alert classified as P1 (critical) requiring an immediate model review.

---

### Requirement 22: Investigator Dashboard and Case Management

**User Story:** As an investigator, I want a dashboard that surfaces flagged claims with their risk scores, indicators, and explanations, so that I can efficiently review and action cases without switching between systems.

#### Acceptance Criteria

1. THE Claimant_Portal's investigator view SHALL display all claims with Risk_Decision FLAG_FOR_REVIEW or INVESTIGATE, sorted by Risk_Score descending, refreshed in near-real-time via server-sent events or WebSocket.
2. WHEN an investigator selects a flagged claim, THE Claimant_Portal SHALL display the Risk_Score, Risk_Decision, all Indicators with severity badges, the SHAP attribution chart, and the LLM_Service-generated explanation in the investigator's selected official language.
3. THE Claimant_Portal's investigator view SHALL allow investigators to record a disposition — CONFIRMED_FRAUD, FALSE_POSITIVE, or NEEDS_FURTHER_REVIEW — and attach case notes for each flagged claim.
4. WHEN an investigator records a CONFIRMED_FRAUD or FALSE_POSITIVE disposition, THE EI-ADS SHALL immediately publish an outcome event to the Kafka_Bus for ingestion as a model feedback signal.
5. THE Claimant_Portal's investigator view SHALL render the SHAP attribution chart as an accessible, screen-reader-compatible bar chart with text alternatives in both English and French.
6. THE Claimant_Portal SHALL enforce that only users with the INVESTIGATOR or ADMINISTRATOR role can access flagged claim details and that claimant PII fields are masked for users with the REVIEWER role.

---

### Requirement 23: System Resilience and Disaster Recovery

**User Story:** As ESDC, I want the EI-ADS to remain operational during infrastructure failures and recover predictably, so that claim processing is not blocked by single points of failure.

#### Acceptance Criteria

1. THE EI-ADS SHALL achieve a monthly uptime of at least 99.5% for all claim intake and detection services, excluding scheduled maintenance windows.
2. WHEN any single External_Data_Adapter is unavailable, THE Detection_Pipeline SHALL continue processing claims using available data sources and SHALL produce a Risk_Decision annotated with the names of any unavailable sources.
3. THE EI-ADS SHALL implement a recovery time objective (RTO) of 4 hours and a recovery point objective (RPO) of 1 hour for the PostgreSQL and ClickHouse data stores.
4. THE Kafka_Bus SHALL be configured with a replication factor of at least 3 and a minimum in-sync replica count of 2 to prevent message loss during broker failures.
5. THE Redis_Cache SHALL be deployed in a clustered configuration with at least 2 replica nodes to prevent cache loss from causing External_Data_Adapter latency spikes.
6. THE EI-ADS SHALL perform daily automated backups of all PostgreSQL and ClickHouse data to a geographically separate Government Cloud storage bucket and SHALL verify backup integrity via automated restoration tests no less frequently than weekly.

---

## Property-Based Correctness Properties Summary

The following properties SHALL be verified through automated property-based tests using a framework such as Hypothesis (Python) or fast-check (TypeScript) as part of the CI pipeline:

| ID | Component | Property | Type |
|---|---|---|---|
| P1 | Repetitive_Pattern_Engine | For N ≥ 3 claims from the same SIN with inter-claim intervals within ±14 calendar days, at least one pattern Indicator is always generated. | Invariant |
| P2 | Geographic_Anomaly_Engine | distance(A, B) = distance(B, A) and distance(A, A) = 0 for all coordinate pairs. | Invariant |
| P3 | Fraud_Ring_Engine | Community detection partitions all N claimant nodes into exhaustive, non-overlapping communities whose member counts sum to N. | Invariant |
| P4 | History_Inconsistency_Engine | For identical ROE and T4 earnings, discrepancy = 0 and no discrepancy Indicator is generated. | Invariant |
| P5 | Document_Forgery_Engine | Authenticated ROE documents produce zero forgery Indicators. | Invariant |
| P6 | Document_Forgery_Engine | parse(render(parse(D))) produces a field set with exact value matches for all numeric and date fields and at most 1 character difference per free-text field of 20+ characters, for all ROE documents D. | Round-trip |
| P7 | Ensemble_Scorer | SHAP contributions sum to Risk_Score minus base value for all claims. | Invariant |
| P8 | Ensemble_Scorer | Zero Indicators produces Risk_Score = 0 and AUTO_APPROVE. | Invariant |
| P9 | Ensemble_Scorer | If Indicators(C1) ⊃ Indicators(C2), then Risk_Score(C1) ≥ Risk_Score(C2). | Monotonicity |
| P10 | Stats_Canada_Adapter | parse(serialise(parse(response))) produces equivalent LabourForceData covering all ESDC economic regions with values to 4 decimal places, for all API responses. | Round-trip |
| P11 | GeoNames_Adapter | distance(A, B) = distance(B, A) within 0.001 km; distance(A, A) = 0; INVALID_COORDINATE returned for out-of-bounds inputs. | Invariant |
| P12 | Job_Bank_Adapter | parse(serialise(canonical)) preserves posting counts and wage values for all (NOC, region) pairs. | Round-trip |
| P13 | Regional_Analysis_Engine | For regional_rate = national_rate, deviation = 0 and no regional unemployment Indicator is generated. | Invariant |
| P14 | Regional_Analysis_Engine | Time-series forecast for month N+1 falls within the model's 95% prediction interval across all historical regions. | Statistical calibration |
| P15 | LLM_Service (bilingual) | English and French explanations for the same claim are within 20% of each other in word count. | Metamorphic |
