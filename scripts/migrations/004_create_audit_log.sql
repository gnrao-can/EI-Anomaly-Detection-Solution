-- =============================================================================
-- Migration 004: Create audit_log and claim_dispositions tables
-- =============================================================================
-- Requirements: 1.4, 2.6, 2.7, 19.3
--
-- Depends on: 001_create_claims.sql
--
-- Idempotent: safe to run multiple times.
--
-- SECURITY NOTE: The audit_log table is APPEND-ONLY and immutable. INSERT is
-- permitted; UPDATE and DELETE are actively refused with insufficient_privilege
-- (SQLSTATE 42501) by a BEFORE UPDATE OR DELETE trigger. Row-Level Security is
-- enabled with INSERT + visibility policies so existing rows remain visible to
-- a mutation attempt (otherwise RLS would silently filter them and an UPDATE
-- would report "UPDATE 0" instead of failing). The trigger fires even for a
-- role holding UPDATE/DELETE table privileges. This implements the 7-year
-- immutable audit trail required by Requirements 2.7 and 19.3 and the design's
-- security spec ("Audit Log Immutability").
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
    'Immutable append-only audit log. INSERT is permitted; UPDATE and DELETE are '
    'refused with insufficient_privilege (42501) by the trg_audit_log_immutable '
    'trigger, so rows cannot be modified or removed. 7-year retention; Protected B.';

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
-- Immutability enforcement: append-only audit_log (Req 2.7, 19.3)
-- ---------------------------------------------------------------------------
-- The ei_ads_app role is the application database user used by all EI-ADS
-- services. The audit_log must be APPEND-ONLY: INSERT is permitted, but any
-- UPDATE or DELETE MUST be actively refused with a permission error.
--
-- IMPORTANT — Postgres RLS semantics (why an INSERT-only policy is NOT enough):
--   With RLS enabled and only a FOR INSERT policy, an UPDATE or DELETE does not
--   raise an error — it simply finds NO rows visible to modify and reports
--   "UPDATE 0" / "DELETE 0". That is silent tampering-tolerance, not
--   immutability: a caller (or attacker) receives a SUCCESS response while
--   believing they altered the audit history. For a Protected B audit trail
--   that is unacceptable.
--
-- Correct enforcement (verified against PostgreSQL 16):
--   1. Enable RLS with permissive INSERT + SELECT policies so the application
--      can append and read audit rows, and so existing rows ARE visible to an
--      UPDATE/DELETE attempt (rather than being silently filtered to zero).
--   2. A BEFORE UPDATE OR DELETE trigger raises insufficient_privilege (42501)
--      for every affected row, so the mutation is REFUSED — regardless of any
--      table-level UPDATE/DELETE GRANT the role may hold. Superusers bypass RLS
--      but the trigger still fires, so even administrative UPDATE/DELETE is
--      blocked; intentional corrections must DROP the trigger deliberately.
-- ---------------------------------------------------------------------------

ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;

-- Permissive INSERT policy: the application may append new audit records.
DROP POLICY IF EXISTS audit_log_insert_only ON audit_log;
CREATE POLICY audit_log_insert_only
    ON audit_log
    FOR INSERT
    WITH CHECK (true);

-- Permissive SELECT policy: audit rows are readable.
DROP POLICY IF EXISTS audit_log_select ON audit_log;
CREATE POLICY audit_log_select
    ON audit_log
    FOR SELECT
    USING (true);

-- Permissive UPDATE/DELETE VISIBILITY policies. These do NOT authorise
-- mutation — they exist so that an UPDATE/DELETE attempt SEES the existing rows
-- (USING (true)) instead of RLS silently filtering them to zero ("UPDATE 0").
-- With the rows visible, the BEFORE UPDATE OR DELETE trigger below fires and
-- refuses the operation with 42501. Without these policies the trigger never
-- runs and tampering appears to "succeed" against zero rows.
DROP POLICY IF EXISTS audit_log_update_visible ON audit_log;
CREATE POLICY audit_log_update_visible
    ON audit_log
    FOR UPDATE
    USING (true);

DROP POLICY IF EXISTS audit_log_delete_visible ON audit_log;
CREATE POLICY audit_log_delete_visible
    ON audit_log
    FOR DELETE
    USING (true);

-- The immutability trigger: refuse every UPDATE/DELETE with 42501.
CREATE OR REPLACE FUNCTION audit_log_reject_mutation()
    RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'audit_log is append-only and immutable; % is not permitted', TG_OP
        USING ERRCODE = '42501';
END;
$$;

COMMENT ON FUNCTION audit_log_reject_mutation() IS
    'Enforces audit_log immutability (Req 2.7, 19.3): raises insufficient_privilege '
    '(42501) on any UPDATE or DELETE so the append-only audit trail cannot be tampered '
    'with, even by a role holding UPDATE/DELETE table privileges.';

DROP TRIGGER IF EXISTS trg_audit_log_immutable ON audit_log;
CREATE TRIGGER trg_audit_log_immutable
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW
    EXECUTE FUNCTION audit_log_reject_mutation();

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
