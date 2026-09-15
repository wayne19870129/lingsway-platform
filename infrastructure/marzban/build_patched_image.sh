#!/usr/bin/env bash
# Reproducible, fail-closed build of a patched Marzban image carrying the
# routing_principal source patch (ADR-016 Decision 3, Candidate B).
# TASK-T16 Phase 2C2.
#
# This script does not vendor Marzban source into this repository. It
# fetches the exact pinned upstream commit into a temporary directory,
# verifies it really is that commit, verifies the local source tree still
# matches this repository's recorded contract, applies the repository-
# owned patch, builds an image from the result, attaches immutable
# identity labels, and deletes the temporary source tree. Nothing from
# the fetched tree is committed to Git.
#
# Usage:
#   infrastructure/marzban/build_patched_image.sh [image-tag]
#
# image-tag defaults to lingsway/marzban:v0.8.4-routing-principal.
#
# Fail-closed contract: any step failing (wrong commit, source-tree hash
# mismatch, patch not applicable, patch not actually present in the
# result, docker build failure) exits non-zero and leaves no image
# tagged, no temporary source tree behind, and no partial build treated
# as usable. There is no "skip the check and build anyway" option, and
# none of skip_patch_check/force/override/unsafe/bypass exist here or may
# be added later -- see AGENTS.md rule 3 on external-system write paths.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PATCHES_DIR="${SCRIPT_DIR}/patches"
UPSTREAM_URL="https://github.com/Gozargah/Marzban.git"

IMAGE_TAG="${1:-lingsway/marzban:v0.8.4-routing-principal}"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "FAIL-CLOSED: required command is unavailable: $1" >&2
    exit 1
  }
}

require_cmd git
require_cmd docker
require_cmd python3

PINNED_COMMIT="$(python3 -c 'import sys; sys.path.insert(0, "'"${PATCHES_DIR}"'"); from pinned_upstream_manifest import PINNED_COMMIT; print(PINNED_COMMIT)')"
PINNED_TAG="$(python3 -c 'import sys; sys.path.insert(0, "'"${PATCHES_DIR}"'"); from pinned_upstream_manifest import PINNED_TAG; print(PINNED_TAG)')"

[[ "${PINNED_COMMIT}" =~ ^[0-9a-f]{40}$ ]] || {
  echo "FAIL-CLOSED: PINNED_COMMIT from pinned_upstream_manifest.py is not a valid 40-char git commit SHA: ${PINNED_COMMIT}" >&2
  exit 1
}

WORK_DIR="$(mktemp -d -t lingsway-marzban-build.XXXXXX)"
SRC_DIR="${WORK_DIR}/src"

cleanup() {
  rm -rf "${WORK_DIR}"
}
trap cleanup EXIT

echo "[build_patched_image] step 1-2/10: fetching pinned upstream commit ${PINNED_COMMIT} (tag ${PINNED_TAG}) into a temporary directory"
mkdir -p "${SRC_DIR}"
git -C "${SRC_DIR}" init -q
git -C "${SRC_DIR}" remote add origin "${UPSTREAM_URL}"
git -C "${SRC_DIR}" fetch --depth 1 origin "${PINNED_COMMIT}"
git -C "${SRC_DIR}" checkout -q FETCH_HEAD

echo "[build_patched_image] step 3/10: verifying checked-out HEAD matches the pinned commit exactly"
ACTUAL_COMMIT="$(git -C "${SRC_DIR}" rev-parse HEAD)"
if [[ "${ACTUAL_COMMIT}" != "${PINNED_COMMIT}" ]]; then
  echo "FAIL-CLOSED: checked-out HEAD (${ACTUAL_COMMIT}) does not match the pinned commit (${PINNED_COMMIT})." >&2
  echo "Refusing to build from an unverified/unexpected upstream ref -- never latest/master, never a silent substitute." >&2
  exit 1
fi

echo "[build_patched_image] step 4/10: verifying local source-tree contract (file hashes, Xray email formula, pinned dependency versions)"
python3 "${PATCHES_DIR}/verify_local_source_tree.py" "${SRC_DIR}"

echo "[build_patched_image] step 5/10: checking routing_principal patch applies cleanly (dry run)"
"${PATCHES_DIR}/apply_patch.sh" --check "${SRC_DIR}"

echo "[build_patched_image] step 6/10: applying routing_principal patch"
"${PATCHES_DIR}/apply_patch.sh" "${SRC_DIR}"

echo "[build_patched_image] step 7/10: verifying the patch result exposes the routing_principal contract"
USER_PY="${SRC_DIR}/app/models/user.py"
grep -q 'routing_principal: str = Field(default=""' "${USER_PY}" || {
  echo "FAIL-CLOSED: patched app/models/user.py does not define the expected routing_principal field." >&2
  exit 1
}
grep -q 'def compute_routing_principal' "${USER_PY}" || {
  echo "FAIL-CLOSED: patched app/models/user.py is missing compute_routing_principal()." >&2
  exit 1
}
grep -q 'routing_principal: str = Field(default="", exclude=True)' "${USER_PY}" || {
  echo "FAIL-CLOSED: patched app/models/user.py does not exclude routing_principal on SubscriptionUserResponse." >&2
  exit 1
}

echo "[build_patched_image] step 8-9/10: building patched image ${IMAGE_TAG} with immutable identity labels"
docker build \
  --tag "${IMAGE_TAG}" \
  --label "org.lingsway.marzban.upstream-commit=${PINNED_COMMIT}" \
  --label "org.lingsway.marzban.upstream-tag=${PINNED_TAG}" \
  --label "org.lingsway.marzban.routing-principal-patch=applied" \
  --label "org.lingsway.marzban.patch-id=0001-expose-routing-principal" \
  --label "org.lingsway.marzban.contract-version=1" \
  "${SRC_DIR}"

echo "[build_patched_image] step 10/10: cleaning up temporary source tree"
# (handled by the EXIT trap; nothing further to do here)

echo "[build_patched_image] OK: built ${IMAGE_TAG} from pinned commit ${PINNED_COMMIT} with routing_principal applied"
