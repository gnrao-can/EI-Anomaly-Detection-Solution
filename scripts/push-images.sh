#!/usr/bin/env bash
#
# EI-ADS container image push script (Podman, rootless).
#
# Publishes the images produced by scripts/build-images.sh to the internal
# OpenShift image registry. For every service that ships a Containerfile under
# services/<name>/Containerfile this script:
#
#   1. Logs in to the OpenShift registry once, using credentials supplied via
#      environment variables (OCP_REGISTRY, OCP_USER, OCP_TOKEN). The token is
#      fed to `podman login --password-stdin` so it never appears in the
#      process argument list, in `ps` output, or in the shell history.
#   2. Re-tags the locally built SHA image for the remote registry and pushes
#      it:  podman push ei-ads/<name>:<sha> ${OCP_REGISTRY}/ei-ads/<name>:<sha>
#   3. Pushes the `latest` tag ONLY when the pipeline is running on the main
#      branch (detected from $GITHUB_REF or $CI_COMMIT_BRANCH). On feature
#      branches only the immutable SHA tag is published, so `latest` in the
#      registry always points at a commit that landed on main.
#
# Service discovery, the image prefix, and the SHA tag are computed exactly the
# same way as scripts/build-images.sh so the two scripts always agree on which
# images exist and how they are named.
#
# The script fails fast: `set -euo pipefail` plus explicit checks mean a missing
# credential, a failed login, or a single failed push aborts the whole run with
# a non-zero exit code.
#
# Make it executable once with:   chmod +x scripts/push-images.sh
# Then run from anywhere:         scripts/push-images.sh
#
# Required environment variables:
#   OCP_REGISTRY      Registry host (and optional namespace path), e.g.
#                     default-route-openshift-image-registry.apps.example.com
#   OCP_USER          Registry username / service-account name
#   OCP_TOKEN         Registry password / OpenShift token (kept secret)
#
# Optional environment variables:
#   IMAGE_PREFIX      Local image namespace/prefix   (default: ei-ads)
#   PODMAN            podman binary / override        (default: podman)
#   GITHUB_REF        Set by GitHub Actions; used for main-branch detection
#   CI_COMMIT_BRANCH  Set by GitLab CI; used for main-branch detection
#
# Requirements: 17.5
#
set -euo pipefail

# --- Locate the monorepo root -----------------------------------------------
# This script lives in <root>/scripts, so the repo root is one level up. We do
# not rely on the caller's CWD; service discovery and `git rev-parse` are run
# from REPO_ROOT so the results match scripts/build-images.sh exactly.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

IMAGE_PREFIX="${IMAGE_PREFIX:-ei-ads}"
PODMAN="${PODMAN:-podman}"

# --- Preconditions ----------------------------------------------------------
if ! command -v "${PODMAN}" >/dev/null 2>&1; then
  echo "ERROR: '${PODMAN}' not found on PATH. Install Podman (rootless)." >&2
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  echo "ERROR: git is required to compute the image tag but was not found." >&2
  exit 1
fi

# Validate required credentials up front and fail fast if any are missing. We
# intentionally never print the value of OCP_TOKEN — only whether it is set.
missing=()
[[ -z "${OCP_REGISTRY:-}" ]] && missing+=("OCP_REGISTRY")
[[ -z "${OCP_USER:-}" ]]     && missing+=("OCP_USER")
[[ -z "${OCP_TOKEN:-}" ]]    && missing+=("OCP_TOKEN")
if (( ${#missing[@]} > 0 )); then
  echo "ERROR: missing required environment variable(s): ${missing[*]}" >&2
  echo "       Set OCP_REGISTRY, OCP_USER, and OCP_TOKEN before pushing." >&2
  exit 1
fi

# Immutable, traceable tag: the short Git SHA of the current HEAD. Must match
# the tag produced by scripts/build-images.sh.
GIT_SHA="$(git rev-parse --short HEAD)"
if [[ -z "${GIT_SHA}" ]]; then
  echo "ERROR: could not resolve the current Git SHA (git rev-parse HEAD)." >&2
  exit 1
fi

# --- Determine whether we are on the main branch ----------------------------
# CI systems expose the branch differently:
#   * GitHub Actions -> GITHUB_REF, e.g. "refs/heads/main"
#   * GitLab CI      -> CI_COMMIT_BRANCH, e.g. "main"
# We normalise both to a bare branch name and only push `latest` when it is
# exactly "main". Anywhere without those variables (e.g. a local run) is treated
# as NOT main, so `latest` is left untouched in the registry.
is_main_branch() {
  local ref=""
  if [[ -n "${GITHUB_REF:-}" ]]; then
    ref="${GITHUB_REF#refs/heads/}"   # strip refs/heads/ if present
  elif [[ -n "${CI_COMMIT_BRANCH:-}" ]]; then
    ref="${CI_COMMIT_BRANCH}"
  fi
  [[ "${ref}" == "main" ]]
}

if is_main_branch; then
  PUSH_LATEST=1
else
  PUSH_LATEST=0
fi

echo "==> EI-ADS image push"
echo "    repo root : ${REPO_ROOT}"
echo "    git sha   : ${GIT_SHA}"
echo "    registry  : ${OCP_REGISTRY}"
echo "    prefix    : ${IMAGE_PREFIX}"
echo "    push latest: $([[ "${PUSH_LATEST}" == "1" ]] && echo "yes (main branch)" || echo "no (not main)")"
echo

# --- Log in to the OpenShift registry ---------------------------------------
# Feed the token on stdin via --password-stdin so it is never exposed as a
# command-line argument. `printf %s` avoids a trailing newline being treated as
# part of the token.
echo "==> Logging in to ${OCP_REGISTRY} as ${OCP_USER}"
if ! printf '%s' "${OCP_TOKEN}" \
      | "${PODMAN}" login --username "${OCP_USER}" --password-stdin "${OCP_REGISTRY}"; then
  echo "ERROR: podman login to ${OCP_REGISTRY} failed." >&2
  exit 1
fi

# --- Discover services ------------------------------------------------------
# A service is any directory under services/ that ships a Containerfile. This is
# the same discovery loop used by scripts/build-images.sh. We use a portable
# `while read` loop (avoids `mapfile`, unavailable on bash 3.2 shipped by macOS).
CONTAINERFILES=()
while IFS= read -r cf; do
  [[ -n "${cf}" ]] && CONTAINERFILES+=("${cf}")
done < <(find services -mindepth 2 -maxdepth 2 -name Containerfile | sort)

if (( ${#CONTAINERFILES[@]} == 0 )); then
  echo "ERROR: no services/<name>/Containerfile found under ${REPO_ROOT}." >&2
  exit 1
fi

echo
echo "==> Found ${#CONTAINERFILES[@]} service(s) with a Containerfile"
echo

pushed_images=()

for containerfile in "${CONTAINERFILES[@]}"; do
  # services/<name>/Containerfile -> <name>
  service_dir="$(dirname "${containerfile}")"
  name="$(basename "${service_dir}")"

  local_sha_tag="${IMAGE_PREFIX}/${name}:${GIT_SHA}"
  remote_sha_tag="${OCP_REGISTRY}/${IMAGE_PREFIX}/${name}:${GIT_SHA}"

  echo "==> [${name}] pushing ${local_sha_tag} -> ${remote_sha_tag}"

  # Push the immutable SHA tag. A failure here aborts the whole script via
  # `set -e`; the explicit `if` gives us a clearer error message first.
  if ! "${PODMAN}" push "${local_sha_tag}" "${remote_sha_tag}"; then
    echo "ERROR: [${name}] failed to push ${remote_sha_tag}. Aborting." >&2
    exit 1
  fi
  pushed_images+=("${remote_sha_tag}")

  # Push `latest` only on the main branch. We re-tag the local latest image for
  # the remote registry and push it, so the registry `latest` always tracks a
  # commit that landed on main.
  if [[ "${PUSH_LATEST}" == "1" ]]; then
    local_latest_tag="${IMAGE_PREFIX}/${name}:latest"
    remote_latest_tag="${OCP_REGISTRY}/${IMAGE_PREFIX}/${name}:latest"

    echo "==> [${name}] pushing ${local_latest_tag} -> ${remote_latest_tag}"
    "${PODMAN}" tag "${local_sha_tag}" "${remote_latest_tag}"
    if ! "${PODMAN}" push "${remote_latest_tag}"; then
      echo "ERROR: [${name}] failed to push ${remote_latest_tag}. Aborting." >&2
      exit 1
    fi
    pushed_images+=("${remote_latest_tag}")
  fi
  echo
done

echo "==> Pushed ${#pushed_images[@]} image reference(s) to ${OCP_REGISTRY}:"
for img in "${pushed_images[@]}"; do
  echo "    - ${img}"
done

# --- 39.4 SBOM publish START ------------------------------------------------
# Every image for this SHA is now in the registry, so publish the per-service
# CycloneDX SBOMs alongside them: scripts/publish-sboms.sh pushes each SBOM as
# its own OCI artifact (${OCP_REGISTRY}/ei-ads/<name>-sbom:<sha>) via oras and,
# when ARTIFACT_REPO_URL is set, uploads them plus a sbom/manifest.json index to
# the project's Nexus/Artifactory repo. It reuses the same service discovery,
# IMAGE_PREFIX, GIT_SHA, and OCP_REGISTRY resolved above, and soft-skips in a
# sandbox where oras is absent (fatal in CI via REQUIRE_SBOM=true). A failure
# here aborts the run via `set -e`.
echo
echo "==> Publishing SBOM artifacts (scripts/publish-sboms.sh)"
"${SCRIPT_DIR}/publish-sboms.sh"
# --- 39.4 SBOM publish END --------------------------------------------------
