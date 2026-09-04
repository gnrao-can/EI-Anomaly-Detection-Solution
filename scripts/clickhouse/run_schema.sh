#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CLICKHOUSE_HOST="${CLICKHOUSE_HOST:-localhost}"
CLICKHOUSE_PORT="${CLICKHOUSE_PORT:-8123}"
CLICKHOUSE_USER="${CLICKHOUSE_USER:-ei_ads_app}"
CLICKHOUSE_PASSWORD="${CLICKHOUSE_PASSWORD:-changeme_local_only}"

SCRIPTS=(
  "001_create_claims_analytics.sql"
  "002_create_indicators_analytics.sql"
  "003_create_risk_assessments_analytics.sql"
  "004_create_shap_attributions.sql"
  "005_create_adapter_audit_log.sql"
)

echo "==> EI-ADS ClickHouse schema setup"
echo "    Host: ${CLICKHOUSE_HOST}:${CLICKHOUSE_PORT}"

for script in "${SCRIPTS[@]}"; do
  echo "  Applying ${script} ..."
  curl -sS --fail \
    "http://${CLICKHOUSE_HOST}:${CLICKHOUSE_PORT}/" \
    --user "${CLICKHOUSE_USER}:${CLICKHOUSE_PASSWORD}" \
    --data-binary @"${SCRIPT_DIR}/${script}"
  echo "  Done: ${script}"
done

echo "==> All ClickHouse schemas applied successfully."
