#!/usr/bin/env bash
# scripts/itest-env.sh
# =====================
# Export the environment testcontainers needs to talk to a running Podman
# machine on macOS, then (optionally) run the command passed as arguments.
#
# Why this is needed
# ------------------
# testcontainers looks for a Docker-compatible daemon at DOCKER_HOST (or the
# default /var/run/docker.sock). On macOS+Podman the live API socket lives at a
# machine-specific path under $TMPDIR, and the conventional /var/run/docker.sock
# symlink is often stale, so DOCKER_HOST must be set explicitly. Podman also
# does not run the testcontainers "Ryuk" reaper reliably, so we disable it and
# rely on testcontainers' own context-manager cleanup.
#
# Usage
# -----
#   source scripts/itest-env.sh              # export vars into the current shell
#   scripts/itest-env.sh <command> [args...] # run a command with the vars set
#
# Example
#   scripts/itest-env.sh .venv-itest/bin/python -m pytest tests/integration \
#       -m integration -v

set -euo pipefail

_machine="${PODMAN_MACHINE:-podman-machine-default}"

_sock="$(podman machine inspect --format '{{.ConnectionInfo.PodmanSocket.Path}}' "${_machine}" 2>/dev/null | head -1)"
if [[ -z "${_sock}" || ! -S "${_sock}" ]]; then
  echo "itest-env: could not resolve a running Podman socket for machine '${_machine}'." >&2
  echo "itest-env: is the machine up?  Run:  podman machine start" >&2
  exit 1
fi

export DOCKER_HOST="unix://${_sock}"
# Ryuk (the reaper container) is unreliable under Podman; disable it. Cleanup is
# handled by testcontainers' context managers in the test code.
export TESTCONTAINERS_RYUK_DISABLED="true"
# Point testcontainers' socket-mount logic at the same socket.
export TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE="${_sock}"

echo "itest-env: DOCKER_HOST=${DOCKER_HOST}" >&2
echo "itest-env: TESTCONTAINERS_RYUK_DISABLED=${TESTCONTAINERS_RYUK_DISABLED}" >&2

if [[ "$#" -gt 0 ]]; then
  exec "$@"
fi
