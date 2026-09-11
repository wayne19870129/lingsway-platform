#!/usr/bin/env bash
# Reproducible, fail-closed application of 0001-expose-routing-principal.patch
# (ADR-016 Decision 3, Candidate B) against a pinned Marzban source tree.
#
# Usage:
#   apply_patch.sh --check <source-tree-root>   # dry-run drift guard only
#   apply_patch.sh <source-tree-root>           # apply for real
#
# <source-tree-root> must contain app/models/user.py at the path the patch
# targets (i.e. it is the root of a Marzban checkout/extraction, pinned to
# commit 7f396db3e703d71a28060bc9ce4a532ec64cb1f4 / tag v0.8.4 -- this
# script does not fetch or verify the pinned commit itself; that is the
# caller's responsibility, e.g. a Docker build stage that clones/extracts
# the pinned commit before invoking this script).
#
# Fail-closed contract: any failure (missing file, patch context drift,
# `git apply` rejecting the hunks) exits non-zero and applies nothing.
# There is no fallback to "continue unpatched" -- a caller that ignores a
# non-zero exit here and proceeds to build/start Marzban anyway is doing
# so against this script's explicit contract.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_FILE="${SCRIPT_DIR}/0001-expose-routing-principal.patch"

usage() {
  echo "usage: $0 [--check] <source-tree-root>" >&2
  exit 2
}

CHECK_ONLY=0
if [[ "${1:-}" == "--check" ]]; then
  CHECK_ONLY=1
  shift
fi

SOURCE_ROOT="${1:-}"
if [[ -z "${SOURCE_ROOT}" ]]; then
  usage
fi

if [[ ! -f "${PATCH_FILE}" ]]; then
  echo "FAIL-CLOSED: patch file not found: ${PATCH_FILE}" >&2
  exit 1
fi

TARGET_FILE="${SOURCE_ROOT}/app/models/user.py"
if [[ ! -f "${TARGET_FILE}" ]]; then
  echo "FAIL-CLOSED: expected upstream file not found: ${TARGET_FILE}" >&2
  echo "This usually means the pinned source tree layout changed, or the" >&2
  echo "wrong directory was passed as <source-tree-root>." >&2
  exit 1
fi

cd "${SOURCE_ROOT}"

if [[ "${CHECK_ONLY}" -eq 1 ]]; then
  if git apply --check "${PATCH_FILE}" 2>/tmp/marzban_patch_check.err; then
    echo "OK: patch applies cleanly to $(realpath "${TARGET_FILE}")"
    exit 0
  else
    echo "FAIL-CLOSED: patch does not apply cleanly (drift detected)." >&2
    cat /tmp/marzban_patch_check.err >&2
    echo "This means the pinned upstream source has drifted from what" >&2
    echo "0001-expose-routing-principal.patch expects. Do not build or" >&2
    echo "start Marzban from this source tree with the assumption that" >&2
    echo "routing_principal is present -- it is not." >&2
    exit 1
  fi
fi

if ! git apply --check "${PATCH_FILE}" 2>/tmp/marzban_patch_check.err; then
  echo "FAIL-CLOSED: patch does not apply cleanly; refusing to apply partially or fall back to unpatched source." >&2
  cat /tmp/marzban_patch_check.err >&2
  exit 1
fi

git apply "${PATCH_FILE}"
echo "OK: applied 0001-expose-routing-principal.patch to $(realpath "${TARGET_FILE}")"
