-- =============================================================================
-- ClickHouse Schema 003: Create risk_assessments_analytics table
-- =============================================================================
-- Requirements: 2.6, 19.3
--
-- Analytics copy of risk assessment scoring results.
-- One row per scored EI claim (final ensemble decision + explanation).
-- Idempotent: safe to run multiple times (CREATE TABLE IF NOT EXISTS).
--
-- Engine: MergeTree
-- Partition key: toYYYYMM(scored_at)
-- Order key: (claim_id, scored_at)
-- TTL: 7 years from scored_at
-- =============================================================================

CREATE TABLE IF NOT EXISTS ei_ads_analytics.risk_assessments_analytics
(
    assessment_id       UUID,
    claim_id            UUID,
    risk_score          UInt8,                        -- 0–100 ensemble risk score
    risk_decision       LowCardinality(String),       -- E.g. AUTO_APPROVE | REVIEW | INVESTIGATE
    shap_base_value     Float64,                      -- SHAP expected value (model baseline)
    explanation_en      String,                       -- Natural language rationale (English)
    explanation_fr      String,                       -- French equivalent
    engines_completed   Array(LowCardinality(String)),-- Engines that returned results
    engines_partial     Array(LowCardinality(String)),-- Engines that returned partial/no results
    model_version       String,                       -- Scoring model version identifier
    processing_time_ms  Nullable(Float64),            -- End-to-end scoring latency
    scored_at           DateTime64(3, 'UTC')
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(scored_at)
ORDER BY (claim_id, scored_at)
TTL toDateTime(scored_at) + INTERVAL 7 YEAR
SETTINGS index_granularity = 8192;
