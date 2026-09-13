#!/usr/bin/env bash
# Static guard for the Issue #68 / TASK-T21 model/effort configuration in
# .github/workflows/claude.yml. Real GitHub Actions expression evaluation
# and the pinned action's own claude_args parsing can't be exercised
# outside github.com, so this asserts the *shape* the fix depends on:
#   - the main `claude` job's claude_args configures `--model` via the
#     `sonnet` rolling alias (env.CLAUDE_MODEL_SELECTOR), never a dated
#     concrete model ID such as `claude-sonnet-5`;
#   - it configures `--effort` via `medium` (env.CLAUDE_EFFORT);
#   - `--max-turns 30` is unchanged;
#   - the run-summary step is wired to receive the same
#     CONFIGURED_MODEL_SELECTOR/CONFIGURED_EFFORT values, from the same
#     job-level env block claude_args itself reads -- so the two can never
#     silently drift apart;
#   - no hardcoded dated Sonnet model ID (e.g. `claude-sonnet-5`) appears
#     anywhere in the file as an actual configuration value (only allowed
#     inside comments, e.g. explaining what the alias resolves to today).
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

claude_job=$(awk '
  /^  claude:$/ { capture = 1 }
  capture && /^  [A-Za-z0-9_-]+:$/ && !/^  claude:$/ { capture = 0 }
  capture { print }
' "${WORKFLOW}")

assert "claude job sets CLAUDE_MODEL_SELECTOR to the rolling 'sonnet' alias" \
  "$(echo "${claude_job}" | grep -qE '^\s*CLAUDE_MODEL_SELECTOR:\s*sonnet\s*$' && echo true || echo false)"

assert "claude job sets CLAUDE_EFFORT to 'medium'" \
  "$(echo "${claude_job}" | grep -qE '^\s*CLAUDE_EFFORT:\s*medium\s*$' && echo true || echo false)"

assert "claude_args passes --model referencing the CLAUDE_MODEL_SELECTOR env var" \
  "$(echo "${claude_job}" | grep -qF -- '--model ${{ env.CLAUDE_MODEL_SELECTOR }}' && echo true || echo false)"

assert "claude_args passes --effort referencing the CLAUDE_EFFORT env var" \
  "$(echo "${claude_job}" | grep -qF -- '--effort ${{ env.CLAUDE_EFFORT }}' && echo true || echo false)"

assert "claude_args still passes --max-turns 30" \
  "$(echo "${claude_job}" | grep -qF -- '--max-turns 30' && echo true || echo false)"

assert "run-summary step's CONFIGURED_MODEL_SELECTOR reads the same job env var" \
  "$(echo "${claude_job}" | grep -qF 'CONFIGURED_MODEL_SELECTOR: ${{ env.CLAUDE_MODEL_SELECTOR }}' && echo true || echo false)"

assert "run-summary step's CONFIGURED_EFFORT reads the same job env var" \
  "$(echo "${claude_job}" | grep -qF 'CONFIGURED_EFFORT: ${{ env.CLAUDE_EFFORT }}' && echo true || echo false)"

# The model selector must never be hardcoded to a dated concrete ID as an
# actual configuration value. Comments are allowed to mention the ID (e.g.
# "sonnet -> claude-sonnet-5") to explain what the alias resolves to today,
# so this check only looks at non-comment lines.
hardcoded_lines=$(echo "${claude_job}" | grep -n 'claude-sonnet-5' | grep -vE '^\s*[0-9]+:\s*#' || true)
assert "no hardcoded 'claude-sonnet-5' model ID used as an actual config value" \
  "$([ -z "${hardcoded_lines}" ] && echo true || echo false)"

# --model must not be left unspecified (no bare "--model" with no selector
# after it) and must not silently point at an unspecified/default value.
assert "claude_args does not configure --model with an empty/unset selector" \
  "$(echo "${claude_job}" | grep -qE '^\s*--model\s*$' && echo false || echo true)"

echo
if [ "${failures}" -gt 0 ]; then
  echo "FAILED: ${failures} assertion(s) failed."
  exit 1
fi
echo "All assertions passed."
