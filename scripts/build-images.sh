#!/usr/bin/env bash
#
# EI-ADS container image build script (Podman, rootless).
#
# Builds an OCI image for every service that ships a Containerfile under
# services/<name>/Containerfile. Each image is:
#
#   1. Built from the MONOREPO ROOT (build context = repo root) so that the
#      shared/ Python package is inside the build context and every service
#      can `COPY --from=builder /build/shared ./shared`. This is why the build
#      uses `-f services/<name>/Containerfile .` rather than cd-ing into the
#      service directory.
#   2. Tagged first with the short Git SHA:  ei-ads/<name>:<sha>
#   3. Smoke-verified by actually RUNNING the container and probing its HTTP
#      health endpoint (GET /healthz, or GET / for the static frontend). See
#      the note in smoke_test() below — the design's shorthand of
#      `podman run --rm ei-ads/<name>:latest /healthz` is implemented here as a
#      real readiness probe against the running service, not a literal argv.
#   4. Tagged `latest` ONLY after the smoke test passes:  ei-ads/<name>:latest
#
# The script fails fast: `set -euo pipefail` plus explicit error handling means
# a single build error, a failed smoke test, or a missing prerequisite aborts
# the whole run with a non-zero exit code. No `latest` tag is ever applied to an
# image that did not pass its readiness probe.
#
# Make it executable once with:   chmod +x scripts/build-images.sh
# Then run from anywhere:         scripts/build-images.sh
#
# Environment variables:
#   IMAGE_PREFIX      Image namespace/prefix         (default: ei-ads)
#   PROBE_TIMEOUT     Seconds to wait for readiness  (default: 30)
#   PROBE_INTERVAL    Seconds between probe attempts (default: 2)
#   PODMAN            podman binary / override        (default: podman)
#   SERVICES          Space/comma-separated list of  (default: all services
#                     service names to build. When       with a Containerfile)
#                     set, only those services are
#                     built — used by the CI matrix
#                     (35.4) to build one service per
#                     parallel job. Unknown names are
#                     an error so a typo fails fast.
#
# Requirements: 17.5
#
set -euo pipefail

# --- Locate the monorepo root -----------------------------------------------
# This script lives in <root>/scripts, so the repo root is one level up. We do
# not rely on the caller's CWD; every podman build is executed from REPO_ROOT
# so the shared/ package is always part of the build context.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

IMAGE_PREFIX="${IMAGE_PREFIX:-ei-ads}"
PODMAN="${PODMAN:-podman}"
PROBE_TIMEOUT="${PROBE_TIMEOUT:-30}"
PROBE_INTERVAL="${PROBE_INTERVAL:-2}"

# --- SBOM generation config (task 39.1, Req 19.6) ---------------------------
# Syft is a standalone Go binary (not a pip package); the required version is
# pinned under [tool.sbom] in scripts/dependencies/python-versions.toml and
# echoed here as the default. SBOMs are written to ${SBOM_DIR} (repo-root sbom/).
# REQUIRE_SBOM forces a hard failure if syft is missing — it is switched on in
# CI (CI=true) so no image can ship without an SBOM, but left off for local
# sandbox runs where syft may be absent.
SYFT="${SYFT:-syft}"
SYFT_VERSION="${SYFT_VERSION:-1.18.1}"
SBOM_DIR="${SBOM_DIR:-${REPO_ROOT}/sbom}"
REQUIRE_SBOM="${REQUIRE_SBOM:-${CI:-false}}"

# --- Grype vulnerability-scan config (task 39.2, Req 19.6) ------------------
# Grype is a standalone Go binary (not a pip package); the required version is
# pinned under [tool.sbom] in scripts/dependencies/python-versions.toml and
# echoed here as the default. Grype scans the CycloneDX SBOM emitted by 39.1
# against the OSV vulnerability database and FAILS THE BUILD on any HIGH or
# CRITICAL CVE (--fail-on high), so no such image is ever tagged `latest` or
# pushed. REQUIRE_SBOM (shared with 39.1) also gates whether a missing grype is
# fatal (CI) or a soft skip (local sandbox).
GRYPE="${GRYPE:-grype}"
GRYPE_VERSION="${GRYPE_VERSION:-0.87.0}"

# --- SBOM OCI-annotation config (task 39.3, Req 19.6) -----------------------
# skopeo is a standalone binary (not a pip package); the required version is
# pinned under [tool.sbom] in scripts/dependencies/python-versions.toml and
# echoed here as the default. 39.3 re-materialises each freshly built image as
# an OCI layout with three provenance annotations baked into its manifest — the
# SHA-256 digest of the CycloneDX SBOM (org.cyclonedx.sbom.digest), the SBOM
# file name it refers to (org.cyclonedx.sbom.url), and the image ref name
# (org.opencontainers.image.ref.name) — so OpenShift's image admission
# controller can verify SBOM provenance at deploy time. REQUIRE_SBOM (shared
# with 39.1/39.2) gates whether a missing skopeo/podman is fatal (CI) or a soft
# skip (local sandbox); the OCI layouts are written under ${SBOM_DIR}/oci.
SKOPEO="${SKOPEO:-skopeo}"
SKOPEO_VERSION="${SKOPEO_VERSION:-1.16.1}"

# --- Preconditions ----------------------------------------------------------
if ! command -v "${PODMAN}" >/dev/null 2>&1; then
  echo "ERROR: '${PODMAN}' not found on PATH. Install Podman (rootless)." >&2
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  echo "ERROR: git is required to compute the image tag but was not found." >&2
  exit 1
fi

# Immutable, traceable tag: the short Git SHA of the current HEAD.
GIT_SHA="$(git rev-parse --short HEAD)"
if [[ -z "${GIT_SHA}" ]]; then
  echo "ERROR: could not resolve the current Git SHA (git rev-parse HEAD)." >&2
  exit 1
fi

echo "==> EI-ADS image build"
echo "    repo root : ${REPO_ROOT}"
echo "    git sha   : ${GIT_SHA}"
echo "    prefix    : ${IMAGE_PREFIX}"
echo

# --- Determine the HTTP port a service listens on ---------------------------
# 35.1 aligned each Containerfile's EXPOSE directive to the port in the
# service's config.py, so the exposed port is the single source of truth for
# the readiness probe. We parse the last EXPOSE directive from the Containerfile
# rather than hard-coding a port table that could drift from the images.
service_port() {
  local containerfile="$1"
  local port
  port="$(grep -iE '^[[:space:]]*EXPOSE[[:space:]]+[0-9]+' "${containerfile}" \
            | grep -oE '[0-9]+' | tail -n 1)"
  echo "${port}"
}

# Static frontends (claimant-portal) do not expose /healthz — they serve a
# single-page app, so we probe the index route instead. Everything else exposes
# GET /healthz per task 31.2.
health_path_for() {
  local name="$1"
  case "${name}" in
    claimant-portal) echo "/" ;;
    *)               echo "/healthz" ;;
  esac
}

# --- Smoke test: run the container and probe its health endpoint ------------
# NOTE ON THE DESIGN SHORTHAND:
# The design/task text says to verify with
#   podman run --rm ei-ads/<name>:latest /healthz
# Taken literally, "/healthz" would be passed as an argument to the image's
# ENTRYPOINT (uvicorn / serve), which is NOT a real health check and would just
# make the process fail. What that shorthand actually means is "start the
# container and confirm its /healthz endpoint answers". So we implement a real
# readiness probe: start the container detached, poll the HTTP endpoint until it
# returns 200 (or time out), then tear the container down. Only a 200 counts as
# success.
smoke_test() {
  local name="$1" sha_tag="$2" port="$3" health_path="$4"
  local cid=""
  local url="http://127.0.0.1:${port}${health_path}"

  echo "    smoke test: starting container to probe ${url}"

  # Run detached with the container port published to an ephemeral host port.
  # --rm cannot be combined reliably with our explicit stop/rm cleanup, so we
  # remove it explicitly in the trap below.
  cid="$("${PODMAN}" run -d -P --publish "${port}" "${sha_tag}")"

  # Ensure the container is always cleaned up, even on probe failure.
  # shellcheck disable=SC2064
  trap "'${PODMAN}' rm -f '${cid}' >/dev/null 2>&1 || true" RETURN

  # Resolve the host port Podman mapped the container's ${port} to.
  local host_addr host_port
  host_addr="$("${PODMAN}" port "${cid}" "${port}/tcp" 2>/dev/null | head -n 1)"
  if [[ -z "${host_addr}" ]]; then
    echo "    ERROR: could not determine published host port for ${name}." >&2
    return 1
  fi
  # host_addr looks like "0.0.0.0:49153"; take the trailing port number.
  host_port="${host_addr##*:}"

  local probe_url="http://127.0.0.1:${host_port}${health_path}"
  local deadline=$(( SECONDS + PROBE_TIMEOUT ))

  while (( SECONDS < deadline )); do
    if probe_http "${probe_url}"; then
      echo "    smoke test: ${name} answered 200 on ${health_path}"
      return 0
    fi
    sleep "${PROBE_INTERVAL}"
  done

  echo "    ERROR: ${name} did not become healthy within ${PROBE_TIMEOUT}s." >&2
  echo "    ---- container logs (${name}) ----" >&2
  "${PODMAN}" logs "${cid}" 2>&1 | tail -n 40 >&2 || true
  echo "    ----------------------------------" >&2
  return 1
}

# Return 0 only if the URL responds with HTTP 200. Prefers curl, falls back to
# wget so the script works on minimal CI images.
probe_http() {
  local url="$1"
  if command -v curl >/dev/null 2>&1; then
    local code
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "${url}" || echo "000")"
    [[ "${code}" == "200" ]]
  elif command -v wget >/dev/null 2>&1; then
    wget -q -T 3 -O /dev/null "${url}"
  else
    echo "ERROR: neither curl nor wget is available for the readiness probe." >&2
    return 2
  fi
}

# --- SBOM generation (task 39.1, Req 19.6) ----------------------------------
# Generate a Software Bill of Materials for a freshly built image in BOTH the
# CycloneDX and SPDX JSON formats, written to ${SBOM_DIR}. Committing these
# alongside the Git SHA gives every release tag an auditable supply-chain
# snapshot for the SA&A package.
#
# Behaviour when syft is unavailable:
#   - REQUIRE_SBOM=true  (CI): hard-fail so no image ships without an SBOM.
#   - REQUIRE_SBOM=false (sandbox): warn and skip so local builds still work.
#
# This function is intentionally self-contained: it takes the service name and
# the SHA-tagged image reference, and returns 0 on success (or a clean skip).
# Downstream tasks (39.2 Grype scan, 39.3 OCI annotation) consume the CycloneDX
# file it writes at ${SBOM_DIR}/<name>-<sha>.cyclonedx.json.
generate_sbom() {
  local name="$1" sha_tag="$2"
  local cdx_file="${SBOM_DIR}/${name}-${GIT_SHA}.cyclonedx.json"
  local spdx_file="${SBOM_DIR}/${name}-${GIT_SHA}.spdx.json"

  if ! command -v "${SYFT}" >/dev/null 2>&1; then
    if [[ "${REQUIRE_SBOM}" == "true" ]]; then
      echo "ERROR: '${SYFT}' not found on PATH but an SBOM is required (REQUIRE_SBOM=true)." >&2
      echo "       Install Syft ${SYFT_VERSION} (see sbom/README.md)." >&2
      return 1
    fi
    echo "    WARN: '${SYFT}' not found — skipping SBOM generation for ${name}." >&2
    echo "          (set REQUIRE_SBOM=true to make this fatal; it is fatal in CI.)" >&2
    return 0
  fi

  mkdir -p "${SBOM_DIR}"

  echo "    sbom: generating CycloneDX + SPDX for ${sha_tag}"
  "${SYFT}" "${sha_tag}" -o cyclonedx-json > "${cdx_file}"
  "${SYFT}" "${sha_tag}" -o spdx-json      > "${spdx_file}"
  echo "    sbom: wrote ${cdx_file#"${REPO_ROOT}/"} and ${spdx_file#"${REPO_ROOT}/"}"
}

# --- Grype vulnerability scan (task 39.2, Req 19.6) -------------------------
# Scan the CycloneDX SBOM produced by generate_sbom() against the OSV
# vulnerability database and GATE the build on the result. `--fail-on high`
# makes grype exit non-zero on any HIGH or CRITICAL CVE; because this runs
# BEFORE the smoke test tags `latest` / pushes, `set -e` aborts the run and no
# vulnerable image is ever promoted. The full scan report is saved as
# ${SBOM_DIR}/<name>-<sha>-grype-report.json (-o json) so it can be published
# as a CI artifact for audit.
#
# This function is intentionally self-contained: it takes the service name and
# the SHA-tagged image reference (only used for messages) and returns 0 on a
# clean scan (or a clean skip).
#
# Behaviour when grype is unavailable:
#   - REQUIRE_SBOM=true  (CI): hard-fail so no image ships unscanned.
#   - REQUIRE_SBOM=false (sandbox): warn and skip so local builds still work.
#
# It also skips (with a warning) when the CycloneDX SBOM is absent — e.g. when
# syft was missing and generate_sbom() soft-skipped in a local sandbox.
scan_sbom() {
  local name="$1" sha_tag="$2"
  local cdx_file="${SBOM_DIR}/${name}-${GIT_SHA}.cyclonedx.json"
  local report_file="${SBOM_DIR}/${name}-${GIT_SHA}-grype-report.json"

  if ! command -v "${GRYPE}" >/dev/null 2>&1; then
    if [[ "${REQUIRE_SBOM}" == "true" ]]; then
      echo "ERROR: '${GRYPE}' not found on PATH but an SBOM scan is required (REQUIRE_SBOM=true)." >&2
      echo "       Install Grype ${GRYPE_VERSION} (see sbom/README.md)." >&2
      return 1
    fi
    echo "    WARN: '${GRYPE}' not found — skipping vulnerability scan for ${name}." >&2
    echo "          (set REQUIRE_SBOM=true to make this fatal; it is fatal in CI.)" >&2
    return 0
  fi

  if [[ ! -f "${cdx_file}" ]]; then
    if [[ "${REQUIRE_SBOM}" == "true" ]]; then
      echo "ERROR: CycloneDX SBOM ${cdx_file} not found; cannot scan ${name} (REQUIRE_SBOM=true)." >&2
      return 1
    fi
    echo "    WARN: CycloneDX SBOM ${cdx_file#"${REPO_ROOT}/"} missing — skipping scan for ${name}." >&2
    return 0
  fi

  echo "    scan: grype ${cdx_file#"${REPO_ROOT}/"} (--fail-on high)"

  # Persist the machine-readable report first (this invocation does not gate the
  # build), then run the gating scan. Writing the report separately guarantees
  # the audit artifact exists even when the gating scan aborts the build on a
  # HIGH/CRITICAL finding.
  "${GRYPE}" "sbom:${cdx_file}" -o json > "${report_file}"
  echo "    scan: wrote ${report_file#"${REPO_ROOT}/"}"

  # Gating scan: a non-zero exit here (HIGH/CRITICAL CVE) propagates via `set -e`
  # and aborts the run before the image can be tagged `latest` or pushed.
  "${GRYPE}" "sbom:${cdx_file}" --fail-on high
  echo "    scan: ${name} passed — no HIGH or CRITICAL CVEs"
}

# --- Embed SBOM digest as an OCI annotation (task 39.3, Req 19.6) -----------
# Bake the CycloneDX SBOM's provenance into the image manifest so OpenShift's
# image admission controller can verify, at deploy time, that the image is
# accompanied by the exact SBOM it was scanned against. We compute the SHA-256
# digest of the CycloneDX file 39.1 produced and re-materialise the image as an
# OCI layout carrying three annotations:
#
#   org.opencontainers.image.ref.name  the SHA-tagged image reference
#   org.cyclonedx.sbom.digest          sha256:<digest of the CycloneDX SBOM>
#   org.cyclonedx.sbom.url             the CycloneDX SBOM file name (repo-relative)
#
# Mechanics (as prescribed by the task):
#   1. `podman image tag`     — ensure a stable local ref for the built image.
#   2. `podman image inspect` — confirm the image exists locally before copying.
#   3. `skopeo copy --dest-oci-accept-uncompressed-layers` from the local podman
#      store into an OCI layout, passing each annotation via `--dest-annotation`.
# The resulting OCI layout is written under ${SBOM_DIR}/oci/<name>-<sha>/ and is
# the artifact a downstream push/admission step consumes.
#
# This function is intentionally self-contained: it takes the service name and
# the SHA-tagged image reference.
#
# Behaviour when skopeo/podman is unavailable:
#   - REQUIRE_SBOM=true  (CI): hard-fail so no image ships without provenance.
#   - REQUIRE_SBOM=false (sandbox): warn and skip so local builds still work.
#
# It also skips (with a warning) when the CycloneDX SBOM is absent — e.g. when
# syft was missing and generate_sbom() soft-skipped in a local sandbox.
annotate_sbom_digest() {
  local name="$1" sha_tag="$2"
  local cdx_file="${SBOM_DIR}/${name}-${GIT_SHA}.cyclonedx.json"
  local oci_dir="${SBOM_DIR}/oci/${name}-${GIT_SHA}"

  if ! command -v "${SKOPEO}" >/dev/null 2>&1; then
    if [[ "${REQUIRE_SBOM}" == "true" ]]; then
      echo "ERROR: '${SKOPEO}' not found on PATH but SBOM annotation is required (REQUIRE_SBOM=true)." >&2
      echo "       Install skopeo ${SKOPEO_VERSION} (see sbom/README.md)." >&2
      return 1
    fi
    echo "    WARN: '${SKOPEO}' not found — skipping SBOM OCI annotation for ${name}." >&2
    echo "          (set REQUIRE_SBOM=true to make this fatal; it is fatal in CI.)" >&2
    return 0
  fi

  if [[ ! -f "${cdx_file}" ]]; then
    if [[ "${REQUIRE_SBOM}" == "true" ]]; then
      echo "ERROR: CycloneDX SBOM ${cdx_file} not found; cannot annotate ${name} (REQUIRE_SBOM=true)." >&2
      return 1
    fi
    echo "    WARN: CycloneDX SBOM ${cdx_file#"${REPO_ROOT}/"} missing — skipping annotation for ${name}." >&2
    return 0
  fi

  # 1. SHA-256 digest of the CycloneDX SBOM. sha256sum prints "<hex>  <file>";
  #    take the first field. Fall back to `shasum -a 256` on hosts (e.g. macOS)
  #    that ship shasum instead of the coreutils sha256sum.
  local sbom_hash
  if command -v sha256sum >/dev/null 2>&1; then
    sbom_hash="$(sha256sum "${cdx_file}" | awk '{print $1}')"
  elif command -v shasum >/dev/null 2>&1; then
    sbom_hash="$(shasum -a 256 "${cdx_file}" | awk '{print $1}')"
  else
    if [[ "${REQUIRE_SBOM}" == "true" ]]; then
      echo "ERROR: neither sha256sum nor shasum available to digest the SBOM (REQUIRE_SBOM=true)." >&2
      return 1
    fi
    echo "    WARN: no sha256sum/shasum available — skipping annotation for ${name}." >&2
    return 0
  fi
  local sbom_digest="sha256:${sbom_hash}"
  local sbom_url="sbom/${name}-${GIT_SHA}.cyclonedx.json"

  # 2. Ensure a stable local ref and confirm the image is present in the podman
  #    store before we hand it to skopeo.
  "${PODMAN}" image tag "${sha_tag}" "${sha_tag}"
  if ! "${PODMAN}" image inspect "${sha_tag}" >/dev/null 2>&1; then
    echo "ERROR: image ${sha_tag} not found in the local podman store; cannot annotate ${name}." >&2
    return 1
  fi

  mkdir -p "${oci_dir}"

  echo "    annotate: ${name} sbom digest ${sbom_digest}"

  # 3. Copy from the local podman store into an OCI layout, embedding the three
  #    provenance annotations. --dest-oci-accept-uncompressed-layers keeps the
  #    copy working when the source layers are uncompressed.
  "${SKOPEO}" copy --dest-oci-accept-uncompressed-layers \
    --dest-annotation "org.opencontainers.image.ref.name=${sha_tag}" \
    --dest-annotation "org.cyclonedx.sbom.digest=${sbom_digest}" \
    --dest-annotation "org.cyclonedx.sbom.url=${sbom_url}" \
    "containers-storage:${sha_tag}" \
    "oci:${oci_dir}:${GIT_SHA}"

  echo "    annotate: wrote OCI layout ${oci_dir#"${REPO_ROOT}/"} with SBOM provenance"
}

# --- Aggregate release SBOM (task 39.5, Req 19.6, 19.8) ---------------------
# Merge every per-service CycloneDX SBOM produced by generate_sbom() during the
# build loop into ONE release-wide SBOM, then scan the merged document with
# Grype. The aggregate is the authoritative supply-chain artifact submitted with
# the Government of Canada Security Assessment and Authorisation (SA&A) package,
# so a clean copy is also placed under docs/security/sbom/ where the Privacy
# Impact Assessment (PIA, Req 19.8) references it.
#
#   1. syft --merge sbom/*.cyclonedx.json -o cyclonedx-json
#        > sbom/ei-ads-<sha>-aggregate.cyclonedx.json
#   2. grype sbom:<aggregate> --fail-on high   (gating: any HIGH/CRITICAL aborts)
#   3. copy the aggregate into docs/security/sbom/ (authoritative SA&A artifact)
#
# This step runs AFTER the per-service build loop completes (all SBOMs must
# exist before they can be merged) and is intentionally self-contained.
#
# Behaviour when syft/grype is unavailable, or when no per-service CycloneDX
# SBOMs exist (e.g. syft soft-skipped in a local sandbox):
#   - REQUIRE_SBOM=true  (CI): hard-fail so no release ships without an
#                              aggregate SBOM.
#   - REQUIRE_SBOM=false (sandbox): warn and skip so local builds still work.
aggregate_sbom() {
  local aggregate_file="${SBOM_DIR}/ei-ads-${GIT_SHA}-aggregate.cyclonedx.json"
  local docs_sbom_dir="${REPO_ROOT}/docs/security/sbom"

  if ! command -v "${SYFT}" >/dev/null 2>&1; then
    if [[ "${REQUIRE_SBOM}" == "true" ]]; then
      echo "ERROR: '${SYFT}' not found on PATH but an aggregate SBOM is required (REQUIRE_SBOM=true)." >&2
      echo "       Install Syft ${SYFT_VERSION} (see sbom/README.md)." >&2
      return 1
    fi
    echo "    WARN: '${SYFT}' not found — skipping aggregate SBOM merge." >&2
    echo "          (set REQUIRE_SBOM=true to make this fatal; it is fatal in CI.)" >&2
    return 0
  fi

  # Gather the per-service CycloneDX SBOMs, EXCLUDING any prior aggregate so a
  # re-run does not fold a previous aggregate back into the new one. A portable
  # glob loop (no `shopt -s nullglob`) keeps this working on bash 3.2 (macOS).
  local cdx_files=()
  local f
  for f in "${SBOM_DIR}"/*.cyclonedx.json; do
    [[ -e "${f}" ]] || continue
    case "${f}" in
      *-aggregate.cyclonedx.json) continue ;;
    esac
    cdx_files+=("${f}")
  done

  if (( ${#cdx_files[@]} == 0 )); then
    if [[ "${REQUIRE_SBOM}" == "true" ]]; then
      echo "ERROR: no per-service CycloneDX SBOMs under ${SBOM_DIR#"${REPO_ROOT}/"}; cannot build aggregate (REQUIRE_SBOM=true)." >&2
      return 1
    fi
    echo "    WARN: no per-service CycloneDX SBOMs found — skipping aggregate SBOM merge." >&2
    return 0
  fi

  echo "==> Aggregate SBOM: merging ${#cdx_files[@]} per-service CycloneDX SBOM(s)"

  # 1. Merge all per-service CycloneDX SBOMs into the release-wide aggregate.
  #    The task prescribes the glob form; we pass the pre-filtered file list so
  #    a stale aggregate is never re-merged.
  "${SYFT}" --merge "${cdx_files[@]}" -o cyclonedx-json > "${aggregate_file}"
  echo "    aggregate: wrote ${aggregate_file#"${REPO_ROOT}/"}"

  # 2. Gate the aggregate on HIGH/CRITICAL CVEs. A non-zero exit propagates via
  #    `set -e` and aborts the run so no release ships with a vulnerable
  #    aggregate SBOM.
  if command -v "${GRYPE}" >/dev/null 2>&1; then
    echo "    aggregate: grype ${aggregate_file#"${REPO_ROOT}/"} (--fail-on high)"
    "${GRYPE}" "sbom:${aggregate_file}" --fail-on high
    echo "    aggregate: passed — no HIGH or CRITICAL CVEs in the release SBOM"
  else
    if [[ "${REQUIRE_SBOM}" == "true" ]]; then
      echo "ERROR: '${GRYPE}' not found on PATH but the aggregate scan is required (REQUIRE_SBOM=true)." >&2
      echo "       Install Grype ${GRYPE_VERSION} (see sbom/README.md)." >&2
      return 1
    fi
    echo "    WARN: '${GRYPE}' not found — skipping aggregate vulnerability scan." >&2
    echo "          (set REQUIRE_SBOM=true to make this fatal; it is fatal in CI.)" >&2
  fi

  # 3. Publish the authoritative copy for the SA&A package / PIA reference.
  mkdir -p "${docs_sbom_dir}"
  cp "${aggregate_file}" "${docs_sbom_dir}/"
  echo "    aggregate: copied authoritative SBOM to ${docs_sbom_dir#"${REPO_ROOT}/"}/$(basename "${aggregate_file}")"
}

# --- Discover services and build --------------------------------------------
# A service is any directory under services/ that ships a Containerfile.
# Read into an array with a portable `while read` loop (avoids `mapfile`, which
# is unavailable on bash 3.2 shipped by macOS).
CONTAINERFILES=()
while IFS= read -r cf; do
  [[ -n "${cf}" ]] && CONTAINERFILES+=("${cf}")
done < <(find services -mindepth 2 -maxdepth 2 -name Containerfile | sort)

if (( ${#CONTAINERFILES[@]} == 0 )); then
  echo "ERROR: no services/<name>/Containerfile found under ${REPO_ROOT}." >&2
  exit 1
fi

# --- Optional service filter (used by the CI matrix, task 35.4) -------------
# When SERVICES is set (space- or comma-separated), restrict the build to just
# those services. This lets each parallel CI matrix job build a single image
# without changing the default "build everything" behaviour of a plain run.
if [[ -n "${SERVICES:-}" ]]; then
  # Normalise commas to spaces, then split into a set for membership checks.
  requested="${SERVICES//,/ }"
  declare -a wanted=()
  for s in ${requested}; do
    [[ -n "${s}" ]] && wanted+=("${s}")
  done

  filtered=()
  for want in "${wanted[@]}"; do
    match=""
    for cf in "${CONTAINERFILES[@]}"; do
      if [[ "$(basename "$(dirname "${cf}")")" == "${want}" ]]; then
        match="${cf}"
        break
      fi
    done
    if [[ -z "${match}" ]]; then
      echo "ERROR: SERVICES requested '${want}' but no services/${want}/Containerfile exists." >&2
      exit 1
    fi
    filtered+=("${match}")
  done
  CONTAINERFILES=("${filtered[@]}")
  echo "==> SERVICES filter active — building only: ${wanted[*]}"
fi

echo "==> Found ${#CONTAINERFILES[@]} service(s) with a Containerfile"
echo

built_images=()

for containerfile in "${CONTAINERFILES[@]}"; do
  # services/<name>/Containerfile -> <name>
  service_dir="$(dirname "${containerfile}")"
  name="$(basename "${service_dir}")"

  port="$(service_port "${containerfile}")"
  if [[ -z "${port}" ]]; then
    echo "ERROR: no EXPOSE port found in ${containerfile}; cannot probe ${name}." >&2
    exit 1
  fi
  health_path="$(health_path_for "${name}")"

  sha_tag="${IMAGE_PREFIX}/${name}:${GIT_SHA}"
  latest_tag="${IMAGE_PREFIX}/${name}:latest"

  echo "==> [${name}] building ${sha_tag} (port ${port}, probe ${health_path})"

  # Build from the repo root so shared/ is in the build context. --no-cache
  # guarantees a clean, reproducible image for release tagging. A failure here
  # aborts the whole script via `set -e`.
  "${PODMAN}" build --no-cache \
    -t "${sha_tag}" \
    -f "${containerfile}" \
    "${REPO_ROOT}"

  # --- 39.1 SBOM generation START ---------------------------------------
  # Build succeeded: generate the per-image SBOM (CycloneDX + SPDX) before we
  # smoke-test and tag `latest`, so a supply-chain snapshot exists for this SHA.
  # A failure here aborts the run via `set -e` when SBOMs are required (CI).
  # NOTE (coordination): tasks 39.2 (Grype scan of the CycloneDX SBOM) and 39.3
  # (embed SBOM digest as an OCI annotation) insert their steps immediately
  # AFTER this block, in that order.
  generate_sbom "${name}" "${sha_tag}"
  # --- 39.1 SBOM generation END -----------------------------------------

  # --- 39.2 Grype scan START --------------------------------------------
  # Scan the CycloneDX SBOM against the OSV vulnerability database and GATE the
  # build: any HIGH/CRITICAL CVE makes grype exit non-zero, which aborts the run
  # via `set -e` BEFORE the smoke test tags `latest` or the image is pushed. The
  # JSON report is saved under ${SBOM_DIR} for CI to publish as an audit artifact.
  # NOTE (coordination): task 39.3 (embed SBOM digest as an OCI annotation)
  # inserts its step immediately AFTER this block.
  scan_sbom "${name}" "${sha_tag}"
  # --- 39.2 Grype scan END ----------------------------------------------

  # --- 39.3 OCI annotation START ----------------------------------------
  # Embed the CycloneDX SBOM's SHA-256 digest (plus the image ref name and the
  # SBOM file URL) into an OCI layout of the image so OpenShift's image
  # admission controller can verify SBOM provenance at deploy time. Runs after
  # the scan so only a scanned image is ever annotated; failure aborts the run
  # via `set -e` when provenance is required (CI).
  annotate_sbom_digest "${name}" "${sha_tag}"
  # --- 39.3 OCI annotation END ------------------------------------------

  # Only tag `latest` once the running container passes its readiness probe.
  if smoke_test "${name}" "${sha_tag}" "${port}" "${health_path}"; then
    "${PODMAN}" tag "${sha_tag}" "${latest_tag}"
    echo "==> [${name}] OK — tagged ${latest_tag}"
    built_images+=("${sha_tag}")
  else
    echo "ERROR: [${name}] smoke test failed; NOT tagging latest. Aborting." >&2
    exit 1
  fi
  echo
done

echo "==> All ${#built_images[@]} image(s) built, verified, and tagged:"
for img in "${built_images[@]}"; do
  echo "    - ${img} (+ :latest)"
done
echo

# --- 39.5 aggregate SBOM START ----------------------------------------------
# All per-service SBOMs now exist (generate_sbom ran inside the loop above), so
# merge them into ONE release-wide CycloneDX SBOM, scan the merged document with
# Grype (--fail-on high), and publish the authoritative copy under
# docs/security/sbom/ for the SA&A package / PIA reference. Runs once, after the
# whole build loop, and soft-skips in a local sandbox where syft/grype are
# absent (fatal in CI via REQUIRE_SBOM=true).
aggregate_sbom
# --- 39.5 aggregate SBOM END ------------------------------------------------
