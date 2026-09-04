-- =============================================================================
-- ClickHouse Schema 002: Create indicators_analytics table
-- =============================================================================
-- Requirements: 2.6
--
-- Analytics copy of detection engine Indicator events.
-- One row per Indicator produced by any of the six detection engines.
-- Idempotent: safe to run multiple times (CREATE TABLE IF NOT EXISTS).
--
-- Engine: MergeTree
-- Partition key: toYYYYMM(produced_at)
-- Order key: (claim_id, engine_type, produced_at)
-- TTL: 7 years from produced_at
-- =============================================================================

CREATE TABLE IF NOT EXISTS ei_ads_analytics.indicators_analytics
(
    indicator_id      UUID,
    claim_id          UUID,
    assessment_id     UUID,
    engine_type       LowCardinality(String),   -- REPETITIVE_PATTERN | GEOGRAPHIC | FRAUD_RING |
                                                --   HISTORY_INCONSISTENCY | DOCUMENT_FORGERY | REGIONAL
    severity          LowCardinality(String),   -- LOW | MEDIUM | HIGH | CRITICAL | UNRESOLVED
    algorithm         String,                   -- E.g. "sequential_pattern_mining"
    title             String,                   -- Short description (≤ 100 chars)
    explanation_en    String,                   -- Natural language, ≤ 500 chars
    explanation_fr    String,                   -- French equivalent
    payload           String,                   -- JSON serialised as string (structured evidence)
    shap_contribution Nullable(Float64),        -- SHAP attribution value; NULL before scoring
    produced_at       DateTime64(3, 'UTC')
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(produced_at)
ORDER BY (claim_id, engine_type, produced_at)
TTL toDateTime(produced_at) + INTERVAL 7 YEAR
SETTINGS index_granularity = 8192;
