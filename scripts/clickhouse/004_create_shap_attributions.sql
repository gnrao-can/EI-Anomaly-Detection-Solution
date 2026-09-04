-- =============================================================================
-- ClickHouse Schema 004: Create shap_attributions table
-- =============================================================================
-- Requirements: 2.6, 19.3
--
-- Per-indicator SHAP feature attributions supporting a risk assessment.
-- One row per (assessment, indicator) contribution to the final score.
-- Idempotent: safe to run multiple times (CREATE TABLE IF NOT EXISTS).
--
-- Engine: MergeTree
-- Partition key: toYYYYMM(scored_at)
-- Order key: (assessment_id, indicator_id)
-- TTL: 7 years from scored_at
-- =============================================================================

CREATE TABLE IF NOT EXISTS ei_ads_analytics.shap_attributions
(
    id              UInt64,                           -- Row identifier
    assessment_id   UUID,                             -- Parent risk assessment
    indicator_id    UUID,                             -- Source indicator
    feature_name    String,                           -- Model feature name
    contribution    Float64,                          -- SHAP contribution value
    base_value      Float64,                          -- SHAP expected value (model baseline)
    scored_at       DateTime64(3, 'UTC')
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(scored_at)
ORDER BY (assessment_id, indicator_id)
TTL toDateTime(scored_at) + INTERVAL 7 YEAR
SETTINGS index_granularity = 8192;
