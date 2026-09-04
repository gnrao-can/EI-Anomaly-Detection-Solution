-- =============================================================================
-- Migration 003: Create indicators table
-- =============================================================================
-- Requirements: 1.4, 2.6, 2.7, 19.3
--
-- Depends on: 001_create_claims.sql, 002_create_risk_assessments.sql
--
-- Idempotent: safe to run multiple times.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Table: indicators
-- ---------------------------------------------------------------------------
-- Structured anomaly signals produced by the six Detection Engines.
-- Each row captures one anomalous observation: which engine produced it,
-- its severity, the algorithm, bilingual explanations, and a JSONB payload
-- of structured evidence (amounts, dates, identifiers).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS indicators (
    indicator_id        UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id            UUID            NOT NULL
                            REFERENCES claims (claim_id) ON DELETE CASCADE,
    assessment_id       UUID
                            REFERENCES risk_assessments (assessment_id) ON DELETE SET NULL,
    engine_type         VARCHAR(30)     NOT NULL
                            CHECK (engine_type IN (
                                'REPETITIVE_PATTERN',
                                'GEOGRAPHIC_ANOMALY',
                                'FRAUD_RING',
                                'HISTORY_INCONSISTENCY',
                                'DOCUMENT_FORGERY',
                                'REGIONAL_ANALYSIS'
                            )),
    severity            VARCHAR(15)     NOT NULL
                            CHECK (severity IN (
                                'LOW',
                                'MEDIUM',
                                'HIGH',
                                'CRITICAL',
                                'UNRESOLVED'
                            )),
    algorithm           VARCHAR(100)    NOT NULL,   -- E.g. "sequential_pattern_mining", "haversine_distance"
    title               VARCHAR(200)    NOT NULL,   -- Short description (≤ 200 chars for display)
    explanation_en      TEXT            NOT NULL,   -- Natural-language explanation in English (≤ 500 chars)
    explanation_fr      TEXT            NOT NULL,   -- Equivalent in French (≤ 500 chars)
    payload             JSONB           NOT NULL DEFAULT '{}',  -- Structured evidence (amounts, dates, IDs)
    shap_contribution   DOUBLE PRECISION,           -- Assigned by Ensemble Scorer; NULL until scored
    produced_at         TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE indicators IS
    'Anomaly signals produced by the six Detection Engines. '
    'One row per anomalous observation per claim. '
    'payload contains structured evidence (no raw PII).';

COMMENT ON COLUMN indicators.engine_type IS
    'Which Detection Engine produced this Indicator: REPETITIVE_PATTERN | GEOGRAPHIC_ANOMALY | '
    'FRAUD_RING | HISTORY_INCONSISTENCY | DOCUMENT_FORGERY | REGIONAL_ANALYSIS.';
COMMENT ON COLUMN indicators.severity IS
    'Risk severity: LOW | MEDIUM | HIGH | CRITICAL | UNRESOLVED. '
    'UNRESOLVED is used when a definitive severity cannot be determined (e.g., CRA adapter unavailable).';
COMMENT ON COLUMN indicators.payload IS
    'JSONB evidence dict: CAD amounts, dates, employer BNs, distance values, etc. '
    'Must not contain raw PII (SIN, full name, full address).';
COMMENT ON COLUMN indicators.shap_contribution IS
    'Signed SHAP contribution assigned by the Ensemble Scorer after scoring. '
    'NULL when the Indicator has not yet been scored.';

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_indicators_claim_id
    ON indicators (claim_id);

CREATE INDEX IF NOT EXISTS idx_indicators_engine_type
    ON indicators (engine_type);

CREATE INDEX IF NOT EXISTS idx_indicators_severity
    ON indicators (severity);

CREATE INDEX IF NOT EXISTS idx_indicators_assessment_id
    ON indicators (assessment_id);

-- GIN index on payload for efficient JSONB containment and key-existence queries
CREATE INDEX IF NOT EXISTS idx_indicators_payload
    ON indicators USING GIN (payload);
