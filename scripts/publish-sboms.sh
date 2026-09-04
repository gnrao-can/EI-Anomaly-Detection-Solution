#!/usr/bin/env bash
#
# EI-ADS SBOM publish script (OCI artifact + artifact repository).
#
# Publishes the per-service CycloneDX SBOMs produced by scripts/build-images.sh
# (each written to sbom/<name>-<sha>.cyclonedx.json) to two destinations so the
# supply-chain snapshot for a release SHA is retrievable both from the registry
# that holds the images and from the project's long-term artifact store:
#
#   1. OCI ARTIFACT PUSH (oras) — for every service SBOM, push the CycloneDX
#      JSON as its own OCI artifact next to the image it describes:
#        oras push ${OCP_REGISTRY}/ei-ads/<name>-sbom:<sha> \
#          sbom/<name>-<sha>.cyclonedx.json:application/vnd.cyclonedx+json
#      This keeps the SBOM addressable by the same registry/tag scheme as the
#      image, so a deploy-time check can resolve <name>-sbom:<sha> from
#      <name>:<sha>.
#
#   2. ARTIFACT REPOSITORY UPLOAD (Nexus / Artifactory, optional) — upload each
#      SBOM under sbom/ in the project's artifact repo via curl, plus a
#      sbom/manifest.json index listing every SBOM by service name, Git SHA,
#      image digest (podman image inspect --format '{{.Digest}}'), and the
#      generation timestamp. This step is GUARDED: it runs only when
#      ARTIFACT_REPO_URL is set, so a plain local run (or a registry-only CI
#      job) still succeeds without an artifact-repo configured.
#
# Service discovery, the image prefix, and the SHA tag are computed exactly the
# same way as scripts/build-images.sh and scripts/push-images.sh so all three
# scripts agree on which SBOMs exist and how they are named. This script is
# called by scripts/push-images.sh after the image push loop, but is also safe
# to run standalone.
#
# The script fails fast: `set -euo pipefail`. Behaviour when oras is missing is
# gated by REQUIRE_SBOM (shared with build-images.sh): fatal in CI, a soft skip
# in a local sandbox where oras is not installed.
#
# Make it executable once with:   chmod +x scripts/publish-sboms.sh
# Then run from anywhere:         scripts/publish-sboms.sh
#
# Required environment variables (for the OCI artifact push):
#   OCP_REGISTRY      Registry host (and optional namespace path), e.g.
#                     default-route-openshift-image-registry.apps.example.com
#
# Optional environment variables:
#   IMAGE_PREFIX      Image namespace/prefix          (default: ei-ads)
#   PODMAN            podman binary / override         (default: podman)
#   ORAS              oras binary / override           (default: oras)
#   ORAS_VERSION      required pinned oras version      (default: 1.2.0; pinned
#                     under [tool.sbom] in
#                     scripts/dependencies/python-versions.toml)
#   SBOM_DIR          Directory holding the SBOMs      (default: <repo>/sbom)
#   REQUIRE_SBOM      "true" => missing oras is fatal   (default: $CI or false)
#
#   ARTIFACT_REPO_URL     Base URL of the Nexus/Artifactory sbom/ folder, e.g.
#                         https://nexus.example.com/repository/ei-ads-sbom
#                         When UNSET the artifact-repo upload is skipped.
#   ARTIFACT_REPO_USER    Artifact-repo username (optional; enables basic auth)
#   ARTIFACT_REPO_TOKEN   Artifact-repo password / token (kept secret; optional)
#   CURL                  curl binary / override        (default: curl)
#
# Requirements: 19.6
#
set -euo pipefail

# --- Locate the monorepo root -----------------------------------------------
# This script lives in <root>/scripts, so the repo root is one level up. We do
# not rely on the caller's CWD; service discovery and `git rev-parse` are run
# from REPO_ROOT so the results match build-images.sh / push-images.sh exactly.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

IMAGE_PREFIX="${IMAGE_PREFIX:-ei-ads}"
PODMAN="${PODMAN:-podman}"
ORAS="${ORAS:-oras}"
ORAS_VERSION="${ORAS_VERSION:-1.2.0}"
SBOM_DIR="${SBOM_DIR:-${REPO_ROOT}/sbom}"
REQUIRE_SBOM="${REQUIRE_SBOM:-${CI:-false}}"
CURL="${CURL:-curl}"

# CycloneDX OCI artifact media type — the SBOM push tags each layer with this so
# a consumer can tell an SBOM artifact from an image manifest.
CYCLONEDX_MEDIA_TYPE="application/vnd.cyclonedx+json"

# --- Preconditions ----------------------------------------------------------
if ! command -v git >/dev/null 2>&1; then
  echo "ERROR: git is required to compute the SBOM tag but was not found." >&2
  exit 1
fi

# Immutable, traceable tag: the short Git SHA of the current HEAD. Must match
# the tag produced by build-images.sh / push-images.sh.
GIT_SHA="$(git rev-parse --short HEAD)"
if [[ -z "${GIT_SHA}" ]]; then
  echo "ERROR: could not resolve the current Git SHA (git rev-parse HEAD)." >&2
  exit 1
fi

# The OCI artifact push needs a registry host. When it is missing we treat this
# like the other soft-skips: fatal in CI (REQUIRE_SBOM=true), a warning locally.
if [[ -z "${OCP_REGISTRY:-}" ]]; then
  if [[ "${REQUIRE_SBOM}" == "true" ]]; then
    echo "ERROR: OCP_REGISTRY is not set but SBOM publish is required (REQUIRE_SBOM=true)." >&2
    exit 1
  fi
  echo "    WARN: OCP_REGISTRY not set — skipping SBOM publish." >&2
  echo "          (set REQUIRE_SBOM=true to make this fatal; it is fatal in CI.)" >&2
  exit 0
fi

echo "==> EI-ADS SBOM publish"
echo "    repo root : ${REPO_ROOT}"
echo "    git sha   : ${GIT_SHA}"
echo "    registry  : ${OCP_REGISTRY}"
echo "    prefix    : ${IMAGE_PREFIX}"
echo "    sbom dir  : ${SBOM_DIR#"${REPO_ROOT}/"}"
echo "    artifact repo: $([[ -n "${ARTIFACT_REPO_URL:-}" ]] && echo "${ARTIFACT_REPO_URL}" || echo "(unset — upload skipped)")"
echo

# --- Discover services ------------------------------------------------------
# A service is any directory under services/ that ships a Containerfile. This is
# the same discovery loop used by build-images.sh / push-images.sh. We use a
# portable `while read` loop (avoids `mapfile`, unavailable on bash 3.2 / macOS).
CONTAINERFILES=()
while IFS= read -r cf; do
  [[ -n "${cf}" ]] && CONTAINERFILES+=("${cf}")
done < <(find services -mindepth 2 -maxdepth 2 -name Containerfile | sort)

if (( ${#CONTAINERFILES[@]} == 0 )); then
  echo "ERROR: no services/<name>/Containerfile found under ${REPO_ROOT}." >&2
  exit 1
fi

echo "==> Found ${#CONTAINERFILES[@]} service(s) with a Containerfile"
echo

# --- oras availability guard ------------------------------------------------
# oras is a standalone binary (pinned as ORAS_VERSION under [tool.sbom] in
# scripts/dependencies/python-versions.toml). When it is absent:
#   - REQUIRE_SBOM=true  (CI): hard-fail so no release ships without published
#                              SBOM artifacts.
#   - REQUIRE_SBOM=false (sandbox): warn and skip the OCI push so local runs
#                              still work; the artifact-repo upload below is
#                              independently guarded on ARTIFACT_REPO_URL.
ORAS_AVAILABLE=1
if ! command -v "${ORAS}" >/dev/null 2>&1; then
  if [[ "${REQUIRE_SBOM}" == "true" ]]; then
    echo "ERROR: '${ORAS}' not found on PATH but SBOM publish is required (REQUIRE_SBOM=true)." >&2
    echo "       Install oras ${ORAS_VERSION} (see sbom/README.md)." >&2
    exit 1
  fi
  echo "    WARN: '${ORAS}' not found — skipping OCI artifact push for all SBOMs." >&2
  echo "          (set REQUIRE_SBOM=true to make this fatal; it is fatal in CI.)" >&2
  ORAS_AVAILABLE=0
fi

# --- Resolve an image digest for a service ----------------------------------
# The manifest index records the digest of the image each SBOM describes so an
# auditor can tie the SBOM to an exact image. We read it from the local podman
# store (podman image inspect --format '{{.Digest}}'); if podman or the image is
# unavailable (e.g. a standalone sandbox run) we record an empty string rather
# than aborting — the SBOM push does not depend on it.
image_digest_for() {
  local name="$1"
  local sha_tag="${IMAGE_PREFIX}/${name}:${GIT_SHA}"
  if command -v "${PODMAN}" >/dev/null 2>&1 \
       && "${PODMAN}" image inspect "${sha_tag}" >/dev/null 2>&1; then
    "${PODMAN}" image inspect --format '{{.Digest}}' "${sha_tag}" 2>/dev/null || echo ""
  else
    echo ""
  fi
}

# --- Build the manifest index + push each SBOM ------------------------------
# We assemble sbom/manifest.json incrementally as a JSON array of entries, one
# per service SBOM we find. Rather than depend on jq (not guaranteed present),
# we emit well-formed JSON by hand with careful comma handling. String values
# here (service name, sha, digest, file name) come from our own controlled
# naming scheme, not arbitrary input.
GENERATED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
MANIFEST_FILE="${SBOM_DIR}/manifest.json"

published_artifacts=()
manifest_entries=()

for containerfile in "${CONTAINERFILES[@]}"; do
  # services/<name>/Containerfile -> <name>
  service_dir="$(dirname "${containerfile}")"
  name="$(basename "${service_dir}")"

  sbom_file="${SBOM_DIR}/${name}-${GIT_SHA}.cyclonedx.json"
  sbom_rel="sbom/${name}-${GIT_SHA}.cyclonedx.json"

  # A missing SBOM means build-images.sh soft-skipped syft in a sandbox. Honour
  # REQUIRE_SBOM: fatal in CI, a per-service skip locally.
  if [[ ! -f "${sbom_file}" ]]; then
    if [[ "${REQUIRE_SBOM}" == "true" ]]; then
      echo "ERROR: CycloneDX SBOM ${sbom_rel} not found; cannot publish ${name} (REQUIRE_SBOM=true)." >&2
      exit 1
    fi
    echo "    WARN: SBOM ${sbom_rel} missing — skipping ${name}." >&2
    continue
  fi

  sbom_ref="${OCP_REGISTRY}/${IMAGE_PREFIX}/${name}-sbom:${GIT_SHA}"
  digest="$(image_digest_for "${name}")"

  # 1. OCI artifact push (guarded on oras availability). oras is run from
  #    ${SBOM_DIR} so the file reference in the push argument is the bare file
  #    name (oras records the path exactly as given).
  if (( ORAS_AVAILABLE == 1 )); then
    echo "==> [${name}] oras push ${sbom_ref}"
    if ! ( cd "${SBOM_DIR}" \
             && "${ORAS}" push "${sbom_ref}" \
                  "${name}-${GIT_SHA}.cyclonedx.json:${CYCLONEDX_MEDIA_TYPE}" ); then
      echo "ERROR: [${name}] failed to push SBOM artifact ${sbom_ref}. Aborting." >&2
      exit 1
    fi
    published_artifacts+=("${sbom_ref}")
  fi

  # 2. Record a manifest entry for this SBOM (always, so the index reflects
  #    every SBOM we found even when oras soft-skipped).
  manifest_entries+=("$(printf '    {\n      "service": "%s",\n      "gitSha": "%s",\n      "imageDigest": "%s",\n      "sbomFile": "%s",\n      "ociArtifact": "%s",\n      "mediaType": "%s",\n      "generatedAt": "%s"\n    }' \
    "${name}" "${GIT_SHA}" "${digest}" "${sbom_rel}" "${sbom_ref}" "${CYCLONEDX_MEDIA_TYPE}" "${GENERATED_AT}")")
done

if (( ${#manifest_entries[@]} == 0 )); then
  echo "    WARN: no per-service CycloneDX SBOMs found — nothing to publish; no manifest written." >&2
  exit 0
fi

# --- Write sbom/manifest.json ------------------------------------------------
# Join the entries with commas into a single JSON document. We write to a temp
# file and move it into place so a partially written manifest is never left
# behind on failure.
mkdir -p "${SBOM_DIR}"
tmp_manifest="${MANIFEST_FILE}.tmp.$$"
{
  printf '{\n'
  printf '  "release": "%s",\n' "${IMAGE_PREFIX}"
  printf '  "gitSha": "%s",\n' "${GIT_SHA}"
  printf '  "generatedAt": "%s",\n' "${GENERATED_AT}"
  printf '  "sboms": [\n'
  # Join array entries with ",\n".
  for i in "${!manifest_entries[@]}"; do
    printf '%s' "${manifest_entries[$i]}"
    if (( i < ${#manifest_entries[@]} - 1 )); then
      printf ',\n'
    else
      printf '\n'
    fi
  done
  printf '  ]\n'
  printf '}\n'
} > "${tmp_manifest}"
mv "${tmp_manifest}" "${MANIFEST_FILE}"
echo
echo "==> Wrote SBOM index ${MANIFEST_FILE#"${REPO_ROOT}/"} (${#manifest_entries[@]} entr$([[ ${#manifest_entries[@]} -eq 1 ]] && echo "y" || echo "ies"))"

# --- Artifact repository upload (Nexus / Artifactory) — OPTIONAL ------------
# Guarded on ARTIFACT_REPO_URL: when it is unset we skip the upload entirely so
# a registry-only run still succeeds. When set, we PUT each SBOM plus the
# manifest under sbom/ in the repo via curl. Credentials are optional; when
# both ARTIFACT_REPO_USER and ARTIFACT_REPO_TOKEN are present we pass them as
# HTTP basic auth (on stdin via a config file, so the token never appears in the
# process argument list or `ps` output).
if [[ -n "${ARTIFACT_REPO_URL:-}" ]]; then
  if ! command -v "${CURL}" >/dev/null 2>&1; then
    echo "ERROR: ARTIFACT_REPO_URL is set but '${CURL}' was not found on PATH." >&2
    exit 1
  fi

  # Strip a single trailing slash so we can join with "/sbom/<file>" cleanly.
  repo_base="${ARTIFACT_REPO_URL%/}"

  # Assemble curl auth options. When credentials are provided we feed them via
  # `--config -` (stdin) so the secret is not exposed on the command line.
  upload_one() {
    local local_path="$1" remote_name="$2"
    local target="${repo_base}/sbom/${remote_name}"
    echo "    upload: ${remote_name} -> ${target}"
    if [[ -n "${ARTIFACT_REPO_USER:-}" && -n "${ARTIFACT_REPO_TOKEN:-}" ]]; then
      printf 'user = "%s:%s"\n' "${ARTIFACT_REPO_USER}" "${ARTIFACT_REPO_TOKEN}" \
        | "${CURL}" --fail --silent --show-error --config - \
            --upload-file "${local_path}" "${target}"
    else
      "${CURL}" --fail --silent --show-error \
        --upload-file "${local_path}" "${target}"
    fi
  }

  echo
  echo "==> Uploading SBOMs to artifact repository ${repo_base}/sbom/"
  for containerfile in "${CONTAINERFILES[@]}"; do
    name="$(basename "$(dirname "${containerfile}")")"
    sbom_file="${SBOM_DIR}/${name}-${GIT_SHA}.cyclonedx.json"
    [[ -f "${sbom_file}" ]] || continue
    if ! upload_one "${sbom_file}" "${name}-${GIT_SHA}.cyclonedx.json"; then
      echo "ERROR: failed to upload SBOM for ${name} to the artifact repository. Aborting." >&2
      exit 1
    fi
  done

  # Upload the manifest index last so it only appears once every SBOM is stored.
  if ! upload_one "${MANIFEST_FILE}" "manifest.json"; then
    echo "ERROR: failed to upload manifest.json to the artifact repository. Aborting." >&2
    exit 1
  fi
  echo "==> Artifact repository upload complete."
else
  echo "==> ARTIFACT_REPO_URL unset — skipping artifact repository upload."
fi

# --- Summary ----------------------------------------------------------------
echo
if (( ${#published_artifacts[@]} > 0 )); then
  echo "==> Pushed ${#published_artifacts[@]} SBOM OCI artifact(s) to ${OCP_REGISTRY}:"
  for art in "${published_artifacts[@]}"; do
    echo "    - ${art}"
  done
else
  echo "==> No SBOM OCI artifacts pushed (oras unavailable — soft skip)."
fi
