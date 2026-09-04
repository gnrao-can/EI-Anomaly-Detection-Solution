# EI-ADS Kafka Topics

This directory provisions the Apache Kafka topics that form the EI-ADS event
bus. The topic definitions come directly from the "Kafka Topic Structure"
section of the design document, which is the source of truth for partition
counts, retention, and replication factors.

## Files

| File | Purpose |
|---|---|
| `topics.json` | Declarative manifest of all 11 topics and their configuration. |
| `create-topics.sh` | Idempotent provisioning script that reads `topics.json`. |
| `README.md` | This document — topic taxonomy and operational notes. |

## Provisioning

```bash
# Local single-broker Kafka (RF and min.insync.replicas forced to 1)
KAFKA_BOOTSTRAP_SERVERS=localhost:9092 KAFKA_ENV=dev ./create-topics.sh

# Production 6-broker cluster (uses RF=3 / min.insync.replicas=2 from manifest)
KAFKA_BOOTSTRAP_SERVERS=kafka-0:9092,kafka-1:9092 KAFKA_ENV=prod ./create-topics.sh
```

The script requires [`jq`](https://jqlang.github.io/jq/) to parse `topics.json`
and `kafka-topics.sh` on `PATH`. On the Bitnami image the binary lives at
`/opt/bitnami/kafka/bin/kafka-topics.sh`; set `KAFKA_TOPICS_BIN` to point at it
if it is not already on `PATH`. The `--if-not-exists` flag makes the script safe
to re-run.

## Replication and durability

- **Production:** every topic uses `replication_factor=3` and
  `min.insync.replicas=2`. With acks=all producers, this guarantees a write is
  acknowledged only once it is durably stored on at least two brokers, so the
  cluster tolerates the loss of a single broker without data loss (Req 23.4).
- **Local dev:** a single-broker Kafka cannot host 3 replicas, so
  `create-topics.sh` overrides `replication_factor` and `min.insync.replicas`
  to `1` when `KAFKA_ENV=dev`.

## Partitioning key

All topics are partitioned by `claim_id`. This guarantees that every event for a
given claim is ordered within a single partition, so downstream consumers see a
consistent per-claim event sequence.

## Topic taxonomy

Retention below is shown in human terms; the manifest stores `retention_ms`.
Production RF is 3 and `min.insync.replicas` is 2 for every topic.

### `claim.received`
- **Purpose:** Claim intake events emitted when a new claim is accepted.
- **Producer(s):** Claim Intake Service.
- **Consumer(s):** Detection Pipeline Orchestrator.
- **Partition key:** `claim_id`
- **Partitions:** 24 &nbsp;|&nbsp; **Retention:** 7 days &nbsp;|&nbsp; **RF:** 3

### `roe.received`
- **Purpose:** ROE normalisation events emitted after an ROE document is parsed
  into the canonical Claim model.
- **Producer(s):** ROE Adapter.
- **Consumer(s):** Detection Pipeline Orchestrator.
- **Partition key:** `claim_id`
- **Partitions:** 24 &nbsp;|&nbsp; **Retention:** 7 days &nbsp;|&nbsp; **RF:** 3

### `indicators.repetitive-pattern`
- **Purpose:** Indicator events from the Repetitive Pattern Engine.
- **Producer(s):** Repetitive Pattern Engine.
- **Consumer(s):** Ensemble Scorer.
- **Partition key:** `claim_id`
- **Partitions:** 12 &nbsp;|&nbsp; **Retention:** 30 days &nbsp;|&nbsp; **RF:** 3

### `indicators.geographic-anomaly`
- **Purpose:** Indicator events from the Geographic Anomaly Engine.
- **Producer(s):** Geographic Anomaly Engine.
- **Consumer(s):** Ensemble Scorer.
- **Partition key:** `claim_id`
- **Partitions:** 12 &nbsp;|&nbsp; **Retention:** 30 days &nbsp;|&nbsp; **RF:** 3

### `indicators.fraud-ring`
- **Purpose:** Indicator events from the Fraud Ring Engine.
- **Producer(s):** Fraud Ring Engine.
- **Consumer(s):** Ensemble Scorer.
- **Partition key:** `claim_id`
- **Partitions:** 12 &nbsp;|&nbsp; **Retention:** 30 days &nbsp;|&nbsp; **RF:** 3

### `indicators.history-inconsistency`
- **Purpose:** Indicator events from the Employment History Inconsistency Engine.
- **Producer(s):** History Inconsistency Engine.
- **Consumer(s):** Ensemble Scorer.
- **Partition key:** `claim_id`
- **Partitions:** 12 &nbsp;|&nbsp; **Retention:** 30 days &nbsp;|&nbsp; **RF:** 3

### `indicators.document-forgery`
- **Purpose:** Indicator events from the Document Forgery Engine.
- **Producer(s):** Document Forgery Engine.
- **Consumer(s):** Ensemble Scorer.
- **Partition key:** `claim_id`
- **Partitions:** 12 &nbsp;|&nbsp; **Retention:** 30 days &nbsp;|&nbsp; **RF:** 3

### `indicators.regional-analysis`
- **Purpose:** Indicator events from the Regional Analysis Engine.
- **Producer(s):** Regional Analysis Engine.
- **Consumer(s):** Ensemble Scorer.
- **Partition key:** `claim_id`
- **Partitions:** 12 &nbsp;|&nbsp; **Retention:** 30 days &nbsp;|&nbsp; **RF:** 3

### `claim.scored`
- **Purpose:** Final Risk_Score and Risk_Decision for each processed claim.
- **Producer(s):** LLM Service / Ensemble Scoring pipeline (via Orchestrator).
- **Consumer(s):** Investigator Dashboard, ClickHouse audit sink,
  `claim.escalated` router.
- **Partition key:** `claim_id`
- **Partitions:** 24 &nbsp;|&nbsp; **Retention:** 90 days &nbsp;|&nbsp; **RF:** 3

### `claim.escalated`
- **Purpose:** INVESTIGATE decisions routed for escalation.
- **Producer(s):** Detection Pipeline Orchestrator.
- **Consumer(s):** SIU / Investigator Dashboard notification service.
- **Partition key:** `claim_id`
- **Partitions:** 12 &nbsp;|&nbsp; **Retention:** 90 days &nbsp;|&nbsp; **RF:** 3

### `outcome.feedback`
- **Purpose:** Investigator disposition events (CONFIRMED_FRAUD,
  FALSE_POSITIVE, NEEDS_FURTHER_REVIEW) used to retrain models.
- **Producer(s):** Investigator Dashboard.
- **Consumer(s):** Model retraining pipeline, ClickHouse analytics sink.
- **Partition key:** `claim_id`
- **Partitions:** 12 &nbsp;|&nbsp; **Retention:** 365 days &nbsp;|&nbsp; **RF:** 3

## Summary

| Topic | Partitions | Retention | RF | min.insync.replicas (prod) |
|---|---|---|---|---|
| `claim.received` | 24 | 7 days | 3 | 2 |
| `roe.received` | 24 | 7 days | 3 | 2 |
| `indicators.repetitive-pattern` | 12 | 30 days | 3 | 2 |
| `indicators.geographic-anomaly` | 12 | 30 days | 3 | 2 |
| `indicators.fraud-ring` | 12 | 30 days | 3 | 2 |
| `indicators.history-inconsistency` | 12 | 30 days | 3 | 2 |
| `indicators.document-forgery` | 12 | 30 days | 3 | 2 |
| `indicators.regional-analysis` | 12 | 30 days | 3 | 2 |
| `claim.scored` | 24 | 90 days | 3 | 2 |
| `claim.escalated` | 12 | 90 days | 3 | 2 |
| `outcome.feedback` | 12 | 365 days | 3 | 2 |

Local dev uses `RF=1` / `min.insync.replicas=1` (single-broker Kafka).
