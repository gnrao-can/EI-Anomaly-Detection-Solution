-- =============================================================================
-- ClickHouse Schema 001: Create claims_analytics table
-- =============================================================================
-- Requirements: 2.6
--
-- Analytics copy of EI claim records for 7-year retention and reporting.
-- Protected B: SIN is never stored — only sin_hash (SHA-256 digest).
-- Idempotent: safe to run multiple times (CREATE ... IF NOT EXISTS).
--
-- Engine: MergeTree
-- Partition key: toYYYYMM(received_at)
-- Order key: (sin_hash, received_at)
-- TTL: 7 years from received_at
-- =============================================================================

-- Ensure the analytics database exists before creating the table.
CREATE DATABASE IF NOT EXISTS ei_ads_analytics;

CREATE TABLE IF NOT EXISTS ei_ads_analytics.claims_analytics
(
    claim_id                UUID,
    sin_hash                FixedString(64),          -- SHA-256 hex digest; raw SIN never stored
    employer_bn             String,                   -- 15-char BN (9-digit + RT + 4-digit account)
    insurable_hours         Decimal(10, 2),
    insurable_earnings      Decimal(12, 2),
    separation_date         Date,
    separation_reason_code  String,                   -- ROE reason code A–N
    claim_start_date        Date,
    economic_region_code    String,                   -- ESDC 2-char economic region code
    noc_code                String,                   -- NOC 2021 5-digit code
    residential_postal_code String,                   -- FSA only (first 3 chars of postal code)
    status                  LowCardinality(String),   -- RECEIVED | PROCESSING | SCORED | ERROR
    language                LowCardinality(String),   -- 'en' | 'fr'
    received_at             DateTime64(3, 'UTC'),
    updated_at              DateTime64(3, 'UTC')
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(received_at)
ORDER BY (sin_hash, received_at)
TTL toDateTime(received_at) + INTERVAL 7 YEAR
SETTINGS index_granularity = 8192;
