# TASK-T22 — Replace `GITHUB_TOKEN` with a GitHub App installation token for the trusted Claude PR/dispatch path (Issue #79)

风险等级：触碰 `.github/workflows/claude.yml` 的 PR 按既有先例
（T17/Issue #50/TASK-T19/TASK-T20/TASK-T21）一律落 `risk-classify.yml` 的
`risk:high`，以自动分类结果为准。本任务不涉及生产凭据泄露、不涉及数据库、
不涉及业务代码，属于 CI/Action 身份治理修复；但它确实改变了谁来创建/更新
受信任路径的 PR 与谁来触发 CI/Security/Risk，因此按 `CLAUDE.md` "Before
implementation" 规则记录为需要 TASK 文档的信任边界变更。

## 目标

Implement the phase-1 fix for Issue #79: mint a short-lived GitHub App
installation token inside the trusted (`wayne19870129`-only) Claude
automation path and use that token — instead of the repository
`GITHUB_TOKEN` — for exactly two things:

1. The deterministic `ensure-pr` step in the `claude` job
   (`scripts/claude-ensure-pr-and-dispatch.sh`'s `gh pr create` /
   `gh pr list` / `gh workflow run ci.yml|security.yml|risk-classify.yml`
   calls).
2. The `claude-recovery` job (`workflow_dispatch`), which runs the same
   script against an already-pushed branch.

Everything else in the file (Claude's own tool allowlist, the dedup job,
the actor gates, `pre_head_sha`/`issue_number` plumbing, the completion
marker, the run-summary step) is unchanged.

## 已确认的实现阻碍（本任务本身遇到，不是假设）

This exact task was attempted directly on Issue #79's own automation
branch on 2026-09-13, in the same session as this document. **It could
not be completed**, for a reason that is durable and applies to *any*
Claude Code session running under this repository's current
`claude-code-action` GitHub App installation, not just this run:

> Claude Code's own stated capabilities for this integration include,
> verbatim: *"Modify files in the .github/workflows directory (GitHub
> App permissions do not allow workflow modifications)"* under "What You
> CANNOT Do." This is enforced by the App installation's own permission
> grant (no `workflows: write`), not by this repository's `AGENTS.md`/
> `CLAUDE.md` policy and not by anything in this session's prompt — it
> cannot be worked around by asking more explicitly, by editing the file
> anyway (the write would be rejected at the platform level), or by
> routing the change through a different tool.

Concretely: implementing this task's goal requires adding a new step to
`.github/workflows/claude.yml` (see the exact diff below) and changing
two `env: GH_TOKEN: ${{ github.token }}` lines in that same file. A
Claude Code session triggered via `issue_comment`/`pull_request_review_comment`
cannot make that edit. This is the actionable finding this document
records, per this Issue's own instruction to "STOP and document that as
a blocker instead of silently expanding permissions or changing the
trust model" when an implementation obstacle is found — the obstacle
here is a tooling/permission boundary, not an architectural one, but the
same stop-and-document discipline applies.

**What this means for finishing Issue #79:** the diff below must be
applied by the repository owner directly (a manual commit/PR, or a
Claude session invoked through a mechanism that does hold
`.github/workflows` write scope, e.g. a local `claude` CLI run with the
owner's own credentials — not the `issue_comment`-triggered GitHub
Action). Everything else in this task (script review, permission list,
validation plan, doc updates) is otherwise complete and does not need to
be redone once the diff is applied.

## 约束

- Do not broaden Claude's own `--allowedTools`/give Claude generic
  `gh`/shell access. (Not touched by this design — the new step runs as
  a plain workflow step, never inside Claude's own turn loop, exactly
  like the existing `ensure-pr` step.)
- Do not weaken fork/external-contributor Actions-approval settings.
  (Not touched — that setting lives under Settings → Actions and is
  independent of which token a same-repo job uses internally.)
- Do not introduce auto-merge, auto-deploy, or auto-close of Issue #79.
- Do not modify `AGENTS.md`.
- Request only the minimum GitHub App permissions actually used (see
  below) — do not request `workflows: write` merely because this task's
  *implementation* touches `.github/workflows/claude.yml`; that is a
  one-time human/implementation-time edit, not a permission the deployed
  App token needs at runtime.
- Keep `scripts/claude-ensure-pr-and-dispatch.sh` token-source agnostic.
  **Verified already true, no change needed:** the script only ever
  reads `GH_TOKEN` from its process environment (`gh` picks it up
  automatically); it has no reference to `github.token` or any other
  token source, so swapping what `claude.yml` exports into `GH_TOKEN`
  requires zero script changes.
- Preserve one-task-one-PR, `pre_head_sha`/`issue_number` guards,
  no-new-commit short-circuit, recovery/idempotency, and human-only
  merge — none of these are affected by which identity's token the `gh`
  calls run under.

## 允许修改的文件

- `.github/workflows/claude.yml` — **blocked in this session**, see
  above; diff specified below for manual/owner application.
- `docs/82-tasks/TASK-T22-claude-automation-app-token-identity.md` (this
  file).
- `docs/83-project-continuity.md` (durable-state update).
- Explicitly *not* modified in this pass: `scripts/claude-ensure-pr-and-dispatch.sh`
  (no change needed, see above), `AGENTS.md` (forbidden by this Issue's
  own constraints).

## Root cause recap (from the Issue #79 diagnostic-only run, still valid)

`scripts/claude-ensure-pr-and-dispatch.sh`'s `gh pr create` / `gh
workflow run` calls run with `GH_TOKEN: ${{ github.token }}` — the
repository's own `GITHUB_TOKEN`, rendered as actor `github-actions[bot]`.
**Observed fact, not yet a proven causal mechanism:** on PR #77 and PR
#78, the PR was created/updated by this `GITHUB_TOKEN`-driven step, and
the `pull_request`-triggered runs of `ci.yml`/`security.yml`/
`risk-classify.yml` on those same SHAs went `action_required` while the
same-SHA `workflow_dispatch` runs of those same three files succeeded
without approval, leaving `mergeable_state=blocked` on the required
named check contexts (`lint`, `shellcheck`, `backend`, `backend-image`,
`frontend`, `gitleaks`, `python-audit`, `npm-audit`, `marzban-contract`).
Live evidence: PR #77 head `1df3441f92fc883fa358eb93c8659277126fbb84`,
PR #78 head `c7d325ae96c3fa57e5058d6a2d3d51d13e7bad43` (both recorded in
`docs/83-project-continuity.md`).

What has **not** been isolated from that evidence: whether the
`action_required` gating is triggered by the PR-create/dispatch call's
identity (`GITHUB_TOKEN` via this script), by the separate branch-push
identity (`claude-code-action`'s own push wrapper, deliberately left
unchanged by this task), or by both together — the two identities have
never been varied independently in a live run. Minting the
PR-create/dispatch token from a **GitHub App installation** instead,
while leaving branch-push identity unchanged, is therefore this task's
**phase-1 hypothesis to validate**, not a documented, already-confirmed
GitHub product guarantee: if the App-token swap alone does not clear
`action_required` on the `pull_request` runs, that is the signal
(anticipated in the "Notes for whoever applies this diff" section below)
that push identity is also part of the mechanism and phase 1's scope was
too narrow. This task does not change the *fork*-approval rule at all,
which is a separate setting keyed on the PR author being an outside
collaborator, untouched by this change either way.

## Minimum GitHub App permissions

The human setup for this Issue already created and installed a
repository-scoped App with secrets `CLAUDE_AUTOMATION_APP_ID` /
`CLAUDE_AUTOMATION_APP_PRIVATE_KEY`. The exact repository permissions
that installation needs, separated by purpose (per the Issue's own
question 3):

| Purpose | Permission | Used by |
|---|---|---|
| PR create/reuse | Pull requests: Read & write | `gh pr create`, `gh pr list` in `claude-ensure-pr-and-dispatch.sh` |
| CI/Security/Risk dispatch | Actions: Read & write | `gh workflow run ci.yml/security.yml/risk-classify.yml` |
| Reading branch state to decide commit-count/head-SHA | Contents: Read | `git fetch`/`git rev-list`/`git rev-parse` against `origin` inside the script (read-only — the branch push itself remains `claude-code-action`'s own push wrapper, using its existing token, per the "do NOT proactively change Claude's branch-push identity" scope decision in the Issue's own instructions) |

No `Contents: write`, no `Issues: write`, and no `Workflows: write` are
needed for this phase-1 token — the completion-marker/redirect comments
and the dedup job's issue-comment reads/writes stay on the existing job
`GITHUB_TOKEN` (`permissions: issues: write` already granted at the job
level in `claude.yml`), since those are not part of what this Issue
asks to move. If Contents: Read turns out to be unnecessary in practice
(e.g. if the App token is only ever passed as `GH_TOKEN` for `gh`
subcommands and the plain `git fetch`/`git rev-list`/`git rev-parse`
calls keep using the default checkout credentials), drop it before
granting — this table states the maximum plausibly needed, not a
confirmed floor; confirm against a live run per the validation plan
below before locking in the App's permission set.

**Known gap: the App installation actually completed for this Issue's
human setup step is broader than this table.** As installed, it grants
Actions: Read & write, Contents: Read & write, Issues: Read & write, and
Pull requests: Read & write — wider than the phase-1 floor above
(notably `Contents: write` and `Issues: write`, neither used by the two
call sites this task touches). `actions/create-github-app-token` mints a
token scoped to the *installation's* granted permissions by default; it
does not narrow them on its own. Two ways to close this gap before live
use, either is acceptable and both should be recorded once done:

1. **Preferred:** have the repository owner reduce the App installation
   itself (GitHub → Settings → Developer settings → GitHub Apps → this
   App → Permissions) to exactly the three permissions listed in the
   table above (Pull requests, Actions, Contents). Metadata: Read is a
   mandatory/implicit permission every GitHub App installation carries
   regardless of what is configured — it is not a row in the table and
   not an additional grant the owner needs to add. Then re-approve the
   reduced permission set for this repository's installation.
2. **If the installation must stay broader for other reasons:** add
   explicit per-permission inputs to the `create-github-app-token` step
   in the diff below (`permission-pull-requests: write`,
   `permission-actions: write`, `permission-contents: read`), which mint
   a token narrowed to exactly those scopes regardless of what the
   installation itself holds — `create-github-app-token` supports
   requesting a subset of the installation's granted permissions this
   way, one input per permission. (There is no generic aggregate
   `permissions:` input on this action; see the correction below.)

Until one of these is done and confirmed, do not describe this design as
least-privilege in practice — the design's *intent* is least-privilege,
but the currently-provisioned installation is not. The diff below
includes the explicit `permission-*` inputs as the default so the token
itself is narrow even before the installation-level tightening happens.

**Correction (rework round 2):** an earlier version of this document
used a single aggregate `permissions:` input taking a JSON object (e.g.
`permissions: {"pull_requests": "write", ...}`) on the
`create-github-app-token` step. That input does not exist on the
current released `actions/create-github-app-token` action — its
`action.yml` exposes only per-permission inputs named `permission-<name>`
(e.g. `permission-actions`, `permission-contents`,
`permission-pull-requests`), each taking a single value like `read` or
`write`. The YAML below has been corrected to use those per-permission
inputs; do not reintroduce a generic `permissions:` input when applying
this diff, since an unrecognized input would silently do nothing and
the minted token would fall back to the installation's full (broader)
grant.

## Exact diff for manual application to `.github/workflows/claude.yml`

Add a token-minting step immediately before `ensure-pr` in the `claude`
job, using the already-provisioned secrets, and repeat it in
`claude-recovery`. Use `actions/create-github-app-token`, pinned to a
reviewed commit SHA (not a mutable tag), matching this file's existing
pinning convention for `actions/checkout` and
`anthropics/claude-code-action`.

```yaml
      # TASK-T22 (Issue #79): mint a short-lived GitHub App installation
      # token scoped to this repository, used ONLY for the deterministic
      # ensure-pr step's `gh pr create`/`gh workflow run` calls below --
      # never for Claude's own tool loop, never for the completion-marker
      # comment (that stays on the job's own GITHUB_TOKEN). Using an App
      # token instead of GITHUB_TOKEN here is what stops GitHub from
      # putting the resulting pull_request-triggered CI/Security/Risk
      # runs into action_required -- see TASK-T22 for the full analysis.
      - id: app-token
        if: always()
        uses: actions/create-github-app-token@<PIN-TO-REVIEWED-SHA> # v2
        with:
          app-id: ${{ secrets.CLAUDE_AUTOMATION_APP_ID }}
          private-key: ${{ secrets.CLAUDE_AUTOMATION_APP_PRIVATE_KEY }}
          owner: ${{ github.repository_owner }}
          repositories: ${{ github.event.repository.name }}
          permission-pull-requests: write
          permission-actions: write
          permission-contents: read
```

The `permission-*` inputs above explicitly narrow the minted token to
the phase-1 floor from the table, regardless of what the App
installation itself currently grants (see "Known gap" note above) —
include them even after the installation is tightened, as defense in
depth.

Then in the existing `ensure-pr` step, change only the `GH_TOKEN` line:

```yaml
      - id: ensure-pr
        if: always()
        shell: bash
        env:
          GH_TOKEN: ${{ steps.app-token.outputs.token }}   # was: ${{ github.token }}
          REPO: ${{ github.repository }}
          # ... (all other env: keys unchanged)
```

And in `claude-recovery`, add the same minting step before its existing
`gh`-calling step, then change its `GH_TOKEN` the same way:

```yaml
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
      - id: app-token
        uses: actions/create-github-app-token@<PIN-TO-REVIEWED-SHA> # v2
        with:
          app-id: ${{ secrets.CLAUDE_AUTOMATION_APP_ID }}
          private-key: ${{ secrets.CLAUDE_AUTOMATION_APP_PRIVATE_KEY }}
          owner: ${{ github.repository_owner }}
          repositories: ${{ github.event.repository.name }}
          permission-pull-requests: write
          permission-actions: write
          permission-contents: read
      - shell: bash
        env:
          GH_TOKEN: ${{ steps.app-token.outputs.token }}   # was: ${{ github.token }}
          REPO: ${{ github.repository }}
          BRANCH: ${{ inputs.branch }}
          BASE_BRANCH: ${{ github.event.repository.default_branch }}
        run: bash scripts/claude-ensure-pr-and-dispatch.sh
```

Notes for whoever applies this diff:

- `<PIN-TO-REVIEWED-SHA>` must be resolved to an actual reviewed commit
  SHA of `actions/create-github-app-token` before merging, per this
  file's existing pinning discipline (see the comments already on the
  `actions/checkout` and `claude-code-action` `uses:` lines) — do not
  leave a mutable tag in the merged file.
- The job-level `permissions:` blocks for `claude` and `claude-recovery`
  are unaffected by this change: `create-github-app-token` needs no
  `permissions:` grant on the calling job (its own auth comes from the
  App ID/private-key secrets, not from the job's `GITHUB_TOKEN`).
  Whether `actions: write` and `pull-requests: write` can later be
  *removed* from those `permissions:` blocks once the App token fully
  replaces `GITHUB_TOKEN` for those specific calls is a follow-up
  tightening question, not required for phase 1, and out of scope here
  per this Issue's "narrowest phase-1 change first" instruction.
- Branch push identity (`claude-code-action`'s own push wrapper) is
  deliberately **not** changed in this design, per the Issue's own
  scope decision. If live validation (below) shows the `pull_request`
  runs still land in `action_required` even after this diff, that is
  the signal the Issue's own text anticipated: STOP and report it as a
  finding that phase 1's hypothesis (PR-create/dispatch identity alone
  is sufficient) was wrong, rather than silently changing the push
  identity too.

## Live validation plan (once the diff above is applied and merged)

1. Trigger a real trusted `@claude` run (a new Issue-first comment, or a
   follow-up comment on this Issue after the diff lands).
2. Record, the same way `docs/83-project-continuity.md` already records
   PR #77/#78 evidence: the new PR's head SHA, and the status of each
   `pull_request`-triggered `ci.yml`/`security.yml`/`risk-classify.yml`
   run on that SHA (expect anything other than `action_required`).
3. Confirm `mergeable_state` for that PR clears to something other than
   `blocked`-for-approval once the required named check contexts report.
4. In parallel, confirm an external/fork-originated PR's `pull_request`
   runs still show `action_required` under the unchanged fork-approval
   policy — proving no global relaxation occurred.
5. Only after both are observed, update `docs/83-project-continuity.md`
   with the run IDs/SHAs and close out the remaining Issue #79 work.

## 验收标准

- [ ] `.github/workflows/claude.yml` diff above applied by the repository
      owner (or a Claude session with workflow-write scope) — **not
      completed by this task's own Claude Code session; documented as
      the blocker instead.**
- [x] `scripts/claude-ensure-pr-and-dispatch.sh` confirmed token-source
      agnostic; no change required.
- [x] Minimum App permission set identified and stated (table above).
- [x] `docs/83-project-continuity.md` updated to record this blocker as
      a durable fact for future sessions (done in this same PR).
- [ ] Live validation plan executed and recorded (blocked on the diff
      being applied first).
- [ ] Issue #79 closed — **not yet**; remains open pending the manual
      diff application and live validation above.
