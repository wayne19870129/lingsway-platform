#!/usr/bin/env bash
# Issue #71: observability without exposing secrets or tool output.
#
# claude-code-action's own `display_report`/`show_full_output` inputs render
# the *entire* turn-by-turn transcript (every tool call and its result) --
# `show_full_output`'s own description warns it "may contain secrets, API
# keys, or other sensitive information" and both are off by default. This
# script deliberately does not turn either on. Instead it reads only four
# scalar fields out of the action's `execution_file` JSON output (the model
# name, turn count, estimated cost, and permission-denial count, plus the
# run's exit subtype/error flag) and writes just those to the job's step
# summary -- never the raw file, never any message content, never uploaded
# as an artifact.
set -euo pipefail

: "${EXECUTION_FILE:?EXECUTION_FILE is required}"

if [ ! -f "${EXECUTION_FILE}" ]; then
  echo "No execution file at '${EXECUTION_FILE}' -- Claude may not have started; skipping run summary."
  exit 0
fi

summary=$(jq -r '
  (map(select(.type == "system" and .subtype == "init")) | last | .model // "unknown") as $model |
  (map(select(.type == "result")) | last) as $result |
  "### Claude Code run summary\n\n" +
  "| Field | Value |\n" +
  "| --- | --- |\n" +
  "| Model | `" + $model + "` |\n" +
  "| Turns | " + (($result.num_turns // "unknown") | tostring) + " |\n" +
  "| Estimated cost (USD) | " + (($result.total_cost_usd // "unknown") | tostring) + " |\n" +
  "| Permission denials | " + (($result.permission_denials // []) | length | tostring) + " |\n" +
  "| Exit subtype | `" + ($result.subtype // "unknown") + "` |\n" +
  "| is_error | " + (($result.is_error // false) | tostring) + " |\n"
' "${EXECUTION_FILE}")

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  printf '%s\n' "${summary}" >> "${GITHUB_STEP_SUMMARY}"
else
  printf '%s\n' "${summary}"
fi
