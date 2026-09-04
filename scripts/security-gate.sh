#!/usr/bin/env bash
#
# EI-ADS Final Security Gate — Zero-CVE sign-off before OpenShift deployment
# (task 40.1, Requirement 19.6).
#
# This is the LAST line of defence before a release is deployed. It runs the four
# supply-chain security checks IN ORDER and, on success, writes a signed-off
# deployment-readiness report. It fails fast: the FIRST check that finds a
# HIGH/CRITICAL CVE (that is not a documented, unexpired exception) aborts the
# whole run with a non-zero exit code, and NO report is produced.
#
#   1. pip-audit  — every Python lock file (requirements.lock + the per-service
#                   requirements-*.txt manifests under scripts/dependencies/).
#   2. npm audit  — the React frontend (services/claimant-portal), --audit-level=high.
#   3. Grype      — every per-service CycloneDX SBOM: sbom/<name>-<sha>.cyclonedx.json.
#   4. Grype      — the aggregate release SBOM: sbom/ei-ads-<sha>-aggregate.cyclonedx.json.
#
# MANDATORY PRECONDITION FOR THE FINAL DEPLOYMENT CHECKPOINT
# ----------------------------------------------------------
# This gate is a MANDATORY pre-condition for the task 37 final deployment
# checkpoint (Zero-CVE sign-off before OpenShift deployment). The checkpoint
# SHALL NOT proceed if this script exits non-zero. Deployment automation MUST
# invoke `scripts/security-gate.sh` and treat any non-zero exit as a hard stop:
# no image is deployed, no release is promoted.
#
# On success this script writes:
#   docs/security/deployment-readiness-<GIT_SHA>.md
# summarising: total packages scanned, zero-CVE confirmation (or the documented
# exceptions with their issue references), the SBOM digest per service, and the
# sign-off date. That report is the artifact the deployment checkpoint consumes.
#
# TOOL AVAILABILITY (CI vs sandbox)
# ---------------------------------
# The gate needs pip-audit (2.7.3), npm, and grype (0.87.0). In CI these are
# REQUIRED — a missing tool is a hard failure (REQUIRE_TOOLS=true, defaulted from
# CI=true) so the gate can never be silently skipped before a real deployment.
# In a local sandbox where a tool is absent, the gate records the requirement in
# the report and treats that specific check as UNVERIFIED rather than passed —
# and, because an unverified gate cannot sign off, the overall run still exits
# non-zero unless ALLOW_UNVERIFIED=true is explicitly set for local iteration.
#
# Make it executable once with:   chmod +x scripts/security-gate.sh
# Then run from anywhere:         scripts/security-gate.sh
#
# Environment variables:
#   PIP_AUDIT          pip-audit binary / override     (default: pip-audit)
#   NPM                npm binary / override           (default: npm)
#   GRYPE              grype binary / override         (default: grype)
#   SBOM_DIR           SBOM directory                  (default: <root>/sbom)
#   REQUIRE_TOOLS      Missing tool is fatal           (default: ${CI:-false})
#   ALLOW_UNVERIFIED   Allow sign-off with skipped     (default: false)
#                      checks (local sandbox only)
#
# Requirements: 19.6
#
set -euo pipefail

# --- Locate the monorepo root -----------------------------------------------
# This script lives in <root>/scripts, so the repo root is one level up. We do
# not rely on the caller's CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

PIP_AUDIT="${PIP_AUDIT:-pip-audit}"
NPM="${NPM:-npm}"
GRYPE="${GRYPE:-grype}"
SBOM_DIR="${SBOM_DIR:-${REPO_ROOT}/sbom}"
REQUIRE_TOOLS="${REQUIRE_TOOLS:-${CI:-false}}"
ALLOW_UNVERIFIED="${ALLOW_UNVERIFIED:-false}"

# Pinned tool versions (single source of truth is
# scripts/dependencies/python-versions.toml — echoed here for the report).
PIP_AUDIT_VERSION="2.7.3"
GRYPE_VERSION="0.87.0"

# Paths reused from earlier tasks.
FRONTEND_DIR="services/claimant-portal"
DEP_DIR="scripts/dependencies"
EXCEPTIONS_FILE="${DEP_DIR}/EXCEPTIONS.md"
SAFETY_POLICY="${DEP_DIR}/safety-policy.yml"
SECURITY_DOCS_DIR="${REPO_ROOT}/docs/security"

# --- Preconditions ----------------------------------------------------------
if ! command -v git >/dev/null 2>&1; then
  echo "ERROR: git is required to resolve the release SHA for the report." >&2
  exit 1
fi

GIT_SHA="$(git rev-parse --short HEAD)"
if [[ -z "${GIT_SHA}" ]]; then
  echo "ERROR: could not resolve the current Git SHA (git rev-parse HEAD)." >&2
  exit 1
fi

REPORT_FILE="${SECURITY_DOCS_DIR}/deployment-readiness-${GIT_SHA}.md"

echo "==> EI-ADS final security gate (Zero-CVE sign-off)"
echo "    repo root      : ${REPO_ROOT}"
echo "    git sha        : ${GIT_SHA}"
echo "    require tools  : ${REQUIRE_TOOLS}"
echo "    report target  : docs/security/deployment-readiness-${GIT_SHA}.md"
echo

# --- Accumulators for the report --------------------------------------------
# Each check appends a human-readable line describing its result. We track the
# number of packages scanned and whether any check was left UNVERIFIED (tool
# missing) so the sign-off logic can refuse to sign off an incomplete gate.
declare -a CHECK_RESULTS=()
declare -a SERVICE_SBOM_DIGESTS=()
TOTAL_PACKAGES=0
UNVERIFIED=0

# tool_missing <tool-name> <install-hint>
# Returns 0 (caller should skip) after either aborting (REQUIRE_TOOLS=true) or
# recording the check as unverified (sandbox). Uses the global UNVERIFIED flag.
tool_missing() {
  local tool="$1" hint="$2"
  if [[ "${REQUIRE_TOOLS}" == "true" ]]; then
    echo "ERROR: required tool '${tool}' not found on PATH. ${hint}" >&2
    echo "       The security gate cannot sign off without it (REQUIRE_TOOLS=true)." >&2
    exit 1
  fi
  echo "    WARN: '${tool}' not found — check UNVERIFIED in this sandbox. ${hint}" >&2
  UNVERIFIED=1
}

# count_packages <file> — number of pinned requirement lines (non-comment,
# non-blank) in a pip requirements file. Best-effort, used only for the report.
count_packages() {
  local file="$1"
  grep -cE '^[[:space:]]*[A-Za-z0-9]' "${file}" 2>/dev/null || echo 0
}

# --------------------------------------------------------------------------
# Check 1 — pip-audit on every Python lock file
# --------------------------------------------------------------------------
# We audit the canonical aggregate lock file (requirements.lock) plus every
# per-service manifest under scripts/dependencies/. pip-audit --strict makes any
# audit error fatal; --vulnerability-service osv matches the pre-commit hook and
# CI gate (task 38.1). A non-zero exit from pip-audit aborts the run via set -e.
echo "==> [1/4] pip-audit — Python lock files (pip-audit ${PIP_AUDIT_VERSION})"

PY_LOCK_FILES=()
[[ -f "${REPO_ROOT}/requirements.lock" ]] && PY_LOCK_FILES+=("requirements.lock")
while IFS= read -r req; do
  [[ -n "${req}" ]] && PY_LOCK_FILES+=("${req}")
done < <(find "${DEP_DIR}" -maxdepth 1 -name 'requirements-*.txt' | sort)

if (( ${#PY_LOCK_FILES[@]} == 0 )); then
  echo "ERROR: no Python lock files found to audit (requirements.lock / ${DEP_DIR}/requirements-*.txt)." >&2
  exit 1
fi

if ! command -v "${PIP_AUDIT}" >/dev/null 2>&1; then
  tool_missing "${PIP_AUDIT}" "Install pip-audit==${PIP_AUDIT_VERSION} (pip install pip-audit==${PIP_AUDIT_VERSION})."
  CHECK_RESULTS+=("1. pip-audit (Python lock files): UNVERIFIED — pip-audit not installed in this environment; REQUIRED in CI.")
else
  py_pkgs=0
  for lock in "${PY_LOCK_FILES[@]}"; do
    n="$(count_packages "${lock}")"
    py_pkgs=$(( py_pkgs + n ))
    echo "    audit: ${lock} (${n} pinned packages)"
    # --strict: any audit error is fatal. Non-zero exit (CVE found) aborts via set -e.
    "${PIP_AUDIT}" --strict --vulnerability-service osv --requirement "${lock}"
  done
  TOTAL_PACKAGES=$(( TOTAL_PACKAGES + py_pkgs ))
  echo "    pip-audit: PASS — no HIGH/CRITICAL CVEs across ${#PY_LOCK_FILES[@]} lock file(s), ${py_pkgs} pinned packages"
  CHECK_RESULTS+=("1. pip-audit (Python lock files): PASS — ${#PY_LOCK_FILES[@]} lock file(s), ${py_pkgs} pinned packages, zero HIGH/CRITICAL CVEs.")
fi
echo

# --------------------------------------------------------------------------
# Check 2 — npm audit on the React frontend
# --------------------------------------------------------------------------
# --audit-level=high fails (non-zero exit) on any HIGH/CRITICAL advisory. Any
# advisory that must be tolerated is recorded in EXCEPTIONS.md with an issue ref
# and a <=30-day remediation date (task 26.1 already documented two dev-only
# exceptions there). --prefix scopes the audit to the frontend package.
echo "==> [2/4] npm audit — React frontend (${FRONTEND_DIR}, --audit-level=high)"

if [[ ! -f "${FRONTEND_DIR}/package.json" ]]; then
  echo "ERROR: ${FRONTEND_DIR}/package.json not found; cannot audit the frontend." >&2
  exit 1
fi

if ! command -v "${NPM}" >/dev/null 2>&1; then
  tool_missing "${NPM}" "Install Node.js/npm to audit the frontend."
  CHECK_RESULTS+=("2. npm audit (frontend): UNVERIFIED — npm not installed in this environment; REQUIRED in CI.")
else
  echo "    audit: npm audit --audit-level=high --prefix ${FRONTEND_DIR}"
  # A non-zero exit (HIGH/CRITICAL advisory not covered by npm's own overrides)
  # aborts the run via set -e. Documented exceptions live in EXCEPTIONS.md.
  "${NPM}" audit --audit-level=high --prefix "${FRONTEND_DIR}"
  echo "    npm audit: PASS — no unresolved HIGH/CRITICAL advisories"
  CHECK_RESULTS+=("2. npm audit (frontend): PASS — no unresolved HIGH/CRITICAL advisories (documented exceptions: see EXCEPTIONS.md).")
fi
echo

# --------------------------------------------------------------------------
# Check 3 — Grype on every per-service CycloneDX SBOM
# --------------------------------------------------------------------------
# Scans each sbom/<name>-<sha>.cyclonedx.json produced by build-images.sh (39.1)
# against the OSV database with --fail-on high. We also compute each SBOM's
# SHA-256 digest for the report's per-service digest table. Missing SBOMs are a
# hard failure in CI (the release cannot be signed off without them) and an
# UNVERIFIED skip in a sandbox where no images were built.
echo "==> [3/4] Grype — per-service CycloneDX SBOMs (grype ${GRYPE_VERSION}, --fail-on high)"

PER_SERVICE_SBOMS=()
if [[ -d "${SBOM_DIR}" ]]; then
  while IFS= read -r sbom; do
    [[ -n "${sbom}" ]] && PER_SERVICE_SBOMS+=("${sbom}")
  done < <(find "${SBOM_DIR}" -maxdepth 1 -name "*-${GIT_SHA}.cyclonedx.json" \
             ! -name "ei-ads-${GIT_SHA}-aggregate.cyclonedx.json" | sort)
fi

# Portable SHA-256 helper (coreutils sha256sum, else macOS shasum).
sha256_of() {
  local file="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "${file}" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "${file}" | awk '{print $1}'
  else
    echo "unavailable"
  fi
}

if (( ${#PER_SERVICE_SBOMS[@]} == 0 )); then
  if [[ "${REQUIRE_TOOLS}" == "true" ]]; then
    echo "ERROR: no per-service SBOMs (sbom/*-${GIT_SHA}.cyclonedx.json) found for this SHA." >&2
    echo "       Build images first (scripts/build-images.sh) so SBOMs exist to scan." >&2
    exit 1
  fi
  echo "    WARN: no per-service SBOMs for ${GIT_SHA} — check UNVERIFIED in this sandbox." >&2
  UNVERIFIED=1
  CHECK_RESULTS+=("3. Grype (per-service SBOMs): UNVERIFIED — no sbom/*-${GIT_SHA}.cyclonedx.json present; REQUIRED in CI.")
elif ! command -v "${GRYPE}" >/dev/null 2>&1; then
  tool_missing "${GRYPE}" "Install Grype ${GRYPE_VERSION} (see sbom/README.md)."
  CHECK_RESULTS+=("3. Grype (per-service SBOMs): UNVERIFIED — grype not installed in this environment; REQUIRED in CI.")
else
  for sbom in "${PER_SERVICE_SBOMS[@]}"; do
    rel="${sbom#"${REPO_ROOT}/"}"
    base="$(basename "${sbom}")"
    # sbom/<name>-<sha>.cyclonedx.json -> <name>
    svc="${base%-"${GIT_SHA}".cyclonedx.json}"
    digest="sha256:$(sha256_of "${sbom}")"
    echo "    scan: ${rel} (${svc}) digest ${digest}"
    "${GRYPE}" "sbom:${sbom}" --fail-on high
    SERVICE_SBOM_DIGESTS+=("| ${svc} | \`${rel}\` | \`${digest}\` |")
  done
  echo "    Grype (per-service): PASS — no HIGH/CRITICAL CVEs across ${#PER_SERVICE_SBOMS[@]} SBOM(s)"
  CHECK_RESULTS+=("3. Grype (per-service SBOMs): PASS — ${#PER_SERVICE_SBOMS[@]} SBOM(s) scanned, zero HIGH/CRITICAL CVEs.")
fi
echo

# --------------------------------------------------------------------------
# Check 4 — Grype on the aggregate release SBOM
# --------------------------------------------------------------------------
# The merged, release-wide SBOM (task 39.5) is the authoritative supply-chain
# artifact for the GC SA&A package. Scanning it with --fail-on high is the final
# whole-release vulnerability check before sign-off.
echo "==> [4/4] Grype — aggregate release SBOM (grype ${GRYPE_VERSION}, --fail-on high)"

AGGREGATE_SBOM="${SBOM_DIR}/ei-ads-${GIT_SHA}-aggregate.cyclonedx.json"
AGGREGATE_DIGEST="n/a"

if [[ ! -f "${AGGREGATE_SBOM}" ]]; then
  if [[ "${REQUIRE_TOOLS}" == "true" ]]; then
    echo "ERROR: aggregate SBOM ${AGGREGATE_SBOM} not found for ${GIT_SHA}." >&2
    echo "       Generate it first (scripts/build-images.sh + the 39.5 merge step)." >&2
    exit 1
  fi
  echo "    WARN: aggregate SBOM for ${GIT_SHA} missing — check UNVERIFIED in this sandbox." >&2
  UNVERIFIED=1
  CHECK_RESULTS+=("4. Grype (aggregate SBOM): UNVERIFIED — sbom/ei-ads-${GIT_SHA}-aggregate.cyclonedx.json missing; REQUIRED in CI.")
elif ! command -v "${GRYPE}" >/dev/null 2>&1; then
  tool_missing "${GRYPE}" "Install Grype ${GRYPE_VERSION} (see sbom/README.md)."
  CHECK_RESULTS+=("4. Grype (aggregate SBOM): UNVERIFIED — grype not installed in this environment; REQUIRED in CI.")
else
  AGGREGATE_DIGEST="sha256:$(sha256_of "${AGGREGATE_SBOM}")"
  echo "    scan: ${AGGREGATE_SBOM#"${REPO_ROOT}/"} digest ${AGGREGATE_DIGEST}"
  "${GRYPE}" "sbom:${AGGREGATE_SBOM}" --fail-on high
  echo "    Grype (aggregate): PASS — no HIGH/CRITICAL CVEs in the release-wide SBOM"
  CHECK_RESULTS+=("4. Grype (aggregate SBOM): PASS — release-wide SBOM \`${AGGREGATE_SBOM#"${REPO_ROOT}/"}\` (digest \`${AGGREGATE_DIGEST}\`), zero HIGH/CRITICAL CVEs.")
fi
echo

# --------------------------------------------------------------------------
# Sign-off decision
# --------------------------------------------------------------------------
# We reach this point only if every gating scan that DID run passed (a failing
# scan already aborted via set -e). If any check was left UNVERIFIED (a tool or
# artifact was missing in a sandbox), the gate refuses to sign off — and thus
# exits non-zero so the deployment checkpoint cannot proceed — unless the
# operator explicitly sets ALLOW_UNVERIFIED=true for local iteration.
if (( UNVERIFIED == 1 )) && [[ "${ALLOW_UNVERIFIED}" != "true" ]]; then
  echo "ERROR: one or more checks were UNVERIFIED (missing tool or SBOM artifact)." >&2
  echo "       The security gate will NOT sign off and NO deployment-readiness report" >&2
  echo "       was written. Install the required tooling / build the SBOMs and re-run," >&2
  echo "       or set ALLOW_UNVERIFIED=true for local iteration only (NOT for deployment)." >&2
  exit 1
fi

# --- Write the deployment-readiness report ----------------------------------
mkdir -p "${SECURITY_DOCS_DIR}"

SIGN_OFF_DATE="$(date -u +%Y-%m-%d)"
SIGN_OFF_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Assemble the per-service SBOM digest table body (or a placeholder row).
if (( ${#SERVICE_SBOM_DIGESTS[@]} == 0 )); then
  SBOM_TABLE_BODY="| _(none scanned in this environment)_ | | |"
else
  SBOM_TABLE_BODY="$(printf '%s\n' "${SERVICE_SBOM_DIGESTS[@]}")"
fi

# Assemble the per-check results list.
CHECK_LIST=""
for line in "${CHECK_RESULTS[@]}"; do
  CHECK_LIST+="- ${line}"$'\n'
done

{
  echo "# EI-ADS Deployment Readiness Report — Zero-CVE Sign-off"
  echo
  echo "**Release Git SHA:** \`${GIT_SHA}\`  "
  echo "**Sign-off date:** ${SIGN_OFF_DATE}  "
  echo "**Generated (UTC):** ${SIGN_OFF_TS}  "
  echo "**Generated by:** \`scripts/security-gate.sh\` (task 40.1, Requirement 19.6)"
  echo
  echo "## Gate result"
  echo
  if (( UNVERIFIED == 1 )); then
    echo "> STATUS: **SIGNED OFF WITH UNVERIFIED CHECKS** (ALLOW_UNVERIFIED=true)."
    echo "> One or more checks could not run in this environment. This report MUST NOT"
    echo "> be used to authorise a production deployment; re-run in CI with all tooling"
    echo "> present so every check is verified."
  else
    echo "> STATUS: **PASS — cleared for OpenShift deployment.**"
    echo "> All four supply-chain security checks completed with zero HIGH/CRITICAL CVEs"
    echo "> (or only documented, unexpired exceptions)."
  fi
  echo
  echo "This report is a **mandatory pre-condition for the final deployment checkpoint**"
  echo "(task 37). The checkpoint SHALL NOT proceed unless \`scripts/security-gate.sh\`"
  echo "exits zero and this report is present for the release SHA."
  echo
  echo "## Checks run (in order)"
  echo
  echo "${CHECK_LIST}"
  echo "## Packages scanned"
  echo
  echo "- **Total Python packages scanned (pinned requirement lines):** ${TOTAL_PACKAGES}"
  echo "- **Python lock files audited:** ${#PY_LOCK_FILES[@]}"
  echo "- **Per-service SBOMs scanned:** ${#PER_SERVICE_SBOMS[@]}"
  echo "- **Aggregate SBOM scanned:** $([[ "${AGGREGATE_DIGEST}" != "n/a" ]] && echo "yes" || echo "no")"
  echo
  echo "## Zero-CVE confirmation"
  echo
  echo "No dependency ships a CVE with CVSS >= 7.0 (HIGH/CRITICAL), except the"
  echo "following documented, tracked exceptions. Each exception carries a GitHub"
  echo "issue reference and a remediation date no more than 30 days from acceptance"
  echo "(see \`${EXCEPTIONS_FILE}\` and \`${SAFETY_POLICY}\`)."
  echo
  if [[ -f "${EXCEPTIONS_FILE}" ]]; then
    # Inline the Active Exceptions table from EXCEPTIONS.md so the report is
    # self-contained for the SA&A reviewer. We copy the documented table rows.
    echo "### Documented exceptions (from \`${EXCEPTIONS_FILE}\`)"
    echo
    awk '/^## Active Exceptions/{f=1;next} /^## Format/{f=0} f' "${EXCEPTIONS_FILE}"
    echo
  else
    echo "_No exceptions file found; the zero-CVE policy is enforced with no exceptions._"
    echo
  fi
  echo "## SBOM digest per service"
  echo
  echo "| Service | CycloneDX SBOM | SHA-256 digest |"
  echo "|---------|----------------|----------------|"
  echo "${SBOM_TABLE_BODY}"
  echo
  echo "**Aggregate release SBOM:** \`sbom/ei-ads-${GIT_SHA}-aggregate.cyclonedx.json\` — digest \`${AGGREGATE_DIGEST}\`"
  echo
  echo "## Sign-off"
  echo
  echo "- **Automated gate:** \`scripts/security-gate.sh\` exited 0 for SHA \`${GIT_SHA}\` on ${SIGN_OFF_DATE}."
  echo "- **Human sign-off:** _______________________  (name / role / date)"
  echo
  echo "---"
  echo "_This report is generated automatically. Do not edit by hand; re-run the gate to regenerate._"
} > "${REPORT_FILE}"

echo "==> Security gate PASSED."
echo "    wrote report: docs/security/deployment-readiness-${GIT_SHA}.md"
echo "    This report is the mandatory pre-condition for the task 37 deployment checkpoint."
