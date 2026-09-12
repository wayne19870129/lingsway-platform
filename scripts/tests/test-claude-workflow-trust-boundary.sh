#!/usr/bin/env bash
# Static guard against the Issue #71 review round 1 "Critical" finding
# regressing: post-Claude privileged steps in the `claude` job must never
# execute scripts/claude-ensure-pr-and-dispatch.sh or
# scripts/claude-run-summary.sh straight out of the working tree, because
# for a PR-rework (open PR) trigger that working tree gets switched to the
# PR's own (in a public repository, attacker-controlled) head branch by
# claude-code-action. They must execute only the pre-Claude snapshot copied
# to $RUNNER_TEMP, and the checkouts that precede any privileged execution
# must be pinned to a trusted ref.
#
# This can't exercise real GitHub Actions branch-switching behavior, so it
# instead statically asserts the *shape* of .github/workflows/claude.yml
# that the trust boundary depends on. A regression that reintroduces a
# direct `bash scripts/claude-*.sh` call in the `claude` job, or drops the
# snapshot step, or stops pinning a trusted `ref:` on the checkouts that
# precede any privileged execution, fails this test.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKFLOW="${SCRIPT_DIR}/../../.github/workflows/claude.yml"

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

# Isolate the `claude:` job's YAML block (from its own "  claude:" job
# header up to, but not including, the next top-level "  <name>:" job
# header) so assertions below can't accidentally match text belonging to a
# different job.
claude_job=$(awk '
  /^  claude:$/ { capture = 1 }
  capture && /^  [A-Za-z0-9_-]+:$/ && !/^  claude:$/ { capture = 0 }
  capture { print }
' "${WORKFLOW}")

claude_recovery_job=$(awk '
  /^  claude-recovery:$/ { capture = 1 }
  capture { print }
' "${WORKFLOW}")

assert "claude job's initial checkout pins ref to the default branch" \
  "$(echo "${claude_job}" | grep -A6 'uses: actions/checkout' | head -10 | grep -q "ref: \${{ github.event.repository.default_branch }}" && echo true || echo false)"

assert "claude job snapshots both trusted scripts before claude-code-action runs" \
  "$(awk '/id: snapshot-scripts/{t=NR} /uses: anthropics\/claude-code-action/{a=NR} END{print (t>0 && a>0 && t<a) ? "true" : "false"}' <<<"${claude_job}")"

assert "snapshot-scripts step copies claude-ensure-pr-and-dispatch.sh" \
  "$(echo "${claude_job}" | grep -q 'cp scripts/claude-ensure-pr-and-dispatch.sh scripts/claude-run-summary.sh' && echo true || echo false)"

assert "ensure-pr step never invokes scripts/claude-ensure-pr-and-dispatch.sh directly" \
  "$(echo "${claude_job}" | grep -qE '^\s*bash scripts/claude-ensure-pr-and-dispatch\.sh\s*$' && echo false || echo true)"
assert "ensure-pr step invokes the \$SCRIPTS_DIR snapshot copy" \
  "$(echo "${claude_job}" | grep -q '"\${SCRIPTS_DIR}/claude-ensure-pr-and-dispatch.sh"' && echo true || echo false)"

assert "run-summary step never invokes scripts/claude-run-summary.sh directly" \
  "$(echo "${claude_job}" | grep -qE '^\s*run: bash scripts/claude-run-summary\.sh\s*$' && echo false || echo true)"
assert "run-summary step invokes the \$SCRIPTS_DIR snapshot copy" \
  "$(echo "${claude_job}" | grep -q '"\${SCRIPTS_DIR}/claude-run-summary.sh"' && echo true || echo false)"

assert "claude-recovery job pins its checkout ref to the default branch" \
  "$(echo "${claude_recovery_job}" | grep -A6 'uses: actions/checkout' | head -10 | grep -q "ref: \${{ github.event.repository.default_branch }}" && echo true || echo false)"
assert "claude-recovery job runs the script directly from its (trusted) working tree" \
  "$(echo "${claude_recovery_job}" | grep -qE '^\s*run: bash scripts/claude-ensure-pr-and-dispatch\.sh\s*$' && echo true || echo false)"

echo
if [ "${failures}" -gt 0 ]; then
  echo "FAILED: ${failures} assertion(s) failed."
  exit 1
fi
echo "All assertions passed."
