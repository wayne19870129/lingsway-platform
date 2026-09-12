#!/usr/bin/env bash
# Regression tests for scripts/claude-ensure-pr-and-dispatch.sh.
#
# GitHub Actions/gh CLI behavior cannot be exercised for real outside
# github.com, so this drives the actual script against a local temporary
# bare git remote plus a `gh` stub on PATH that implements the real
# `--json`/`--jq` semantics the script depends on (via real `jq`, not a
# hardcoded guess) and records every invocation to a log file this test
# asserts against.
#
# Scenarios covered:
#   1. No existing PR, branch has new commits ahead of base -> creates a PR,
#      dispatches all three workflows, reports pr_number.
#   2. Existing PR for the branch, branch head differs from PRE_HEAD_SHA ->
#      reuses the PR (never calls `gh pr create`), still dispatches.
#   3. Branch has no commits ahead of base -> no-op, no `gh` calls at all.
#   4. REGRESSION (PR #72 independent review round 1, Major 1 -- false
#      completion marker): branch already has an existing PR AND is already
#      ahead of base *before this run* (PRE_HEAD_SHA equals the branch's
#      current head) -> this run must NOT call `gh pr create`, must NOT
#      call `gh workflow run` (nothing new to check), and must NOT report a
#      pr_number -- so claude.yml's completion-marker condition (which
#      checks `steps.ensure-pr.outputs.pr_number != ''`) cannot mistake "PR
#      already existed" for "this run completed the triggering
#      instruction".
#   5. REGRESSION (PR #72 independent review round 1, Major 2 -- duplicate
#      PR for the same Issue): Issue-first branch, no PR yet for THIS
#      branch, but another branch already has an open PR whose name matches
#      this Issue's branch-naming convention -> refuses (nonzero exit), no
#      `gh pr create` call.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_SCRIPT="${SCRIPT_DIR}/../claude-ensure-pr-and-dispatch.sh"

WORKDIR=$(mktemp -d)
trap 'rm -rf "${WORKDIR}"' EXIT

failures=0
assert() {
  local description="$1" condition="$2"
  if [ "${condition}" = "true" ]; then
    echo "PASS: ${description}"
  else
    echo "FAIL: ${description}"
    failures=$((failures + 1))
  fi
}

# --- Build a local bare "origin" remote with a base branch and feature
# branches, so the real `git ls-remote`/`git rev-list` calls in the target
# script have something real to operate against. ---
REMOTE="${WORKDIR}/origin.git"
git init --bare -q "${REMOTE}"

SRC="${WORKDIR}/src"
git init -q "${SRC}"
git -C "${SRC}" config user.email test@example.com
git -C "${SRC}" config user.name "Test"
git -C "${SRC}" remote add origin "${REMOTE}"

git -C "${SRC}" checkout -qb main
echo base >"${SRC}/file.txt"
git -C "${SRC}" add file.txt
git -C "${SRC}" commit -qm "base commit"
git -C "${SRC}" push -q origin main

# Branch with one commit ahead of base (used by scenarios 1/2/4).
git -C "${SRC}" checkout -qb feature-branch
echo change >>"${SRC}/file.txt"
git -C "${SRC}" commit -qam "feature commit"
git -C "${SRC}" push -q origin feature-branch
FEATURE_SHA=$(git -C "${SRC}" rev-parse feature-branch)

# Branch identical to base (used by scenario 3).
git -C "${SRC}" checkout -qb empty-branch main
git -C "${SRC}" push -q origin empty-branch

# Issue-first branch for scenario 5 (this Issue already has an open PR on a
# *different* branch, matching the default tag-mode naming convention).
git -C "${SRC}" checkout -qb claude/issue-99-newrun main
echo issue-fix >>"${SRC}/file.txt"
git -C "${SRC}" commit -qam "issue fix commit"
git -C "${SRC}" push -q origin claude/issue-99-newrun

# Run the target script from a fresh clone, the same way the real workflow
# step does (its own checkout, not the dev repo above).
RUNDIR="${WORKDIR}/rundir"
git clone -q "${REMOTE}" "${RUNDIR}"
cd "${RUNDIR}"

# --- `gh` stub: records every invocation, answers `pr list` using real jq
# against a canned dataset this test controls per scenario. ---
GH_LOG="${WORKDIR}/gh.log"
GH_EXISTING_PRS_JSON="${WORKDIR}/existing-prs.json"
GH_OPEN_PRS_JSON="${WORKDIR}/open-prs.json"
GH_STUB_DIR="${WORKDIR}/bin"
mkdir -p "${GH_STUB_DIR}"
cat >"${GH_STUB_DIR}/gh" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
echo "gh $*" >>"${GH_LOG}"

if [ "$1" = "pr" ] && [ "$2" = "list" ]; then
  args=("$@")
  json_fields=""
  jq_filter=""
  state=""
  for ((i = 0; i < ${#args[@]}; i++)); do
    case "${args[$i]}" in
      --json) json_fields="${args[$((i + 1))]}" ;;
      --jq) jq_filter="${args[$((i + 1))]}" ;;
      --state) state="${args[$((i + 1))]}" ;;
    esac
  done
  if [ "${json_fields}" = "number,headRefName" ] || [ "${state}" = "open" ]; then
    src="${GH_OPEN_PRS_JSON}"
  else
    src="${GH_EXISTING_PRS_JSON}"
  fi
  if [ -n "${jq_filter}" ]; then
    jq -r "${jq_filter}" "${src}"
  else
    cat "${src}"
  fi
  exit 0
fi

if [ "$1" = "pr" ] && [ "$2" = "create" ]; then
  echo "https://github.com/fakeowner/fakerepo/pull/999"
  exit 0
fi

if [ "$1" = "workflow" ] && [ "$2" = "run" ]; then
  exit 0
fi

echo "unstubbed gh invocation: $*" >&2
exit 1
STUB
chmod +x "${GH_STUB_DIR}/gh"

export PATH="${GH_STUB_DIR}:${PATH}"
export GH_LOG GH_EXISTING_PRS_JSON GH_OPEN_PRS_JSON
export REPO="fakeowner/fakerepo"
export BASE_BRANCH="main"

echo "### Scenario 1: no existing PR -> should create one"
echo '[]' >"${GH_EXISTING_PRS_JSON}"
echo '[]' >"${GH_OPEN_PRS_JSON}"
: >"${GH_LOG}"
GITHUB_OUTPUT="${WORKDIR}/out1" BRANCH="feature-branch" PRE_HEAD_SHA="" ISSUE_NUMBER="" \
  bash "${TARGET_SCRIPT}"
out1=$(cat "${WORKDIR}/out1")
assert "scenario 1: pr_number=999" "$(echo "${out1}" | grep -qx 'pr_number=999' && echo true || echo false)"
assert "scenario 1: gh pr create was called" "$(grep -q 'gh pr create' "${GH_LOG}" && echo true || echo false)"
assert "scenario 1: all three workflows dispatched" "$([ "$(grep -c 'gh workflow run' "${GH_LOG}")" -eq 3 ] && echo true || echo false)"

echo "### Scenario 2: existing PR -> reuse, no duplicate create"
echo '[{"number": 42}]' >"${GH_EXISTING_PRS_JSON}"
: >"${GH_LOG}"
GITHUB_OUTPUT="${WORKDIR}/out2" BRANCH="feature-branch" PRE_HEAD_SHA="0000000000000000000000000000000000000" ISSUE_NUMBER="" \
  bash "${TARGET_SCRIPT}"
out2=$(cat "${WORKDIR}/out2")
assert "scenario 2: pr_number=42" "$(echo "${out2}" | grep -qx 'pr_number=42' && echo true || echo false)"
assert "scenario 2: gh pr create was NOT called" "$(grep -q 'gh pr create' "${GH_LOG}" && echo false || echo true)"

echo "### Scenario 3: no commits ahead of base -> no-op, no gh calls"
: >"${GH_LOG}"
GITHUB_OUTPUT="${WORKDIR}/out3" BRANCH="empty-branch" PRE_HEAD_SHA="" ISSUE_NUMBER="" \
  bash "${TARGET_SCRIPT}"
out3=$(cat "${WORKDIR}/out3" 2>/dev/null || true)
assert "scenario 3: no pr_number reported" "$(echo "${out3}" | grep -q 'pr_number=' && echo false || echo true)"
assert "scenario 3: no gh calls at all" "$([ ! -s "${GH_LOG}" ] && echo true || echo false)"

echo "### Scenario 4 (Major 1 regression): existing PR already ahead of base BEFORE this run -> must not create/dispatch/report pr_number"
echo '[{"number": 42}]' >"${GH_EXISTING_PRS_JSON}"
: >"${GH_LOG}"
GITHUB_OUTPUT="${WORKDIR}/out4" BRANCH="feature-branch" PRE_HEAD_SHA="${FEATURE_SHA}" ISSUE_NUMBER="" \
  bash "${TARGET_SCRIPT}"
out4=$(cat "${WORKDIR}/out4" 2>/dev/null || true)
assert "scenario 4: no pr_number reported (false-completion guard)" "$(echo "${out4}" | grep -q 'pr_number=' && echo false || echo true)"
assert "scenario 4: gh pr create was NOT called" "$(grep -q 'gh pr create' "${GH_LOG}" && echo false || echo true)"
assert "scenario 4: no CI/Security/Risk dispatch (nothing new to check)" "$([ ! -s "${GH_LOG}" ] && echo true || echo false)"

echo "### Scenario 5 (Major 2 regression): Issue already has an open PR on a different branch -> refuse, no duplicate PR"
echo '[]' >"${GH_EXISTING_PRS_JSON}"
echo '[{"number": 77, "headRefName": "claude/issue-99-oldrun"}]' >"${GH_OPEN_PRS_JSON}"
: >"${GH_LOG}"
set +e
GITHUB_OUTPUT="${WORKDIR}/out5" BRANCH="claude/issue-99-newrun" PRE_HEAD_SHA="" ISSUE_NUMBER="99" \
  bash "${TARGET_SCRIPT}"
scenario5_exit=$?
set -e
assert "scenario 5: exits nonzero (refuses to create a second PR)" "$([ "${scenario5_exit}" -ne 0 ] && echo true || echo false)"
assert "scenario 5: gh pr create was NOT called" "$(grep -q 'gh pr create' "${GH_LOG}" && echo false || echo true)"
assert "scenario 5: no CI/Security/Risk dispatch" "$(grep -q 'gh workflow run' "${GH_LOG}" && echo false || echo true)"

echo
if [ "${failures}" -gt 0 ]; then
  echo "FAILED: ${failures} assertion(s) failed."
  exit 1
fi
echo "All assertions passed."
