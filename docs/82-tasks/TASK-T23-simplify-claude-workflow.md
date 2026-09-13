# TASK-T23 — Restore the minimal Claude Code workflow (Issue #87)

## Goal

Restore the repository's original development loop:

`wayne19870129` comments `@claude` on an Issue → Claude implements the
request and opens a PR → normal PR CI runs → a human reviews and merges.

## Root cause audit

The previous `.github/workflows/claude.yml` had grown to more than 500
lines. In addition to `issue_comment`, it accepted inline review comments
and manual recovery dispatches. It used workflow-level cancellation, a
deduplication job, comment hashes and completion markers, GraphQL PR
lookups, redirect comments, trusted helper-script snapshots, manual
CI/Security/Risk dispatch, and a recovery job. Those layers obscured the
owner/mention gate and routed PR creation and CI through the workflow's
`GITHUB_TOKEN`, which produced approval-gated PR checks.

The workflow did not yet consume the proposed
`CLAUDE_AUTOMATION_APP_ID` or `CLAUDE_AUTOMATION_APP_PRIVATE_KEY` secrets,
and the repository secret inventory confirms they are not configured.
The only configured Claude authentication secret is
`CLAUDE_CODE_OAUTH_TOKEN`.

## Implementation

- Keep only `issue_comment` with `created` events.
- Gate the sole job directly on comment author `wayne19870129` and the
  presence of `@claude`.
- Keep `contents`, `pull-requests`, and `issues` write permissions.
- Keep `id-token: write`, which is required by the standard Claude Code
  GitHub App authentication used by `anthropics/claude-code-action`.
- Authenticate Claude only with `CLAUDE_CODE_OAUTH_TOKEN`; do not mint or
  pass a custom GitHub App token.
- Let the Claude Code Action perform its normal branch/PR behavior so PR
  workflows run from the normal GitHub App identity.
- Remove custom PR creation, workflow dispatch, recovery, deduplication,
  concurrency cancellation, completion markers, and their dead helper
  scripts/tests.
- Retain an explicit instruction that Claude must not approve or merge
  PRs, close issues, delete branches, or trigger deployments.

## Validation

- Parse all workflow YAML files.
- Run `actionlint` when available.
- Run repository lint/tests relevant to the changed workflow surface.
- After merge, post `@claude test workflow` on a new Issue and confirm the
  action starts, creates one PR, and its normal PR checks start without an
  approval gate. This live trigger cannot validate the new workflow before
  the PR is merged because GitHub loads `issue_comment` workflows from the
  default branch.

## Safety boundary

Claude creates or updates a PR only. A human reviews and merges. No step
approves or merges a PR, closes an Issue, deletes a branch, or deploys.
