# TASK-T26 — Trusted writer for Claude PR-rework pushes

## Goal and scope

Issue #95: eliminate the manual **Approve workflows to run** gate that
appears when an owner-authorized `@claude` comment reworks an
already-open, automation-created pull request. Initial PR creation
(TASK-T24, using `CLAUDE_PR_TOKEN`) already starts CI/Security/Risk
automatically. Rework did not: the interactive Claude step pushed the
follow-up commit directly to the PR branch using its own
GitHub-App/OIDC-authenticated identity, and GitHub records the resulting
`pull_request/synchronize` event's actor as `github-actions[bot]` —
gated behind manual approval, independent of the legacy
`arrickcherney-ops` collaborator identity (TASK-T25 confirmed that
account's write access is unrelated; this reproduced on fresh PR #94
commits after that access was already removed).

## Root cause

GitHub's contributor-approval policy evaluates the actor that triggered
a `pull_request` event, not just the PR author. A push authenticated
through the pinned `anthropics/claude-code-action`'s own default
GitHub-App/OIDC identity is not a repository collaborator, so the
resulting `pull_request/synchronize` run is treated as needing approval
even on a same-repository, owner-authorized PR. TASK-T24 already fixed
the equivalent problem for **PR creation** by having a separate trusted
job push/create through `CLAUDE_PR_TOKEN` (a fine-grained PAT owned by
`wayne19870129`) instead of the Action's own identity. Rework pushes
needed the same fix, but the trigger this time is a comment on an
*already-open* PR, and the pinned Action always pushes any commit it
makes directly onto that PR's own head branch — the FAQ confirms there is
no input to redirect it to a new branch instead when the target PR is
open. Verified directly against the pinned action's own
[`action.yml`](https://github.com/anthropics/claude-code-action/blob/0a8d3c9443bbff909ab973b6a17a340b913f229f/action.yml)
and [`docs/faq.md`](https://github.com/anthropics/claude-code-action/blob/0a8d3c9443bbff909ab973b6a17a340b913f229f/docs/faq.md)
at the pinned SHA: it accepts an optional `github_token` input ("uses
that token's identity instead" of its default GitHub App/OIDC identity),
but exposes no flag to disable its automatic post-turn commit/push or to
force branch creation instead of pushing to an open PR's existing head.

## Trust boundary and architecture

Because the Action cannot be told not to push, and `CLAUDE_PR_TOKEN` must
never reach it, the design decouples **editing/verifying** from the
**final head-changing write** across two jobs in `.github/workflows/claude.yml`,
routed only for comments on an already-open PR
(`github.event.issue.pull_request` truthy); Issue-first comments are
unchanged (`claude` / `create-pr`, now explicitly guarded with
`!github.event.issue.pull_request`):

1. **`claude-rework`** (interactive, no PAT, no push credential at all).
   Its job-level `permissions:` grants only `contents: read`,
   `pull-requests: read`, `issues: write` — no `contents: write`, and no
   `id-token: write` (so the Action's own default GitHub App/OIDC git
   identity is unavailable in this job at all). The Claude step is also
   given an explicit `github_token: ${{ secrets.GITHUB_TOKEN }}` override,
   documented by the Action as replacing its default identity — with the
   job's read-only `contents` permission, any push attempt this token
   makes is rejected by GitHub itself, not by trusting our own code path.
   Claude can still read, edit files, run the same allowlisted
   tests/lint/build commands, and post its final comment; it simply has
   no way to land anything on the PR branch. The step is
   `continue-on-error: true so a failed internal push attempt cannot
   fail the job before the next step runs.
   A `Record the PR head before Claude runs` step (plain read-only
   `pulls.get`, before Claude starts) captures the PR's head branch name
   and SHA as `base_sha` — the stale-SHA baseline for the trusted writer.
   After the Claude step, `Capture the local rework diff for the trusted
   writer` (`if: always()`) diffs the working tree against `base_sha`
   with `git diff <base_sha> --`, which captures Claude's changes whether
   or not the Action's own internal commit succeeded, and — only if the
   diff is non-empty — builds a manifest via `scripts/claude-rework-diff.cjs`
   (also carries the range's commit messages, if any, for traceability).
2. **`apply-pr-rework`** (trusted, the only job with `CLAUDE_PR_TOKEN`,
   `needs: claude-rework`, gated on `has_changes == 'true'`, runs on a
   separate job/runner). It never executes repository-controlled
   application or test code: `scripts/claude-apply-pr-rework.cjs`
   performs the actual write purely through GitHub's Git Data API
   (`getCommit` → `createBlob` → `createTree` → `createCommit` →
   `updateRef`), never a `git clone`/`npm ci`/`pytest`/etc. against the
   target branch — there is no code execution surface for the PAT to be
   exposed to at all, beyond Octokit itself.

**Transfer mechanism.** The manifest (changed file paths + base64
content, or a delete marker) travels between the two jobs as a small
base64 **job output** (`manifest_b64`), not a workflow artifact upload/
download action. This was a deliberate choice over the artifact
approach the Issue suggested as an example: it needed one fewer
third-party action to pin and trust for a trust-sensitive path, using
only `actions/checkout` and `actions/github-script`, both already pinned
and verified elsewhere in this same workflow file. `scripts/claude-rework-diff.cjs`
fails closed (throws, no manifest written) if the total changed content
would exceed 300&nbsp;KiB decoded — comfortably under GitHub's per-job-output
size limit — rather than silently truncating a large rework diff.

## Stale-SHA / concurrency protection

`scripts/claude-apply-pr-rework.cjs` performs every check *before* any
write call:

1. The branch name itself must match this automation's own naming policy
   (`^claude/issue-[0-9]+-[0-9A-Za-z-]+$` — the exact pattern
   `scripts/claude-create-pr.cjs` already uses to create Issue-first
   branches). A branch that doesn't match this is rejected without even
   calling the GitHub API — covers both a fork's differently-named head
   and a same-repository but human-created branch.
2. `pulls.get` must report the PR is still `open`.
3. The head repository must be this exact repository (rejects forks).
4. The head ref must still equal the branch the interactive job started
   from (rejects a PR whose head moved to an unexpected branch).
5. **The head SHA must still equal the SHA the interactive job recorded
   before Claude ran.** Any concurrent human or agent push between then
   and now changes this SHA, and the trusted writer fails closed
   ("Stale transfer...refusing to overwrite concurrent work") rather
   than building on top of it.
6. Even after all of the above pass, `git.updateRef` is called with
   `force: false` — a fast-forward-only update. If the ref moved in the
   narrow window between step 5's check and this call, GitHub itself
   rejects the update (422/409), which the script also treats as a fail-
   closed stale-transfer error rather than retrying or forcing.

## Failure behavior

Every one of the following fails closed (throws, propagates as a failed
job, zero API write calls made) rather than falling back to a workaround:
a fork PR head; a same-repository branch that does not match the
automation-owned naming policy (a human's own branch); a closed/merged
PR; a head ref mismatch; a stale head SHA; a concurrent ref update lost
race; a manifest with no `changes` array, an empty/invalid path (including
path traversal and absolute paths), an unknown action, or an `upsert`
missing its content; a missing/misconfigured `CLAUDE_PR_TOKEN` (same
`PR_TOKEN_CONFIGURED` guard pattern as the existing `create-pr` job — no
`GITHUB_TOKEN` fallback). None of these produce a dummy/no-op commit or
any actor-bridging workaround; they simply do not write.

## Security reasoning

- `CLAUDE_PR_TOKEN` is referenced only in `create-pr` (TASK-T24, unchanged)
  and `apply-pr-rework` (this task). It is never passed as `github_token`,
  an environment variable, or any other input to the interactive `claude`
  or `claude-rework` jobs — verified by a workflow-source regression test
  (see below) that greps job-boundary text for the actual
  `github-token:`/`PR_TOKEN_CONFIGURED:` credential wire-up, not merely
  the identifier's appearance in a design-rationale comment.
- `apply-pr-rework` runs no repository-controlled code: no checkout of
  business logic is executed, no `npm ci`/`pip install`/`pytest`/`make`
  step exists in that job. Its only checkout is `persist-credentials: false`
  and used solely so `actions/github-script` can `require('./scripts/
  claude-apply-pr-rework.cjs')` — that script itself only calls Octokit
  Git Data endpoints.
- `claude-rework` is given no git-write credential at all (see
  "Trust boundary and architecture" above) — a stronger guarantee than
  the Issue's literal minimum ("never give it `CLAUDE_PR_TOKEN`"), verified
  structurally rather than assumed.
- No fork write, no human-branch write, no force push, no PR merge, no
  Issue close, and no deploy path exists anywhere in this change.

## Deterministic test coverage

`scripts/tests/claude-rework-diff.test.cjs` (pure manifest-building logic):
parses added/modified/deleted `git diff --name-status` lines; ignores
blank lines; empty diff yields no entries; builds base64 content for
upserts and omits it for deletes; preserves a `null` commit message
rather than coercing it; rejects a path-traversal entry; fails closed
once total content exceeds the safety limit.

`scripts/tests/claude-apply-pr-rework.test.cjs` (the trust-boundary
logic, fully mocked like the existing `claude-create-pr.test.cjs`
pattern): a trusted automation-owned PR update succeeds (blob → tree →
commit → fast-forward-only `updateRef`); a synthesized commit message is
used when Claude made no local commit; a deletion entry gets a `null`
blob sha in the tree; **a stale starting SHA fails closed with zero
write calls**; **a concurrent head change during the write (422/409 from
`updateRef`) is never overwritten**; an unrelated ref-update error (403)
is not swallowed as a stale race; **a fork PR is rejected** before any
write; **a same-repo human/non-automation-owned branch is rejected by
naming policy alone**, before even calling the GitHub API; a closed PR
is rejected; a head-ref mismatch is rejected; **malformed or conflicting
transfer fails closed** for a missing `changes` array, a path-traversal
entry, an absolute-path entry, an unknown action, and an upsert missing
content; missing `prNumber`/`branch`/`baseSha` fails closed; an empty
change set is a no-op, not an error; the module never reads
`process.env` or embeds a literal token value; and a workflow-source
test confirms only `create-pr` and `apply-pr-rework` ever wire up
`CLAUDE_PR_TOKEN` as a credential, never `claude` or `claude-rework`.

Existing `scripts/tests/claude-create-pr.test.cjs` (Issue-first PR
creation, same-Issue branch→existing-PR-head merging, closure-keyword
safety) is unchanged and still passes — the Issue-first path itself was
not touched.

`.github/workflows/ci.yml`'s `lint` job now runs all three test files
together: `node --test scripts/tests/claude-create-pr.test.cjs
scripts/tests/claude-rework-diff.test.cjs scripts/tests/claude-apply-pr-rework.test.cjs`.

## What this task does not change

- Owner-only trigger (`github.event.comment.user.login == 'wayne19870129'`)
  and the `@claude` mention gate are unchanged for every job.
- Final merge remains exclusively a human action — no job in this
  workflow calls a merge, approve, or auto-merge API.
- No deploy path is introduced or touched.
- Issue-first automatic PR creation (`claude` / `create-pr`) and
  same-Issue later-branch→existing-PR-head merging in
  `scripts/claude-create-pr.cjs` are unchanged; they are now simply
  guarded to run only when the triggering comment is on an Issue, not a
  PR (`!github.event.issue.pull_request`), matching `create-pr`'s
  existing guard.
- `AGENTS.md` is not modified.

## Post-merge live-acceptance procedure

This task's own regression tests (deterministic, mocked) and normal PR
CI can validate the code paths in isolation, but — consistent with this
repository's established practice for every prior Claude-automation
change (TASK-T24, TASK-T25) — the actual GitHub approval-gate behavior
can only be confirmed live, after human merge:

1. Merge this PR.
2. On a **fresh** automation-owned open PR (i.e. one whose head branch
   matches `claude/issue-<n>-<token>`, such as a future PR from a new
   Issue-first run — **not** PR #94, which stays open/paused as this
   Issue's own evidence trail and must not be merged to manufacture a
   clean test), leave an owner `@claude` comment on the PR itself asking
   for a real, small change (e.g. requested by a ChatGPT Work review or
   directly by the owner).
3. Confirm: `claude-rework` runs and posts Claude's final comment, but
   makes **no** push (its job log should show no successful git push, or
   none at all since the Action never got a write-capable token);
   `apply-pr-rework` then runs and its log reports the new commit SHA and
   ref update.
4. Confirm the resulting PR head SHA's CI/Security/Risk checks start
   **automatically**, with no **Approve workflows to run** button, on
   that new SHA.
5. Confirm a stale-SHA rerun path: if a human pushes to the PR between
   steps 2 and 3 in some future run, `apply-pr-rework` must fail closed
   with a "Stale transfer" diagnostic rather than silently overwriting —
   this is exercised deterministically in tests here, but has not yet
   been observed live.
6. Record the outcome in `docs/83-project-continuity.md` and close
   Issue #95 only once step 4 is directly confirmed; until then, this
   task and Issue #95 remain implementation-complete-but-not-live-
   validated, consistent with how TASK-T24's PAT-based PR-creation fix
   was itself only confirmed after a live post-merge run.

## Rollback

Revert this PR through a reviewed PR. `claude`/`create-pr` (Issue-first)
are unaffected by a revert beyond the `!github.event.issue.pull_request`
guard reverting too, restoring the pre-existing (defective) behavior
where a PR-comment `@claude` request is handled by the original `claude`
job and pushes directly with the Action's own identity. No production/
customer runtime or database changes are involved either way.
