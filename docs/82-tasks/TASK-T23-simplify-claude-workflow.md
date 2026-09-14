# TASK-T23 — Minimal workflow restoration (PR #88)

PR #88 removed the old orchestration system and retained owner-authored
issue_comment mentions and the standard Claude OAuth/OIDC authentication.
Human review and merge remain mandatory.

## Correction after live test

The earlier version of this document incorrectly concluded from Claude's
reported capabilities that the Action automatically creates PRs. Run
34792595873 on Issue #89 instead pushed d3926134 and provided a Create PR
link; another actor created PR #90. The official FAQ confirms this is
normal interactive-mode behavior. A capability description was not
evidence of an executed PR creation.

The earlier proposed workflow-wide cancel-in-progress block also does
not belong in the new design. TASK-T24 serializes only its small PR
creation job, with cancellation disabled, to protect the check/create
transaction without cancelling Claude runs on incoming comments.

See [TASK-T24](TASK-T24-claude-pr-and-verification.md) for the dedicated
PAT setup, scoped test/lint commands, 30-turn limit and live acceptance
plan. The old hash/GraphQL/marker/recovery/dispatch system stays removed.
