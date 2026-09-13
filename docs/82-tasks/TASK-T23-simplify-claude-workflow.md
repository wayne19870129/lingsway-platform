# TASK-T23 — Simplify `.github/workflows/claude.yml` back to a stable `@claude` → PR → CI → human-merge loop (Issue #87)

风险等级：触碰 `.github/workflows/claude.yml` 的 PR 按既有先例
（T17/Issue #50/TASK-T19/TASK-T20/TASK-T21/TASK-T22）一律落
`risk-classify.yml` 的 `risk:high`，以自动分类结果为准。本任务不涉及
生产凭据泄露、不涉及数据库、不涉及业务代码，但它改变了这条受信任自动化
路径本身的触发/去重/并发行为，因此按 `CLAUDE.md` "Before implementation"
规则记录为需要 TASK 文档的信任边界变更。

## 已确认的实现阻碍（本任务本身遇到，不是假设）

Same durable blocker already recorded in
`docs/82-tasks/TASK-T22-claude-automation-app-token-identity.md`: this
session's own stated capabilities include, verbatim, *"Modify files in
the .github/workflows directory (GitHub App permissions do not allow
workflow modifications)"* under "What You CANNOT Do." This is enforced
by the App installation's permission grant, not by `AGENTS.md`/
`CLAUDE.md` policy, and cannot be worked around from an
`issue_comment`-triggered run. **This task's own diff to
`.github/workflows/claude.yml` (below) must be applied by
`wayne19870129` directly** (a manual commit/PR, or a Claude session
invoked with the owner's own local credentials — not this GitHub
Action). Everything else in this task (analysis, the exact diff, the
risk tradeoffs, the validation plan) is complete and does not need to be
redone once the diff is applied.

## 目标

Simplify `.github/workflows/claude.yml` to match the loop described in
Issue #87:

> Issue comment `@claude` → Claude Code starts → Claude modifies code →
> Claude creates PR → GitHub Actions validates → Human reviews → Human
> merges

while keeping the safety constraints Issue #87 itself restates (no
self-merge, no self-close, no self-deploy, human final merge authority)
and the trigger constraints (issue_comment kept, owner-only, requires
`@claude` in the comment body).

## Per-requirement audit of the current file

| Issue #87 ask | Current state in `claude.yml` | Action |
|---|---|---|
| Keep `issue_comment` trigger | Present (`on.issue_comment.types: [created]`), plus `pull_request_review_comment` for inline PR review comments and `workflow_dispatch` for manual recovery | Keep all three, but correcting the rationale for `pull_request_review_comment`: a top-level PR "Conversation" comment is, in the GitHub API, an Issue comment on the PR's own Issue object, so `issue_comment` alone already covers the ordinary "human reviews, then asks for a fix in a normal PR comment" follow-up loop the Issue describes. `pull_request_review_comment` fires only for **inline** comments left on a specific diff line as part of a review thread — a narrower, additional path, not the one this workflow relies on for the general follow-up case. It is kept because some follow-up instructions genuinely do arrive as inline review comments and dropping the event type would silently lose that path, not because it is required for follow-up comments in general. `workflow_dispatch` is the recovery path from TASK-T20 (retry the deterministic PR/dispatch step without re-running Claude); keeping it costs nothing and is owner-gated the same way. |
| Only `wayne19870129` may trigger | Enforced in both the `dedup` and `claude` jobs' `if:` (`github.actor == 'wayne19870129'`) | Keep, collapsed into a single job's `if:` (see diff) |
| Trigger only on `@claude` in the comment | Enforced today only inside the `dedup` job's `if:` (`contains(github.event.comment.body, '@claude')`); the `claude` job itself only checks `needs.dedup.outputs.proceed`, so the check is transitive through the job graph rather than direct | Move the check directly onto the `claude` job's own `if:` — same effect, one less job/output edge to read to see it holds |
| Remove GitHub App dependency | **Not present.** `claude.yml` as it stands today uses `secrets.CLAUDE_CODE_OAUTH_TOKEN` for Claude itself and the plain job `GITHUB_TOKEN` (`${{ github.token }}`) for the deterministic `gh` calls — no `actions/create-github-app-token` step exists in this file. (TASK-T22 proposed adding one to fix an `action_required` CI-gating issue on Issue #79, but that diff was never applied — see that task's own "blocked in this session" note.) | Nothing to remove here; note it explicitly so this isn't re-litigated as if it were still pending |
| Remove custom bot-loop handling | The `dedup` job's `if:` already includes `github.event.comment.user.type != 'Bot'` (added for Issue #67/#50) — the *primary* bot-loop guard is already this one native-field check, not the marker system | Keep the `user.type != 'Bot'` check (move it onto `claude`'s own `if:`); remove the completion-marker post/read pair described below, which is a second, redundant loop-prevention layer on top of it |
| Remove unnecessary concurrency cancellation | `concurrency: { group: claude-<number>-<actor>, cancel-in-progress: true }` (TASK-T19, added after Issue #67 to stop a bot-authored comment from cancelling the real owner-triggered run) | **Revised after independent review (see below): keep the actor-scoped `concurrency:` block as-is.** An earlier version of this task proposed removing it entirely on the theory that `ensure-pr`'s idempotency alone was sufficient protection; that theory was checked against `scripts/claude-ensure-pr-and-dispatch.sh` and found incorrect for the Issue-first case (see "Why the actor-scoped concurrency block must be kept" below). What Issue #87 is actually reacting to — the pre-TASK-T19 bug where a bot's own status comment could cancel the real owner run — is already fixed by scoping the group key to `github.actor`; that fix is not "unnecessary concurrency cancellation," it is the mechanism keeping this workflow's one-task-one-PR guarantee correct. Nothing here is removed. |
| Remove excessive deduplication logic | The `dedup` job: (a) a GraphQL preflight that refuses to start if the Issue already has an open linked PR, (b) an instruction-hash + posted-comment-marker system that skips a byte-identical repeat comment, (c) a redirect-comment step, (d) a completion-marker post step in the `claude` job | **Remove the entire `dedup` job**, the completion-marker post step, and the redirect-comment step. Keep only the minimal `TARGET_NUMBER` / `PRE_HEAD_SHA` / `ISSUE_NUMBER` resolution `dedup` used to compute (folded into a small step inside `claude` itself), because `scripts/claude-ensure-pr-and-dispatch.sh` genuinely needs those three values to stay idempotent and avoid a second PR for the same Issue — that part is the deterministic script's own duplicate-PR guard (already fires on every run regardless of the `dedup` job), not "excessive" dedup. See tradeoffs below for what this removal gives up. |
| Remove automatic PR approval behavior | **Not present.** `claude-automerge.yml` was already deleted and the `AGENTS.md`/`CLAUDE.md` auto-merge exception already reverted, per `docs/82-tasks/TASK-T18-conditional-auto-merge.md`'s own "撤销记录" | Nothing to remove here either; note it explicitly for the same reason as the GitHub-App row above |
| No self-merge / self-close / self-deploy; human final merge authority | Never granted anywhere in this file — `ensure-pr` only ever calls `gh pr create`/`gh workflow run`, never `gh pr merge`/`gh pr close`; no `deploy-*.yml` is invoked from here | Unaffected by this task; carries over unchanged |

## Why the actor-scoped `concurrency:` block must be kept

An earlier draft of this task proposed removing the `concurrency:`
block entirely, on the theory that `scripts/claude-ensure-pr-and-
dispatch.sh`'s own idempotency was sufficient to prevent a duplicate PR
even with fully concurrent owner-triggered runs, so the worst case was
"two commits, no corrupted or duplicated PR." Independent review (PR
#88, round 1) checked that theory directly against the script and found
it wrong for the Issue-first trigger case, for a reason specific to how
that script protects against duplicate PRs across *different* branches:

- Issue-first `@claude` triggers create a **new, uniquely timestamped
  branch on every run** (`claude/issue-<N>-<timestamp>-...`) — there is
  no shared branch for `ensure-pr`'s own-branch idempotency check
  (`gh pr list --head "${BRANCH}"`) to find, because each concurrent run
  has a *different* `BRANCH`.
- The only thing standing between that and a duplicate PR for the same
  Issue is the script's separate `ISSUE_NUMBER` guard: `gh pr list
  --state open ... | jq 'select(headRefName startswith "claude/issue-
  <N>-")'`, checked **before** `gh pr create` is called later in the
  same script run.
- That check-then-act sequence is **not atomic**. Two owner-triggered
  runs racing on the same Issue can each reach the `gh pr list` read
  before either has executed `gh pr create`, each observe "no PR open
  yet for this Issue," and each proceed to create its own PR — a classic
  TOCTOU race, not the "safe worst case" the earlier draft claimed.

The actor-scoped `concurrency:` block (TASK-T19) is what actually
prevents this race today, and removing it would reopen it: two owner
`@claude` comments on the same Issue produce the *same* concurrency
group (`claude-<number>-wayne19870129`), so `cancel-in-progress: true`
guarantees only one of them is ever running `ensure-pr` at a time —
there is never a genuinely concurrent second run to race against the
first. This also preserves the property TASK-T19 explicitly designed
for: a newer owner instruction on the same Issue/PR supersedes an
older, still-running one, rather than both completing and potentially
producing two PRs for one task.

Keeping this block does not conflict with Issue #87's ask. The Issue's
"remove unnecessary concurrency cancellation" is best read (and TASK-T19
itself frames it this way) as reacting to the pre-T19 bug — a bot's own
status comment cancelling the real owner run because the group key
didn't include the actor — which is already fixed by the actor-scoped
key, not by removing concurrency altogether. What this task actually
simplifies is everything in the `dedup` job that is redundant with
either this concurrency guarantee or `ensure-pr`'s own idempotency (see
the row above and the "Remove excessive deduplication logic" row).

## Tradeoffs this simplification accepts (state honestly, do not hide)

1. **Turn/cost regression for the "Issue already has an open PR"
   case.** Today, the `dedup` job's GraphQL preflight refuses *before*
   Claude ever starts a turn if the Issue already has an open linked
   PR (TASK-T20, Issue #71 review round 2). After this simplification,
   that same case is still caught — but only *after* a full Claude
   turn loop runs and pushes a branch, when
   `claude-ensure-pr-and-dispatch.sh`'s own `ISSUE_NUMBER` guard
   refuses to open the second PR. This spends real model turns on an
   instruction that was always going to be refused. This is the exact
   cost TASK-T20 introduced the preflight to avoid. Accepting this
   tradeoff is a judgment call for the repository owner, not something
   this task decides unilaterally — flagged here rather than silently
   dropped.
2. **No more skip-on-exact-duplicate-comment.** A byte-identical repeat
   `@claude` comment (e.g. a webhook redelivery) will now start a full
   new run rather than being skipped by the instruction-hash marker.
   In practice this is rare (GitHub webhook redelivery is not routine),
   and the result is not incorrect — `ensure-pr`'s idempotency still
   prevents a duplicate PR — but it is not free (another full Claude
   turn loop). Same category of tradeoff as (1).
3. **No more owner-visible "why nothing happened" comment** for the
   open-PR case — previously the `dedup` job posted a one-time redirect
   comment explaining that follow-ups should go to the existing PR.
   After this simplification, the owner instead sees whatever
   `claude-ensure-pr-and-dispatch.sh` prints to the job log (not a PR
   comment) if it refuses via the `ISSUE_NUMBER` guard.

None of these tradeoffs weaken the required safety constraints (no
self-merge/close/deploy, owner-only trigger, no duplicate PR) — they
trade some turn-cost efficiency and one piece of in-PR observability
for a meaningfully smaller, easier-to-audit workflow file, which is the
explicit goal Issue #87 asks for. If the owner decides after reading
this that (1)/(2) are not acceptable, the correct fix is to keep the
GraphQL open-PR preflight as a small early step inside the `claude` job
itself (still far smaller than today's full `dedup` job with its
instruction-hash marker system) rather than reverting this whole task.

## 约束

- Do not reintroduce auto-merge, auto-close, or auto-deploy in any form.
- Do not widen Claude's own `--allowedTools` or grant generic `gh`/shell
  access to Claude's own turn loop — PR creation and CI/Security/Risk
  dispatch stay in the deterministic `ensure-pr` shell step, unchanged.
- Do not remove the `snapshot-scripts` step or its trust-boundary
  reasoning (copying the deterministic scripts out of the workspace
  before `claude-code-action` can switch the checkout to a PR's own,
  untrusted branch) — that protection is unrelated to the dedup/
  concurrency complexity Issue #87 asks to remove, and removing it would
  reopen the exact script-injection gap TASK-T20 closed.
- Do not change `scripts/claude-ensure-pr-and-dispatch.sh` or
  `scripts/claude-run-summary.sh` — both already work from plain
  environment variables and need no change for this simplification.
- Do not touch `AGENTS.md`.
- `.github/workflows/claude.yml` itself cannot be edited by this
  session (see blocker above) — the diff below is for manual/owner
  application only.

## 允许修改的文件

- `docs/82-tasks/TASK-T23-simplify-claude-workflow.md` (this file).
- `docs/83-project-continuity.md` (durable-state update, same PR).
- `.github/workflows/claude.yml` — **blocked in this session**; exact
  new file content below for manual/owner application.

## Exact new `.github/workflows/claude.yml` for manual application

Replace the file's `on:`/`jobs:` sections with the following (the
pinned `actions/checkout` and `claude-code-action` SHAs, the system
prompt string, and the `claude-recovery` job are carried over unchanged
from the current file — the `dedup` job and the completion-marker/
redirect steps are removed, `TARGET_NUMBER`/`PRE_HEAD_SHA`/
`ISSUE_NUMBER` resolution moves into a small `context` step inside
`claude`, and **the actor-scoped `concurrency:` block is kept
unchanged from the current file** — see "Why the actor-scoped
`concurrency:` block must be kept" above; it is not part of what this
diff removes):

```yaml
name: Claude Code

on:
  issue_comment:
    types: [created]
  pull_request_review_comment:
    types: [created]
  workflow_dispatch:
    inputs:
      branch:
        description: >-
          Branch that already has Claude's pushed commit(s) but is missing
          its PR and/or CI/Security/Risk dispatch (e.g. because the
          deterministic dispatch step failed after a previous run).
        required: true
        type: string

# Issue #67 / TASK-T19: kept unchanged by this simplification. The group
# key includes github.actor, not just the issue/PR number, so a bot's own
# status comment (a new issue_comment event) can never share a group with
# the real owner-triggered run and therefore can never cancel it -- while
# a newer owner instruction on the same Issue/PR still supersedes an
# older, still-running one. This also serializes concurrent owner-
# triggered runs on the same Issue, which is what keeps
# claude-ensure-pr-and-dispatch.sh's ISSUE_NUMBER guard (a non-atomic
# check-then-act) race-free for Issue-first triggers -- removing this
# block was considered and rejected after independent review found that
# race (see above). Copy this block verbatim from the current file.
concurrency:
  group: claude-${{ github.event.issue.number || github.event.pull_request.number }}-${{ github.actor }}
  cancel-in-progress: true

jobs:
  claude:
    if: |
      github.event_name != 'workflow_dispatch' &&
      github.actor == 'wayne19870129' &&
      contains(github.event.comment.body, '@claude') &&
      github.event.comment.user.type != 'Bot'
    runs-on: ubuntu-latest
    timeout-minutes: 20
    permissions:
      contents: write
      pull-requests: write
      issues: write
      id-token: write
      actions: write
    outputs:
      pr_number: ${{ steps.ensure-pr.outputs.pr_number }}
    env:
      CLAUDE_MODEL_SELECTOR: sonnet
      CLAUDE_EFFORT: medium
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          ref: ${{ github.event.repository.default_branch }}
          fetch-depth: 1

      - id: snapshot-scripts
        shell: bash
        run: |
          set -euo pipefail
          mkdir -p "${RUNNER_TEMP}/trusted-scripts"
          cp scripts/claude-ensure-pr-and-dispatch.sh scripts/claude-run-summary.sh "${RUNNER_TEMP}/trusted-scripts/"
          chmod +x "${RUNNER_TEMP}/trusted-scripts/"*.sh
          echo "dir=${RUNNER_TEMP}/trusted-scripts" >> "$GITHUB_OUTPUT"

      # Replaces the removed `dedup` job's TARGET_NUMBER/PRE_HEAD_SHA/
      # ISSUE_NUMBER computation (TASK-T20/T23) -- ensure-pr still needs
      # these three values to stay idempotent and avoid a duplicate PR,
      # even though the GraphQL open-PR preflight and instruction-hash
      # marker that used to live alongside them in `dedup` are gone.
      - id: context
        shell: bash
        env:
          GH_TOKEN: ${{ github.token }}
          REPO: ${{ github.repository }}
        run: |
          set -euo pipefail
          is_pr_comment="${{ github.event.issue.pull_request != null || github.event.pull_request != null }}"
          target_number="${{ github.event.issue.number || github.event.pull_request.number }}"
          echo "target_number=${target_number}" >> "$GITHUB_OUTPUT"
          if [ "$is_pr_comment" = "true" ]; then
            pre_head_sha=$(gh pr view "$target_number" --repo "$REPO" --json headRefOid --jq .headRefOid)
            echo "pre_head_sha=${pre_head_sha}" >> "$GITHUB_OUTPUT"
            echo "issue_number=" >> "$GITHUB_OUTPUT"
          else
            echo "pre_head_sha=" >> "$GITHUB_OUTPUT"
            echo "issue_number=${target_number}" >> "$GITHUB_OUTPUT"
          fi

      - id: claude
        uses: anthropics/claude-code-action@0a8d3c9443bbff909ab973b6a17a340b913f229f # v1
        with:
          claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
          claude_args: |
            --max-turns 30
            --model ${{ env.CLAUDE_MODEL_SELECTOR }}
            --effort ${{ env.CLAUDE_EFFORT }}
            --append-system-prompt "<unchanged -- copy verbatim from the current file's claude_args step>"

      - id: ensure-pr
        if: always()
        shell: bash
        env:
          GH_TOKEN: ${{ github.token }}
          REPO: ${{ github.repository }}
          CLAUDE_BRANCH_OUTPUT: ${{ steps.claude.outputs.branch_name }}
          TARGET_NUMBER: ${{ steps.context.outputs.target_number }}
          BASE_BRANCH: ${{ github.event.repository.default_branch }}
          PRE_HEAD_SHA: ${{ steps.context.outputs.pre_head_sha }}
          ISSUE_NUMBER: ${{ steps.context.outputs.issue_number }}
          SCRIPTS_DIR: ${{ steps.snapshot-scripts.outputs.dir }}
        run: |
          set -euo pipefail
          BRANCH="${CLAUDE_BRANCH_OUTPUT}"
          if [ -z "${BRANCH}" ] && [ -n "${TARGET_NUMBER}" ]; then
            BRANCH=$(gh pr view "${TARGET_NUMBER}" --repo "${REPO}" --json headRefName --jq .headRefName 2>/dev/null || true)
          fi
          if [ -z "${BRANCH}" ]; then
            echo "Could not determine a branch (no new branch reported, and #${TARGET_NUMBER:-<none>} is not an open PR) -- nothing to do."
            exit 0
          fi
          export BRANCH
          bash "${SCRIPTS_DIR}/claude-ensure-pr-and-dispatch.sh"

      - if: always()
        shell: bash
        env:
          EXECUTION_FILE: ${{ steps.claude.outputs.execution_file }}
          SCRIPTS_DIR: ${{ steps.snapshot-scripts.outputs.dir }}
          CONFIGURED_MODEL_SELECTOR: ${{ env.CLAUDE_MODEL_SELECTOR }}
          CONFIGURED_EFFORT: ${{ env.CLAUDE_EFFORT }}
        run: bash "${SCRIPTS_DIR}/claude-run-summary.sh"

  claude-recovery:
    if: github.event_name == 'workflow_dispatch' && github.actor == 'wayne19870129'
    runs-on: ubuntu-latest
    timeout-minutes: 5
    permissions:
      contents: read
      pull-requests: write
      actions: write
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          ref: ${{ github.event.repository.default_branch }}
          fetch-depth: 1
      - shell: bash
        env:
          GH_TOKEN: ${{ github.token }}
          REPO: ${{ github.repository }}
          BRANCH: ${{ inputs.branch }}
          BASE_BRANCH: ${{ github.event.repository.default_branch }}
        run: bash scripts/claude-ensure-pr-and-dispatch.sh
```

Notes for whoever applies this diff:

- Copy the `--append-system-prompt` string verbatim from the current
  file — it is unchanged by this task and reproducing it here in full
  would just risk a transcription error.
- All explanatory `#` comments the current file carries for the
  `checkout`/`snapshot-scripts`/`claude`/`ensure-pr`/run-summary steps
  (SHA-pinning rationale, trust-boundary rationale, `--model`/`--effort`
  rationale) should be kept — they were not part of what Issue #87 asked
  to simplify and remain accurate; they're omitted above only to keep
  this diff block focused on the structural change.
- **Do not delete** `docs/82-tasks/TASK-T19-claude-workflow-concurrency-actor-scoping.md`'s
  fix (the `concurrency:` block) from the file — an earlier version of
  this task proposed that and it was reverted after independent review
  (PR #88, round 1) found it reopens a real TOCTOU race in
  `scripts/claude-ensure-pr-and-dispatch.sh`'s Issue-first `ISSUE_NUMBER`
  guard (see "Why the actor-scoped `concurrency:` block must be kept"
  above). TASK-T19 remains fully in effect; this task does not touch it.

## Live validation plan (once the diff above is applied)

1. Post a genuine `@claude` comment on a test Issue; confirm the `claude`
   job starts, pushes a branch, and `ensure-pr` opens a PR.
2. Confirm CI/Security/Risk workflows dispatch against that PR's head
   SHA and go green (or report their real classification) the same as
   before this change.
3. Post a second `@claude` follow-up comment on the same still-open
   Issue (simulating the case TASK-T20's preflight used to catch early);
   confirm `ensure-pr`'s `ISSUE_NUMBER` guard still refuses to open a
   second PR (accepting the turn-cost tradeoff documented above).
4. Have a non-owner account (or simulate via a bot-authored comment if
   feasible) comment `@claude` on the same Issue/PR; confirm no run
   starts.
5. Confirm no duplicate PR and no duplicate CI dispatch occurs across
   steps 1–4.
6. Post two `@claude` Issue-first comments on the same still-open Issue
   in quick succession (as close to simultaneous as the UI allows);
   confirm the actor-scoped `concurrency:` block cancels the older run
   (visible in the Actions run list as `cancelled`) rather than both
   runs completing — the concurrent-race scenario independent review
   (PR #88, round 1) identified for `ensure-pr`'s `ISSUE_NUMBER` guard.
7. Update `docs/83-project-continuity.md` with the outcome once
   confirmed.

## 验收标准

- [ ] `.github/workflows/claude.yml` simplified per the diff above,
      applied by the repository owner (or a Claude session with
      workflow-write scope) — **not completed by this task's own
      session; documented as the blocker instead.**
- [x] Per-requirement audit of Issue #87's asks against the current file
      completed (table above).
- [x] Tradeoffs of removing the `dedup` job's logic stated explicitly,
      not silently dropped; the actor-scoped `concurrency:` block is
      kept (not removed) after independent review found removing it
      reopens a TOCTOU race in `ensure-pr`'s `ISSUE_NUMBER` guard.
- [x] `docs/83-project-continuity.md` updated to record this as a
      pending, owner-actionable change (same PR).
- [ ] Live validation plan executed and recorded (blocked on the diff
      being applied first).
- [ ] Issue #87 closed — **not yet**; remains open pending manual diff
      application and live validation above.
