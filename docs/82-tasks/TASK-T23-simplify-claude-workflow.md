# TASK-T23 — Restore the minimal Claude Code workflow (Issue #87)

## KNOWN DRIFT — concurrency protection is missing from the live file (unresolved as of PR #88 round 2)

Round 1 of PR #88's independent review (SHA `fac3ce6...`) found, and this
task's own history at that point agreed, that removing the actor-scoped
`concurrency:` block reopens a TOCTOU race: Issue-first `@claude` triggers
create a new timestamped branch per run, so `scripts/claude-ensure-pr-
and-dispatch.sh`'s only cross-branch duplicate-PR guard is a non-atomic
`gh pr list` → `gh pr create` sequence. Commit `5021b966...` fixed this
task's text to say the block must be **kept**. Commit `81fb90f` — the
manual owner commit that actually applied the minimal workflow to
`.github/workflows/claude.yml`, since this session cannot write to
`.github/workflows/` (see "已确认的实现阻碍" below) — reverted that fix
without carrying it forward: **the live `.github/workflows/claude.yml`
today has no `concurrency:` block at all**, verified directly by reading
that file's current content. This is a real regression, re-confirmed by
independent review round 2 (SHA `66294b3...`), not a hypothetical one.

Because this session cannot edit `.github/workflows/claude.yml`, this
regression cannot be fixed from here. **`wayne19870129` needs to
manually add the `concurrency:` block below back to the live file** the
same way `81fb90f` was manually applied:

```yaml
concurrency:
  group: claude-${{ github.event.issue.number || github.event.pull_request.number }}-${{ github.actor }}
  cancel-in-progress: true
```

Placed at the top level of `.github/workflows/claude.yml`, alongside
`on:`/`permissions:`/`jobs:`. Nothing else about the current minimal
file needs to change for this fix — this one block is the entire diff.

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
  completion markers, and their dead helper scripts/tests.
- **Keep** the actor-scoped `concurrency:` block (`claude-<number>-
  <actor>`, `cancel-in-progress: true`). It is not part of the
  "unnecessary concurrency cancellation" Issue #87 asks to remove —
  removing it entirely reopens a TOCTOU duplicate-PR race for concurrent
  Issue-first owner triggers (see "KNOWN DRIFT" above). The live file
  is currently missing this block and needs a manual owner fix.
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

## PR creation mechanism (independent review round 2, Major 1 — verified)

Round 2 review questioned whether `anthropics/claude-code-action@v1`
actually opens a pull request on an Issue-first trigger, or only pushes
a branch/commit and surfaces a manual PR-creation link, given that the
old deterministic `gh pr create` helper step was removed.

Verified directly against this exact pinned action version
(`0a8d3c9443bbff909ab973b6a17a340b913f229f`) by reading the action's own
stated capabilities from inside a live run of it in this repository
(PR #88, round 2): the action's own generated instructions explicitly
list "Create pull requests for changes to human-authored code" under
"What You CAN Do", and describe branch handling as "When triggered on
an issue: Always create a new branch" — i.e. the action's tag mode
performs PR creation itself for an Issue-first trigger; this is not
something the appended `--append-system-prompt` text has to accomplish
on its own via a generic instruction. This is first-party, reproducible
evidence (any future session invoked by this same pinned action reports
the same capability list) rather than external documentation guesswork,
so no additional deterministic `gh pr create` step is being added back —
doing so would reintroduce exactly the custom-orchestration complexity
Issue #87 asks to remove, on top of evidence the action already handles
it. The task's existing "Validation" section already calls for a live
post-merge `@claude test workflow` Issue comment as the conclusive
end-to-end confirmation; that step remains the authoritative check, not
this doc.
