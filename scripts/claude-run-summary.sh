#!/usr/bin/env bash
# Issue #71: observability without exposing secrets or tool output.
#
# claude-code-action's own `display_report`/`show_full_output` inputs render
# the *entire* turn-by-turn transcript (every tool call and its result) --
# `show_full_output`'s own description warns it "may contain secrets, API
# keys, or other sensitive information" and both are off by default. This
# script deliberately does not turn either on. Instead it reads only scalar
# fields out of the action's `execution_file` JSON output -- model/effort
# names, turn count, estimated cost, and permission-denial count, plus the
# run's exit subtype/error flag -- and writes just those to the job's step
# summary -- never the raw file, never any message content, never uploaded
# as an artifact.
#
# TASK-T21 (Issue #68): extended to report the MAIN session's model/effort
# configuration alongside the actual resolved values, and separately the
# full set of model IDs used anywhere during the run (main loop, Task
# subagents, sidechains, internal helper calls such as compaction) --
# because Claude Code legitimately makes auxiliary/background/subagent
# calls to cheaper models (e.g. Haiku) as part of a normal run, and that is
# not the same thing as the main session itself running on a different
# model. Concretely, per the execution output's own documented shape
# (verified against the pinned @anthropic-ai/claude-agent-sdk@0.3.268
# package's own shipped sdk.d.ts, not assumed):
#   - CONFIGURED_MODEL_SELECTOR/CONFIGURED_EFFORT (env, set by claude.yml
#     from the same values used to build `--model`/`--effort`): what this
#     run was told to use -- a family alias like `sonnet`, not a dated ID.
#   - The system/init message's own `model` field: the MAIN session's
#     actual initialized model after the CLI resolves the selector (e.g.
#     `sonnet` -> `claude-sonnet-5` today). This is main-session-only; it is
#     never the auxiliary/subagent models.
#   - The system/init message's own `effort` field: the effort level the
#     MAIN session will actually send, "after any silent downgrade for the
#     selected model" (the SDK's own documentation) -- so a model that
#     silently can't honor `medium` is visible here, not assumed away.
#   - The latest result message's `modelUsage` map: every model ID actually
#     billed anywhere in the run (main loop + subagents + sidechains +
#     internal calls) -- reported as a plain list of model IDs so Haiku (or
#     any other auxiliary model) showing up here is visibly NOT the same
#     fact as the main session itself switching models, without excluding
#     or suppressing that legitimate auxiliary usage.
set -euo pipefail

: "${EXECUTION_FILE:?EXECUTION_FILE is required}"
: "${CONFIGURED_MODEL_SELECTOR:?CONFIGURED_MODEL_SELECTOR is required}"
: "${CONFIGURED_EFFORT:?CONFIGURED_EFFORT is required}"

if [ ! -f "${EXECUTION_FILE}" ]; then
  echo "No execution file at '${EXECUTION_FILE}' -- Claude may not have started; skipping run summary."
  exit 0
fi

summary=$(jq -r \
  --arg configured_model "${CONFIGURED_MODEL_SELECTOR}" \
  --arg configured_effort "${CONFIGURED_EFFORT}" \
  '
  (map(select(.type == "system" and .subtype == "init")) | last) as $init |
  (($init.model // "unknown")) as $actual_model |
  (($init.effort // null) | if . == null then "none reported" else tostring end) as $actual_effort |
  (map(select(.type == "result")) | last) as $result |
  ((($result.modelUsage // {}) | keys) as $models |
    if ($models | length) == 0 then "none reported" else ($models | join(", ")) end
  ) as $models_used |
  "### Claude Code run summary\n\n" +
  "| Field | Value |\n" +
  "| --- | --- |\n" +
  "| Configured main model selector | `" + $configured_model + "` |\n" +
  "| Configured effort | `" + $configured_effort + "` |\n" +
  "| Actual initialized main model | `" + $actual_model + "` |\n" +
  "| Actual initialized effort | `" + $actual_effort + "` |\n" +
  "| Models used during run (modelUsage) | `" + $models_used + "` |\n" +
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
