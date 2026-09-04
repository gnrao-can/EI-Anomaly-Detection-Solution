-- =============================================================================
-- ClickHouse Schema 005: Create adapter_audit_log table
-- =============================================================================
-- Requirements: 2.6, 19.3
--
-- Analytics copy of the adapter audit trail, written within 1 second of the
-- authoritative PostgreSQL audit_log record (Requirement 19.3). This is a
-- reporting/analytics mirror only — the PostgreSQL audit_log remains the
-- authoritative record of access to Protected B data.
-- Idempotent: safe to run multiple times (CREATE TABLE IF NOT EXISTS).
--
-- Engine: MergeTree
-- Partition key: toYYYYMM(logged_at)
-- Order key: (component, logged_at)
-- TTL: 7 years from logged_at
-- =============================================================================

CREATE TABLE IF NOT EXISTS ei_ads_analytics.adapter_audit_log
(
    log_id          UUID,
    claim_id        UUID,
    component       LowCardinality(String),           -- Adapter/component name
    action          LowCardinality(String),           -- Operation performed
    fields_accessed Array(String),                    -- Protected B fields touched
    outcome         LowCardinality(String),           -- SUCCESS | DENIED | ERROR
    actor_role      LowCardinality(String),           -- Role of the requesting actor
    sin_hash        FixedString(64),                  -- SHA-256 hex digest; raw SIN never stored
    request_id      UUID,                             -- Correlating request identifier
    duration_ms     Nullable(Float64),                -- Operation latency
    logged_at       DateTime64(3, 'UTC')
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(logged_at)
ORDER BY (component, logged_at)
TTL toDateTime(logged_at) + INTERVAL 7 YEAR
SETTINGS index_granularity = 8192;
