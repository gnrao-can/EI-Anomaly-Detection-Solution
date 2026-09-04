-- =============================================================================
-- Migration 004: Create audit_log and claim_dispositions tables
-- =============================================================================
-- Requirements: 1.4, 2.6, 2.7, 19.3
--
-- Depends on: 001_create_claims.sql
--
-- Idempotent: safe to run multiple times.
--
-- SECURITY NOTE: The audit_log table uses Row-Level Security to enforce
-- immutability. The application role (ei_ads_app) is granted INSERT only.
-- No UPDATE or DELETE policy is defined, so those operations are blocked
-- for all roles except superusers. This implements the 7-year immutable
-- audit trail required by Requirement 19.3 and the design's security spec.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Table: audit_log
-- ---------------------------------------------------------------------------
-- Immutable append-only log of every significant system action touching a
-- claim. Written by all EI-ADS services (Claim Intake, engines, adapters,
-- Ensemble Scorer). Covers the 7-year retention requirement via a scheduled
-- archival process (not enforced at the DB layer for PG; ClickHouse TTL
-- handles the analytics copy — see 002_clickhouse schema).
--
-- Protected B: raw SIN is never stored; sin_hash is used when needed.
-- IP addresses are stored only as SHA-256 hashes (ip_address_hash).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS audit_log (
    log_id              BIGSERIAL       PRIMARY KEY,
    claim_id            UUID,                           -- NULL for non-claim-specific events
    component           VARCHAR(100)    NOT NULL,       -- Originating service/component name
    action              VARCHAR(100)    NOT NULL,       -- Action performed (e.g. "CLAIM_RECEIVED")
    fields_accessed     TEXT[],                         -- List of field names read/written
    outcome             VARCHAR(50),                    -- E.g. "SUCCESS", "FAILURE", "PARTIAL"
    actor_role          VARCHAR(30),                    -- RBAC role of the actor (CLAIMANT, REVIEWER, …)
    sin_hash            CHAR(64),                       -- SHA-256 hex digest; never plaintext SIN
    ip_address_hash     CHAR(64),                       -- SHA-256 hash of originating IP address
    request_id          UUID,                           -- Correlation ID from the originating HTTP request
    duration_ms         DOUBLE PRECISION,               -- Wall-clock duration of the audited operation
    logged_at           TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE audit_log IS
    'Immutable append-only audit log. Row-Level Security enforces INSERT-only access '
    'for the application role. No UPDATE or DELETE policy → rows cannot be modified. '
    '7-year retention; Protected B.';

COMMENT ON COLUMN audit_log.component IS
    'Name of the EI-ADS service or component that generated this log entry '
    '(e.g. "claim-intake-service", "geographic-anomaly-engine", "cra-adapter").';
COMMENT ON COLUMN audit_log.action IS
    'Action code describing what happened (e.g. "CLAIM_RECEIVED", "CRA_T4_FETCHED", '
    '"RISK_SCORE_ASSIGNED", "PRIVACY_DATA_ACCESS_REQUEST").';
COMMENT ON COLUMN audit_log.fields_accessed IS
    'Array of field names that were read or written during this action. '
    'Used for data minimisation audit trails (CRA adapter requirement 11.8).';
COMMENT ON COLUMN audit_log.sin_hash IS
    'SHA-256 hex digest of the claimant SIN + per-environment salt. '
    'Never store plaintext SIN in this column.';
COMMENT ON COLUMN audit_log.ip_address_hash IS
    'SHA-256 hash of the originating IP address. '
    'Hashed to avoid storing identifiable network information.';

-- ---------------------------------------------------------------------------
-- Indexes on audit_log
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_audit_log_claim_id
    ON audit_log (claim_id);

CREATE INDEX IF NOT EXISTS idx_audit_log_logged_at
    ON audit_log (logged_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_log_component
    ON audit_log (component);

-- ---------------------------------------------------------------------------
-- Row-Level Security: enforce INSERT-only access for the application role
-- ---------------------------------------------------------------------------
-- The ei_ads_app role is the application database user used by all EI-ADS
-- services. By enabling RLS and defining only an INSERT policy, any attempt
-- to UPDATE or DELETE rows will be blocked (no matching policy → denied).
-- Superusers bypass RLS; all application writes go through ei_ads_app.
-- ---------------------------------------------------------------------------

ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;

-- INSERT-only policy: allows the application to append new audit records.
-- The WITH CHECK (true) clause permits all inserts unconditionally.
-- Absence of UPDATE and DELETE policies means those operations are denied
-- for all non-superuser roles, implementing the immutability requirement.
DROP POLICY IF EXISTS audit_log_insert_only ON audit_log;
CREATE POLICY audit_log_insert_only
    ON audit_log
    FOR INSERT
    WITH CHECK (true);

-- ---------------------------------------------------------------------------
-- Table: claim_dispositions
-- ---------------------------------------------------------------------------
-- Investigator case management table. Records the outcome recorded by an
-- ESDC investigator after reviewing a flagged claim. Linked to the claim;
-- does NOT cascade-delete audit_log entries (audit_log is immutable).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS claim_dispositions (
    disposition_id      UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id            UUID            NOT NULL
                            REFERENCES claims (claim_id) ON DELETE CASCADE,
    disposition         VARCHAR(30)     NOT NULL
                            CHECK (disposition IN (
                                'CONFIRMED_FRAUD',
                                'FALSE_POSITIVE',
                                'NEEDS_FURTHER_REVIEW'
                            )),
    case_notes          TEXT,                           -- Free-text investigator notes
    investigator_id     VARCHAR(100)    NOT NULL,       -- GCKey subject ID of the investigator
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE claim_dispositions IS
    'Investigator disposition records for flagged claims. '
    'Written when an INVESTIGATOR or ADMINISTRATOR records a case outcome. '
    'Outcomes feed the Kafka outcome.feedback topic for model retraining.';

COMMENT ON COLUMN claim_dispositions.disposition IS
    'Investigator verdict: CONFIRMED_FRAUD | FALSE_POSITIVE | NEEDS_FURTHER_REVIEW.';
COMMENT ON COLUMN claim_dispositions.investigator_id IS
    'GCKey OAuth 2.0 subject identifier of the investigator who recorded this disposition. '
    'Not a raw name or email address.';

CREATE INDEX IF NOT EXISTS idx_claim_dispositions_claim_id
    ON claim_dispositions (claim_id);
