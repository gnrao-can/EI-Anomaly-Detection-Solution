#!/usr/bin/env bash
#
# scripts/dev/start-stack.sh
# ==========================
# One-command local bring-up for the EI-ADS podman-compose stack on macOS +
# Podman (applehv). Codifies the manual "Bring-up recipe" from
# docs/regeneration/STATUS.md so the whole solution launches with a single
# command and needs NO manual restart afterwards.
#
# What it does, in order:
#   1. Pin the applehv machine provider and ensure the podman machine is running.
#   2. Mint the LOCAL-DEV JWKS + INVESTIGATOR service token (.env.dev-token).
#   3. Force-materialise the build context so Google Drive File Stream has every
#      file locally (avoids intermittent "input/output error" mid-COPY builds).
#   4. Build every service image sequentially (parallel builds OOM the 8 GiB VM).
#      Skipped with --no-build once images already exist.
#   5. Bring the stack up (podman-compose --in-pod false up -d).
#   6. Wait for readiness: Kafka healthy + topics created (kafka-init) + the
#      orchestrator's consumer group attached, then a short heavy-engine warm-up.
#
# Usage:
#   scripts/dev/start-stack.sh            # full: build + up + wait
#   scripts/dev/start-stack.sh --no-build # skip the image build (images present)
#   scripts/dev/start-stack.sh --down     # tear the stack down and exit
#
# After it finishes, drive the pipeline with the commands in STATUS.md
# ("Drive the pipeline" / "LLM Service walkthrough"), e.g. mint a CLAIMANT token
# with scripts/dev/mint-token.py and POST a claim to claim-intake:8001.
#
# This is a LOCAL-DEV convenience script. It never sets DOCKER_HOST (podman uses
# its default connection) and never publishes extra host ports (only the proxy
# on 8090 and postgres on 5432 are published by the compose).

set -euo pipefail

# --- Locate the repo root (this script lives in <root>/scripts/dev) ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

# --- Config ------------------------------------------------------------------
export CONTAINERS_MACHINE_PROVIDER="${CONTAINERS_MACHINE_PROVIDER:-applehv}"
# A forwarded DOCKER_HOST makes podman operate on a different view than the
# default connection (teardowns then miss the real containers). Never use it.
unset DOCKER_HOST 2>/dev/null || true

MACHINE_NAME="${MACHINE_NAME:-podman-machine-default}"
IMAGE_PREFIX="ei-anomaly-detection-solution"
NETWORK="${IMAGE_PREFIX}_default"
COMPOSE_UP=(podman-compose --in-pod false up -d)

# Services that talk to Kafka and self-heal via shared/retry on a cold start.
KAFKA_SERVICES=(ei-ads-claim-intake ei-ads-roe-adapter ei-ads-pipeline-orchestrator)

DO_BUILD=1
DO_DOWN=0
for arg in "$@"; do
  case "${arg}" in
    --no-build) DO_BUILD=0 ;;
    --down)     DO_DOWN=1 ;;
    -h|--help)
      sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown arg: ${arg} (see --help)" >&2; exit 2 ;;
  esac
done

log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mWARN: %s\033[0m\n' "$*" >&2; }

# --- 0. Preconditions --------------------------------------------------------
command -v podman >/dev/null 2>&1 || { echo "ERROR: podman not on PATH." >&2; exit 1; }
command -v podman-compose >/dev/null 2>&1 || { echo "ERROR: podman-compose not on PATH." >&2; exit 1; }

# --- --down: tear down and exit ---------------------------------------------
if [[ "${DO_DOWN}" -eq 1 ]]; then
  log "Tearing down the stack (stop + rm all containers)"
  podman stop -t 5 $(podman ps -aq) 2>/dev/null || true
  podman rm -f $(podman ps -aq) 2>/dev/null || true
  echo "Stack down."
  exit 0
fi

# --- 1. Podman machine -------------------------------------------------------
log "Ensuring the podman machine '${MACHINE_NAME}' (provider=${CONTAINERS_MACHINE_PROVIDER}) is running"
if ! podman machine list --format '{{.Name}} {{.Running}}' 2>/dev/null | grep -qiE "^${MACHINE_NAME}\*? +true"; then
  podman machine start "${MACHINE_NAME}"
else
  echo "machine already running."
fi

# --- 2. Dev auth material (JWKS + INVESTIGATOR service token) -----------------
log "Minting the local-dev JWKS + service token (.env.dev-token)"
if [[ -x .venv-core/bin/python ]]; then
  .venv-core/bin/python scripts/dev/gen-service-token.py
else
  warn ".venv-core/bin/python not found; falling back to 'python' (needs python-jose)."
  python scripts/dev/gen-service-token.py
fi

# --- 3. Force-materialise the Drive-backed build context ---------------------
# Google Drive File Stream materialises files lazily; podman racing an unsynced
# file fails a build with "input/output error" / "unexpected EOF". Reading each
# file first forces Drive to pull it local. node_modules/.venv are excluded (not
# in the image build context).
log "Materialising the build context (Google Drive sync guard)"
materialised=0
while IFS= read -r f; do
  cat "${f}" >/dev/null 2>&1 && materialised=$((materialised+1)) || warn "unreadable (Drive not synced?): ${f}"
done < <(find services shared pyproject.toml .containerignore -type f \
           \( -not -path '*/node_modules/*' -not -path '*/.venv*' -not -path '*/__pycache__/*' \) 2>/dev/null)
echo "materialised ${materialised} files."

# --- 4. Build images sequentially -------------------------------------------
if [[ "${DO_BUILD}" -eq 1 ]]; then
  log "Building service images sequentially (parallel builds OOM the VM)"
  while IFS= read -r cf; do
    svc="$(basename "$(dirname "${cf}")")"
    printf '  -- building %s ... ' "${svc}"
    if podman build -q -f "${cf}" -t "${IMAGE_PREFIX}_${svc}:latest" "${REPO_ROOT}" >/tmp/eiads-build-"${svc}".log 2>&1; then
      echo "OK"
    else
      echo "FAILED"
      echo "     see /tmp/eiads-build-${svc}.log (last lines):" >&2
      tail -6 "/tmp/eiads-build-${svc}.log" >&2
      echo "     If this is an 'input/output error', it is Google Drive sync — re-run the script." >&2
      echo "     If 'container exited on killed', the VM OOM'd — free memory or 'podman machine set --memory 12288'." >&2
      exit 1
    fi
  done < <(find services -mindepth 2 -maxdepth 2 -name Containerfile | sort)
else
  log "Skipping image build (--no-build)"
fi

# --- 5. Bring the stack up ---------------------------------------------------
log "Bringing the stack up: ${COMPOSE_UP[*]}"
"${COMPOSE_UP[@]}"

# --- 6. Readiness waits ------------------------------------------------------
# 6a. Kafka broker healthy.
log "Waiting for Kafka to be ready"
for i in $(seq 1 40); do
  if podman exec ei-ads-kafka /opt/bitnami/kafka/bin/kafka-topics.sh \
       --bootstrap-server localhost:9092 --list >/dev/null 2>&1; then
    echo "kafka is answering."
    break
  fi
  sleep 3
  [[ "${i}" -eq 40 ]] && warn "kafka not confirmed ready after ~120s; continuing anyway."
done

# 6b. Ensure topics exist (kafka-init is idempotent; re-run in case Kafka's log
#     was fresh). Includes claim.escalated for the INVESTIGATE path (Req 2.7).
log "Ensuring Kafka topics exist (kafka-init)"
podman rm -f ei-ads-kafka-init >/dev/null 2>&1 || true
podman-compose --in-pod false up -d kafka-init >/dev/null 2>&1 || true
sleep 6
podman exec ei-ads-kafka /opt/bitnami/kafka/bin/kafka-topics.sh \
  --bootstrap-server localhost:9092 --list 2>/dev/null | grep -E 'claim|roe' || true

# 6c. Orchestrator consumer group attached (proves the self-healing retry
#     connected it — no manual restart needed).
log "Waiting for the orchestrator to attach its Kafka consumer (self-healing retry)"
attached=0
for i in $(seq 1 30); do
  if podman exec ei-ads-kafka /opt/bitnami/kafka/bin/kafka-consumer-groups.sh \
       --bootstrap-server localhost:9092 --describe --group pipeline-orchestrator 2>/dev/null \
       | grep -q 'aiokafka'; then
    attached=1
    echo "orchestrator consumer attached (owns claim.received / roe.received partitions)."
    break
  fi
  sleep 3
done
[[ "${attached}" -eq 0 ]] && warn "orchestrator consumer not confirmed attached yet; give it another ~20s (the retry runs up to ~10 attempts)."

# 6d. Short heavy-engine warm-up. Fraud Ring (Neo4j), Document Forgery, and the
#     LLM Service build their backends lazily; a claim submitted immediately can
#     score lower that cycle. A brief pause makes the first demo warm.
log "Warming heavy engines (~25s) so the first claim scores on warm signals"
sleep 25

# --- Done --------------------------------------------------------------------
log "Stack is up. Container status:"
podman ps --format '{{.Names}}\t{{.Status}}' | sort

cat <<EOF

------------------------------------------------------------------------------
EI-ADS stack is up on network: ${NETWORK}
  * Proxy / UI:   http://localhost:8090
  * Postgres:     localhost:5432 (db=ei_ads, user=ei_ads_app)

Drive the pipeline (see docs/regeneration/STATUS.md "Bring-up recipe" step 7):
  CLAIMANT=\$(.venv-core/bin/python scripts/dev/mint-token.py --role CLAIMANT --ttl 3600)
  run() { podman run --rm --network ${NETWORK} docker.io/curlimages/curl:8.10.1 "\$@"; }
  # submit a claim (SIN must be Luhn-valid, e.g. 193456787) then a matching ROE
  # with the same claimId, then read risk_assessments in Postgres.

Investigator dashboard cookie:
  .venv-core/bin/python scripts/dev/mint-session-cookie.py --role INVESTIGATOR

Tear down:
  scripts/dev/start-stack.sh --down
------------------------------------------------------------------------------
EOF
