#!/usr/bin/env bash
# Regression tests for scripts/claude-run-summary.sh (Issue #68 / TASK-T21).
#
# Drives the actual script against synthetic `execution_file` JSON shaped
# like the real @anthropic-ai/claude-agent-sdk@0.3.268 SDK message array
# (verified against that exact pinned package version's own shipped
# sdk.d.ts, not guessed), covering:
#   1. Configured selector/effort vs. actual initialized main model/effort
#      are reported separately and correctly, even when a synthetic
#      "downgrade" makes them differ.
#   2. A run whose modelUsage contains BOTH a Sonnet and a Haiku model ID
#      shows both, but the "actual initialized main model" field still
#      correctly identifies Sonnet (from the system/init message) as the
#      main session -- Haiku appearing in modelUsage must never be
#      misreported as the main model.
#   3. No raw transcript/message content (assistant text, tool calls,
#      prompts) ever appears in the summary output -- only the specific
#      scalar/model-name fields this script is designed to extract.
#   4. Missing/absent execution file is a clean no-op, not an error.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_SCRIPT="${SCRIPT_DIR}/../claude-run-summary.sh"

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

SECRET_MARKER="SECRET_PLACEHOLDER_MUST_NEVER_LEAK_sk-ant-fake-1234"

echo "### Scenario 1: Sonnet main + Haiku auxiliary, effort resolved as configured"
cat >"${WORKDIR}/exec1.json" <<EOF
[
  {"type": "system", "subtype": "init", "model": "claude-sonnet-5", "effort": "medium", "session_id": "s1"},
  {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "${SECRET_MARKER}"}]}},
  {"type": "result", "subtype": "success", "is_error": false, "num_turns": 12, "total_cost_usd": 0.234, "permission_denials": [], "modelUsage": {"claude-sonnet-5": {"contextWindow": 200000, "maxOutputTokens": 8192}, "claude-haiku-4-5-20251001": {"contextWindow": 200000, "maxOutputTokens": 8192}}}
]
EOF
out1=$(EXECUTION_FILE="${WORKDIR}/exec1.json" CONFIGURED_MODEL_SELECTOR=sonnet CONFIGURED_EFFORT=medium GITHUB_STEP_SUMMARY= bash "${TARGET_SCRIPT}")
assert "scenario 1: configured selector shown" "$(echo "${out1}" | grep -q '| Configured main model selector | `sonnet` |' && echo true || echo false)"
assert "scenario 1: configured effort shown" "$(echo "${out1}" | grep -q '| Configured effort | `medium` |' && echo true || echo false)"
assert "scenario 1: actual main model is Sonnet, not Haiku" "$(echo "${out1}" | grep -q '| Actual initialized main model | `claude-sonnet-5` |' && echo true || echo false)"
assert "scenario 1: actual effort shown" "$(echo "${out1}" | grep -q '| Actual initialized effort | `medium` |' && echo true || echo false)"
assert "scenario 1: modelUsage lists BOTH Sonnet and Haiku" "$(echo "${out1}" | grep -E '\| Models used during run \(modelUsage\) \| `.*claude-sonnet-5.*` \|' | grep -q 'claude-haiku-4-5-20251001' && echo true || echo false)"
assert "scenario 1: Haiku's presence in modelUsage does not become the main model" "$(echo "${out1}" | grep -q '| Actual initialized main model | `claude-haiku-4-5-20251001` |' && echo false || echo true)"
assert "scenario 1: turns/cost/denials/exit reported correctly" "$(echo "${out1}" | grep -q '| Turns | 12 |' && echo "${out1}" | grep -q '| Estimated cost (USD) | 0.234 |' && echo "${out1}" | grep -q '| Permission denials | 0 |' && echo "${out1}" | grep -q '| Exit subtype | `success` |' && echo "${out1}" | grep -q '| is_error | false |' && echo true || echo false)"
assert "scenario 1: raw assistant/secret text never leaks into the summary" "$(echo "${out1}" | grep -q "${SECRET_MARKER}" && echo false || echo true)"

echo "### Scenario 2 (configured vs. actual mismatch): model silently resolves differently than requested, and effort is not honored by the resolved model"
cat >"${WORKDIR}/exec2.json" <<EOF
[
  {"type": "system", "subtype": "init", "model": "claude-sonnet-4-6", "effort": null, "session_id": "s2"},
  {"type": "result", "subtype": "error_max_turns", "is_error": true, "num_turns": 31, "total_cost_usd": 0.64365, "permission_denials": [{"tool":"Bash"},{"tool":"Bash"}], "modelUsage": {"claude-sonnet-4-6": {"contextWindow": 200000, "maxOutputTokens": 8192}}}
]
EOF
out2=$(EXECUTION_FILE="${WORKDIR}/exec2.json" CONFIGURED_MODEL_SELECTOR=sonnet CONFIGURED_EFFORT=medium GITHUB_STEP_SUMMARY= bash "${TARGET_SCRIPT}")
assert "scenario 2: configured selector still reported as requested (sonnet)" "$(echo "${out2}" | grep -q '| Configured main model selector | `sonnet` |' && echo true || echo false)"
assert "scenario 2: actual initialized main model differs from configured selector, and is visible" "$(echo "${out2}" | grep -q '| Actual initialized main model | `claude-sonnet-4-6` |' && echo true || echo false)"
assert "scenario 2: null actual effort reported as none, not silently omitted" "$(echo "${out2}" | grep -q '| Actual initialized effort | `none reported` |' && echo true || echo false)"
assert "scenario 2: permission denial count reported correctly" "$(echo "${out2}" | grep -q '| Permission denials | 2 |' && echo true || echo false)"
assert "scenario 2: is_error/exit subtype reported correctly" "$(echo "${out2}" | grep -q '| Exit subtype | `error_max_turns` |' && echo "${out2}" | grep -q '| is_error | true |' && echo true || echo false)"

echo "### Scenario 3: missing execution file -> clean no-op, not an error"
: >"${WORKDIR}/no-such-file-marker"
rm -f "${WORKDIR}/no-such-file-marker"
out3=$(EXECUTION_FILE="${WORKDIR}/does-not-exist.json" CONFIGURED_MODEL_SELECTOR=sonnet CONFIGURED_EFFORT=medium GITHUB_STEP_SUMMARY= bash "${TARGET_SCRIPT}")
assert "scenario 3: missing file produces an explanatory message, not a crash" "$(echo "${out3}" | grep -q 'Claude may not have started' && echo true || echo false)"

echo
if [ "${failures}" -gt 0 ]; then
  echo "FAILED: ${failures} assertion(s) failed."
  exit 1
fi
echo "All assertions passed."
