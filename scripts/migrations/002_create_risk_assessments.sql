-- =============================================================================
-- Migration 002: Create risk_assessments and shap_attributions tables
-- =============================================================================
-- Requirements: 1.4, 2.6, 2.7, 9.1, 9.3, 19.3
--
-- Depends on: 001_create_claims.sql (claims table must exist)
--
-- Idempotent: safe to run multiple times.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Table: risk_assessments
-- ---------------------------------------------------------------------------
-- One row per scoring run for a claim. The SHAP additivity invariant (P7)
-- requires: |sum(shap_attributions.contribution) - (risk_score - shap_base_value)| < 1e-4.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS risk_assessments (
    assessment_id       UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id            UUID            NOT NULL
                            REFERENCES claims (claim_id) ON DELETE CASCADE,
    risk_score          SMALLINT        NOT NULL
                            CHECK (risk_score BETWEEN 0 AND 100),
    risk_decision       VARCHAR(20)     NOT NULL
                            CHECK (risk_decision IN (
                                'AUTO_APPROVE',
                                'FLAG_FOR_REVIEW',
                                'INVESTIGATE',
                                'INDETERMINATE'
                            )),
    shap_base_value     DOUBLE PRECISION NOT NULL DEFAULT 0.0,  -- explainer.expected_value
    explanation_en      TEXT,                                    -- LLM-generated, ≤ 500 chars
    explanation_fr      TEXT,                                    -- Bilingual equivalent
    engines_completed   VARCHAR(30)[]   NOT NULL DEFAULT '{}',  -- EngineTypes that returned within budget
    engines_partial     VARCHAR(30)[]   NOT NULL DEFAULT '{}',  -- EngineTypes that timed out
    model_version       VARCHAR(50)     NOT NULL,               -- XGBoost / IF artefact identifier
    processing_time_ms  DOUBLE PRECISION,                       -- Wall-clock ms from engine dispatch to decision
    scored_at           TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE risk_assessments IS
    'Aggregated risk assessment produced by the Ensemble Scorer for one claim. '
    'One row per scoring run. SHAP additivity invariant (P7) must hold.';

COMMENT ON COLUMN risk_assessments.risk_score IS
    'Integer risk score [0, 100]: [0,29]=AUTO_APPROVE, [30,69]=FLAG_FOR_REVIEW, [70,100]=INVESTIGATE.';
COMMENT ON COLUMN risk_assessments.shap_base_value IS
    'XGBoost TreeExplainer expected_value (mean prediction across the training set).';
COMMENT ON COLUMN risk_assessments.engines_completed IS
    'Array of EngineType values that returned Indicators within their per-engine time budget.';
COMMENT ON COLUMN risk_assessments.engines_partial IS
    'Array of EngineType values that did NOT return within their per-engine time budget (PARTIAL_RESULT).';

-- ---------------------------------------------------------------------------
-- Indexes on risk_assessments
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_risk_assessments_claim_id
    ON risk_assessments (claim_id);

CREATE INDEX IF NOT EXISTS idx_risk_assessments_risk_decision
    ON risk_assessments (risk_decision);

CREATE INDEX IF NOT EXISTS idx_risk_assessments_risk_score
    ON risk_assessments (risk_score DESC);

CREATE INDEX IF NOT EXISTS idx_risk_assessments_scored_at
    ON risk_assessments (scored_at DESC);

-- ---------------------------------------------------------------------------
-- Table: shap_attributions
-- ---------------------------------------------------------------------------
-- Per-indicator SHAP feature attribution records. One row per Indicator per
-- scoring run. Sum of contribution values must satisfy the P7 additivity
-- invariant: sum(contribution) == risk_score - shap_base_value ± 1e-4.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS shap_attributions (
    id              BIGSERIAL       PRIMARY KEY,
    assessment_id   UUID            NOT NULL
                        REFERENCES risk_assessments (assessment_id) ON DELETE CASCADE,
    indicator_id    UUID            NOT NULL,   -- FK to indicators; not enforced to allow async writes
    feature_name    VARCHAR(100)    NOT NULL,   -- Human-readable XGBoost feature name
    contribution    DOUBLE PRECISION NOT NULL,  -- Signed SHAP value; positive → higher risk
    base_value      DOUBLE PRECISION NOT NULL   -- explainer.expected_value at time of scoring
);

COMMENT ON TABLE shap_attributions IS
    'SHAP feature attribution records. One row per Indicator per RiskAssessment. '
    'Additivity invariant: |SUM(contribution) - (risk_score - base_value)| < 1e-4.';

COMMENT ON COLUMN shap_attributions.contribution IS
    'Signed SHAP contribution: positive values increase the Risk_Score, negative values decrease it.';

-- ---------------------------------------------------------------------------
-- Index on shap_attributions
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_shap_attributions_assessment_id
    ON shap_attributions (assessment_id);
