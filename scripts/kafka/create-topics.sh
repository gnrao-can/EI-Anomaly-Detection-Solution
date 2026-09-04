#!/usr/bin/env bash
#
# EI-ADS Kafka topic provisioning script.
#
# Reads scripts/kafka/topics.json and creates every topic idempotently using
# kafka-topics.sh (Bitnami / Apache Kafka distribution). Safe to re-run: the
# --if-not-exists flag means existing topics are left untouched.
#
# REQUIRES: jq (https://jqlang.github.io/jq/) for parsing topics.json (always
#           runs on the host), and the kafka-topics.sh CLI (Bitnami image ships
#           it at /opt/bitnami/kafka/bin/kafka-topics.sh). The CLI can be run
#           either from the host PATH or routed into a running container (see
#           KAFKA_CONTAINER below) so the two tools need not co-exist anywhere.
#
# Environment variables:
#   KAFKA_BOOTSTRAP_SERVERS  Bootstrap server(s)      (default: localhost:9092)
#   KAFKA_ENV                "dev" | "prod"           (default: dev)
#   KAFKA_TOPICS_BIN         kafka-topics.sh binary   (default: kafka-topics.sh
#                            on PATH; when KAFKA_CONTAINER is set the default
#                            becomes /opt/bitnami/kafka/bin/kafka-topics.sh,
#                            i.e. the in-container path.)
#   KAFKA_CONTAINER          Name of a running container (e.g. ei-ads-kafka).
#                            When set, the Kafka CLI is executed inside that
#                            container via the container engine, so the host
#                            only needs jq + a container engine. When unset,
#                            behaviour is unchanged: the CLI is run from PATH.
#   CONTAINER_ENGINE         Container engine to use   (default: podman; docker
#                            also works.)
#
# In "dev", replication_factor and min.insync.replicas are forced to 1 because
# a single-broker local Kafka cannot satisfy RF=3 / min.insync.replicas=2.
# In "prod" the values from topics.json are used verbatim (RF=3, ISR=2).
#
# Local dev example (Kafka running in a Podman container named ei-ads-kafka):
#   KAFKA_ENV=dev KAFKA_CONTAINER=ei-ads-kafka bash scripts/kafka/create-topics.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOPICS_FILE="${SCRIPT_DIR}/topics.json"

KAFKA_BOOTSTRAP_SERVERS="${KAFKA_BOOTSTRAP_SERVERS:-localhost:9092}"
KAFKA_ENV="${KAFKA_ENV:-dev}"

# When KAFKA_CONTAINER is set, the Kafka CLI is routed into that container and
# the default binary is the in-container Bitnami path; otherwise it is the
# plain host PATH command. An explicit KAFKA_TOPICS_BIN always wins.
KAFKA_CONTAINER="${KAFKA_CONTAINER:-}"
CONTAINER_ENGINE="${CONTAINER_ENGINE:-podman}"
if [[ -n "${KAFKA_CONTAINER}" ]]; then
  KAFKA_TOPICS_BIN="${KAFKA_TOPICS_BIN:-/opt/bitnami/kafka/bin/kafka-topics.sh}"
else
  KAFKA_TOPICS_BIN="${KAFKA_TOPICS_BIN:-kafka-topics.sh}"
fi

# kafka_topics: single entry point for invoking the Kafka CLI. It routes the
# call either to the host binary or into a container, and in BOTH modes it
# reads its stdin from /dev/null so it can never consume the topic loop's stdin
# (a container exec with an attached stdin would otherwise drain the process
# substitution feeding the while-read loop, creating only the first topic).
kafka_topics() {
  if [[ -n "${KAFKA_CONTAINER}" ]]; then
    # No -i: the exec must not attach/consume stdin.
    "${CONTAINER_ENGINE}" exec "${KAFKA_CONTAINER}" "${KAFKA_TOPICS_BIN}" "$@" </dev/null
  else
    "${KAFKA_TOPICS_BIN}" "$@" </dev/null
  fi
}

# --- Preconditions ----------------------------------------------------------
if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: jq is required but not found on PATH." >&2
  echo "       Install jq: https://jqlang.github.io/jq/download/" >&2
  exit 1
fi

if [[ -n "${KAFKA_CONTAINER}" ]]; then
  # Container mode: jq stays on the host; the Kafka CLI runs inside the
  # container, so verify the engine exists and the container is running.
  if ! command -v "${CONTAINER_ENGINE}" >/dev/null 2>&1; then
    echo "ERROR: container engine '${CONTAINER_ENGINE}' not found on PATH." >&2
    echo "       Install it, or set CONTAINER_ENGINE (e.g. CONTAINER_ENGINE=docker)." >&2
    exit 1
  fi
  if ! "${CONTAINER_ENGINE}" inspect -f '{{.State.Running}}' "${KAFKA_CONTAINER}" 2>/dev/null | grep -q '^true$'; then
    echo "ERROR: container '${KAFKA_CONTAINER}' is not running (via ${CONTAINER_ENGINE})." >&2
    echo "       Start the Kafka container, or unset KAFKA_CONTAINER to use a host kafka-topics.sh." >&2
    exit 1
  fi
else
  # Host mode: the Kafka CLI must be available on PATH.
  if ! command -v "${KAFKA_TOPICS_BIN}" >/dev/null 2>&1; then
    echo "ERROR: '${KAFKA_TOPICS_BIN}' not found on PATH." >&2
    echo "       Set KAFKA_TOPICS_BIN to the full path (e.g. /opt/bitnami/kafka/bin/kafka-topics.sh)," >&2
    echo "       or set KAFKA_CONTAINER=<name> to run the CLI inside a running Kafka container." >&2
    exit 1
  fi
fi

if [[ ! -f "${TOPICS_FILE}" ]]; then
  echo "ERROR: topics manifest not found at ${TOPICS_FILE}" >&2
  exit 1
fi

echo "==> EI-ADS Kafka topic provisioning"
echo "    Bootstrap servers: ${KAFKA_BOOTSTRAP_SERVERS}"
echo "    Environment:       ${KAFKA_ENV}"
echo "    Manifest:          ${TOPICS_FILE}"
if [[ -n "${KAFKA_CONTAINER}" ]]; then
  echo "    Kafka CLI:         ${CONTAINER_ENGINE} exec ${KAFKA_CONTAINER} ${KAFKA_TOPICS_BIN}"
else
  echo "    Kafka CLI:         ${KAFKA_TOPICS_BIN} (host PATH)"
fi
if [[ "${KAFKA_ENV}" == "dev" ]]; then
  echo "    NOTE: dev mode -> forcing replication_factor=1 and min.insync.replicas=1"
fi
echo

created_count=0
topic_count=0

# Iterate over each topic. -c emits one compact JSON object per line.
while IFS= read -r topic_json; do
  topic_count=$((topic_count + 1))

  name="$(echo "${topic_json}"       | jq -r '.name')"
  partitions="$(echo "${topic_json}" | jq -r '.partitions')"
  rf="$(echo "${topic_json}"         | jq -r '.replication_factor')"
  retention_ms="$(echo "${topic_json}" | jq -r '.retention_ms')"
  min_isr="$(echo "${topic_json}"    | jq -r '.min_insync_replicas')"
  cleanup_policy="$(echo "${topic_json}" | jq -r '.cleanup_policy')"

  # Local single-broker Kafka cannot satisfy RF=3 / ISR=2.
  if [[ "${KAFKA_ENV}" == "dev" ]]; then
    rf=1
    min_isr=1
  fi

  echo "--> Creating topic '${name}' (partitions=${partitions}, rf=${rf}, retention.ms=${retention_ms}, min.insync.replicas=${min_isr}, cleanup.policy=${cleanup_policy})"

  # kafka_topics reads its stdin from /dev/null (see the function definition),
  # so this call cannot consume the loop's stdin from the process substitution.
  kafka_topics \
    --bootstrap-server "${KAFKA_BOOTSTRAP_SERVERS}" \
    --create \
    --if-not-exists \
    --topic "${name}" \
    --partitions "${partitions}" \
    --replication-factor "${rf}" \
    --config "retention.ms=${retention_ms}" \
    --config "min.insync.replicas=${min_isr}" \
    --config "cleanup.policy=${cleanup_policy}"

  created_count=$((created_count + 1))
  echo "    OK: ${name}"
done < <(jq -c '.topics[]' "${TOPICS_FILE}")

echo
echo "==> Done. Provisioned ${created_count}/${topic_count} topics against ${KAFKA_BOOTSTRAP_SERVERS} (env: ${KAFKA_ENV})."
