-- =============================================================================
-- Migration 001: Create claims table
-- =============================================================================
-- Requirements: 1.4, 2.6, 2.7, 19.3
--
-- Idempotent: safe to run multiple times (CREATE TABLE IF NOT EXISTS,
-- CREATE INDEX IF NOT EXISTS, CREATE OR REPLACE FUNCTION).
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Table: claims
-- ---------------------------------------------------------------------------
-- Core transactional table for EI claim records.
-- Protected B classification: AES-256 at rest; SIN is never stored in plaintext —
-- only the SHA-256 hex digest (sin_hash) is persisted (Requirement 1.11).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS claims (
    claim_id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    sin_hash                CHAR(64)    NOT NULL,                    -- SHA-256 hex digest of SIN + per-env salt
    employer_bn             VARCHAR(15) NOT NULL,                    -- 9-digit BN + RT + 4-digit account
    insurable_hours         NUMERIC(10,2) NOT NULL CHECK (insurable_hours > 0),
    insurable_earnings      NUMERIC(12,2) NOT NULL CHECK (insurable_earnings > 0),
    separation_date         DATE        NOT NULL,
    separation_reason       TEXT        NOT NULL,
    separation_reason_code  CHAR(1),                                 -- ROE reason code A–N
    claim_start_date        DATE,
    economic_region_code    VARCHAR(2),                              -- ESDC 2-char economic region code
    noc_code                VARCHAR(5),                              -- NOC 2021 5-digit code
    residential_postal_code VARCHAR(3),                              -- FSA only (first 3 chars of postal code)
    residential_address_hash CHAR(64),                              -- SHA-256 of full residential address
    employer_work_location  TEXT,
    status                  VARCHAR(20) NOT NULL DEFAULT 'RECEIVED'
                                CHECK (status IN (
                                    'RECEIVED',
                                    'PROCESSING',
                                    'SCORED',
                                    'INVESTIGATE',
                                    'APPROVED',
                                    'REJECTED'
                                )),
    roe_document_id         UUID,
    language                CHAR(2)     NOT NULL DEFAULT 'en'
                                CHECK (language IN ('en', 'fr')),
    protected_b             BOOLEAN     NOT NULL DEFAULT TRUE,
    received_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE claims IS
    'Core EI claim records. Protected B. SIN is stored only as a SHA-256 digest.';

COMMENT ON COLUMN claims.sin_hash IS
    'SHA-256 hex digest of the claimant SIN concatenated with the per-environment salt. Never store plaintext SIN.';
COMMENT ON COLUMN claims.employer_bn IS
    '15-character employer Business Number: 9-digit root BN + RT + 4-digit program account.';
COMMENT ON COLUMN claims.residential_postal_code IS
    'Forward Sortation Area only (first 3 characters of the full postal code). Full address is stored as a hash.';
COMMENT ON COLUMN claims.status IS
    'Claim lifecycle state: RECEIVED → PROCESSING → SCORED → INVESTIGATE / APPROVED / REJECTED.';
COMMENT ON COLUMN claims.protected_b IS
    'Always TRUE; records that this row is classified Protected B under the Government of Canada security taxonomy.';

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_claims_sin_hash
    ON claims (sin_hash);

CREATE INDEX IF NOT EXISTS idx_claims_employer_bn
    ON claims (employer_bn);

CREATE INDEX IF NOT EXISTS idx_claims_status
    ON claims (status);

CREATE INDEX IF NOT EXISTS idx_claims_separation_date
    ON claims (separation_date);

CREATE INDEX IF NOT EXISTS idx_claims_received_at
    ON claims (received_at DESC);

-- ---------------------------------------------------------------------------
-- Trigger: keep updated_at current on every UPDATE
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION set_updated_at()
    RETURNS TRIGGER
    LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS update_claims_updated_at ON claims;

CREATE TRIGGER update_claims_updated_at
    BEFORE UPDATE ON claims
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();
