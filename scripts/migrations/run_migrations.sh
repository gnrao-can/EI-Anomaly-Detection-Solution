#!/usr/bin/env bash
# =============================================================================
# run_migrations.sh — Apply all PostgreSQL migration scripts in order
# =============================================================================
# Usage:
#   DATABASE_URL=postgresql://user:pass@host:5432/dbname ./scripts/migrations/run_migrations.sh
#
# The DATABASE_URL environment variable must be set before running this script.
# All four migrations are applied sequentially; the script exits immediately
# on the first failure (set -e) so that dependent migrations are never run
# against a partially-migrated schema.
#
# Requirements: 1.4, 2.6, 2.7, 19.3
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Validate environment
# ---------------------------------------------------------------------------
if [[ -z "${DATABASE_URL:-}" ]]; then
    echo "ERROR: DATABASE_URL environment variable is not set." >&2
    echo "       Export it before running this script:" >&2
    echo "       export DATABASE_URL=postgresql://user:pass@host:5432/dbname" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Migration files in application order
# ---------------------------------------------------------------------------
MIGRATIONS=(
    "001_create_claims.sql"
    "002_create_risk_assessments.sql"
    "003_create_indicators.sql"
    "004_create_audit_log.sql"
)

# ---------------------------------------------------------------------------
# Apply each migration
# ---------------------------------------------------------------------------
echo "==> EI-ADS PostgreSQL migrations"
echo "    Target: ${DATABASE_URL%%@*}@****"   # Mask credentials in log output
echo ""

for migration in "${MIGRATIONS[@]}"; do
    migration_path="${SCRIPT_DIR}/${migration}"

    if [[ ! -f "${migration_path}" ]]; then
        echo "ERROR: Migration file not found: ${migration_path}" >&2
        exit 1
    fi

    echo "  Applying ${migration} ..."
    psql "${DATABASE_URL}" \
        --no-password \
        --single-transaction \
        --set ON_ERROR_STOP=1 \
        --file "${migration_path}"
    echo "  Done: ${migration}"
    echo ""
done

echo "==> All migrations applied successfully."
