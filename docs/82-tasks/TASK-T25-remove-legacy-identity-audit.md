# TASK-T25 — Remove legacy `arrickcherney-ops` identity from active workflow (audit)

> Note: this task was originally numbered TASK-T24. It was renumbered to
> TASK-T25 during PR #93 review because PR #91 independently introduced
> an unrelated `TASK-T24-claude-pr-and-verification.md` on its own
> branch; keeping both as T24 would have made the number ambiguous once
> both PRs merged.

## 目标

Issue #92: the repository owner (`wayne19870129`) has decided the legacy
`arrickcherney-ops` GitHub account/identity is no longer needed for
ongoing development on `wayne19870129/lingsway-platform`. Independently
audit the current working tree for any *active-workflow* dependency on
or reference to that identity (workflows, CI/config, docs, helper
scripts, `CODEOWNERS`, PR/issue templates, Dependabot config), remove
anything obsolete found, and record the result so a future session does
not need to re-derive it from scratch.

## 约束

- Do not rewrite PR #91's authorship or any other Git history merely to
  erase the old username — PR #91 is historical/in-flight work and stays
  as-is (`CLAUDE.md` "External AI Review" / general history-preservation
  rule; this Issue's own instructions repeat it explicitly).
- Do not modify `AGENTS.md` (requires separate explicit human approval
  per `AGENTS.md`'s own 模块写权限 table and this Issue's instructions).
- Do not modify anything under `.github/workflows/` — out of scope for
  this audit (nothing there referenced the legacy identity — see
  Findings) and outside this session's GitHub App write permissions
  regardless.
- Do not weaken external/fork contributor approval protections while
  doing this cleanup.
- Do not perform any account-, repository-, or collaborator-level
  action (no deletions, no settings changes) — those are GitHub
  Settings-level actions for the human owner, not something expressible
  as a code change, and destructive/account operations are outside an
  agent's authority regardless (`AGENTS.md` 禁止自主执行).
- No merge, no deploy, no closing Issue #92 before human merge.

## 允许修改的文件

- `docs/82-tasks/TASK-T25-remove-legacy-identity-audit.md` (this file)
- `docs/83-project-continuity.md`

No other file needed a change — see Findings below.

## 方法

Case-insensitive full-tree search for `arrickcherney` (and the fuller
`arrickcherney-ops`) across every tracked file, plus a manual read of
the files most likely to carry an identity/approval dependency:
`.github/workflows/*.yml`, `.github/CODEOWNERS`,
`.github/pull_request_template.md`, `.github/dependabot.yml`,
`.github/ISSUE_TEMPLATE/*`, `AGENTS.md`, `CLAUDE.md`,
`docs/83-project-continuity.md`, `docs/10-deploy-new-server.md` (the
"GitHub settings that require manual configuration" section), and the
current `docs/82-tasks/TASK-T2*` series covering the Claude automation
workflow's identity/token design.

## Findings

- **Zero matches.** A case-insensitive search for `arrickcherney` across
  the entire working tree returns no results. No workflow YAML, script,
  doc, or config file in this repository references that account.
- `.github/workflows/claude.yml`'s job-level gate already reads
  `github.event.comment.user.login == 'wayne19870129'` — the only
  account that can trigger the Issue-first Claude automation is the
  canonical owner account, not the legacy one.
- `.github/CODEOWNERS` already lists only `@wayne19870129` for every
  path.
- `docs/10-deploy-new-server.md`'s "Protect `main`" instructions already
  target `wayne19870129/lingsway-platform` by name.
- `docs/83-project-continuity.md` (the living handoff index) already
  describes the collaboration model exclusively in terms of `User`
  (`wayne19870129`), `Claude Code`, and `ChatGPT Work` — no legacy
  identity is named anywhere in it.
- `TASK-T22-claude-automation-app-token-identity.md` (a superseded
  proposal for a custom GitHub App token) and `TASK-T23-simplify-claude-
  workflow.md` (the current design) were both read in full for identity
  references; neither names `arrickcherney-ops`. The workflow's only
  Claude-side credential is `CLAUDE_CODE_OAUTH_TOKEN`, scoped to
  `wayne19870129`'s own Claude Code Action installation.
- The remaining link to the legacy identity is **PR #91 itself**, which
  GitHub recorded as authored by `arrickcherney-ops` at creation time
  (historical fact, not reproducible from any file in this repo) and
  whose `pull_request`-triggered check runs required maintainer
  approval before running. Two things are observed facts and one
  remains unverified from inside this session:
  - Observed: PR #91 is legacy-account-authored.
  - Observed: its `pull_request`-triggered checks required maintainer
    approval before running.
  - Not independently verifiable from this session: the exact
    repository/Actions setting that caused that approval requirement.
    This session has no working `gh`/GitHub API network access to
    inspect Actions or collaborator configuration directly (attempted
    and blocked by the sandbox), so that causal link is not asserted
    here beyond what's stated in `docs/10-deploy-new-server.md`'s
    "Protect main" and `docs/83-project-continuity.md` §5's
    `action_required` discussion.
  - **Collaborator permission on `arrickcherney-ops`:** during PR #93
    review, `wayne19870129` (the repository owner, reviewing
    independently outside this session) reported directly querying
    repository collaborator permissions and confirmed `arrickcherney-ops`
    **currently holds `write` collaborator access** on
    `wayne19870129/lingsway-platform`. This session cannot independently
    re-verify that query (no working GitHub API/`gh` network access from
    inside the sandbox), so it is recorded here as owner-reported current
    state rather than something this audit confirmed itself — but per
    the reviewer's explicit instruction it is treated as known, current
    fact rather than left as unresolved speculation. Revoking that write
    access is a GitHub Settings action only `wayne19870129` can take
    directly (collaborator-removal is outside this session's authority
    regardless of this being confirmed); this audit does not perform
    it and Issue #92 stays open until it's done.

## 验收标准

- `git grep -i arrickcherney` (or equivalent) against the working tree
  returns no matches outside this TASK file's own description of the
  audit.
- `docs/83-project-continuity.md` records this audit's outcome so a
  future session doesn't need to repeat it.
- No `.github/workflows/**`, `AGENTS.md`, or PR #91 change is included.
- This PR's body does not use a GitHub closing keyword for Issue #92,
  because the remaining collaborator/Actions-approval-setting question
  identified above is outside this repository's version-controlled
  files and therefore outside what a code PR can close out — see the
  PR description for the explicit statement of what remains.

## Post-#91 live-acceptance checkpoint (this run)

PR #91 and PR #93 (this task's own audit PR) are both now merged. This
checkpoint records a second, deliberately small, docs-only run
requested directly by `wayne19870129` via an `@claude` comment on Issue
#92, whose explicit purpose is to exercise the now-merged Issue → Claude
→ branch → automatic-PR path end to end and confirm it does not depend
on the legacy `arrickcherney-ops` identity.

What this run itself can directly observe from inside the Claude Code
Action job:
- It is executing because `.github/workflows/claude.yml`'s job-level
  gate matched `github.event.comment.user.login == 'wayne19870129'` —
  i.e. the trigger path that started this run is already owner-only, not
  dependent on any other account.
- The branch it is committing to is `claude/issue-92-20260914-0518`,
  created fresh for this Issue by the standard Claude Code Action step
  (not by, or on behalf of, `arrickcherney-ops`).
- This edit and the resulting commit(s) are the full extent of what this
  job does; per `claude.yml`'s `create-pr` job (a separate job keyed on
  `needs.claude.outputs.branch != ''`), PR creation itself happens in a
  **later, separate job** using `CLAUDE_PR_TOKEN` — a token this
  interactive Claude step never holds (see `docs/83-project-continuity.md`
  §5 and `CLAUDE.md`'s trusted-post-Claude-token boundary).

What this run explicitly does **not** claim, because it cannot observe
it from inside this job:
- Whether the `create-pr` job actually ran and successfully opened a PR
  automatically. That job runs after this one and its result is not
  visible from here — it must be checked afterward from the Issue/PR
  timeline (a PR appearing under this Issue with no manual "Create a PR"
  link having been used), not asserted by this comment.
- Any change to `arrickcherney-ops`'s collaborator access. Per the
  existing Findings above, that account was owner-confirmed to still
  hold `write` access as of PR #93's review, and revoking it is a GitHub
  Settings action for `wayne19870129` to take directly — this run does
  not touch it and cannot verify a state change either way.

**This remains a partial acceptance of Issue #92 for the same reason PR
#93 was partial**: the legacy account's collaborator access has not yet
been revoked. This PR/commit therefore does not carry a `Closes #92` (or
`Fixes`/`Resolves`) line. Remaining human action before Issue #92 can
close: (1) `wayne19870129` revokes `arrickcherney-ops`'s repository
collaborator access in GitHub Settings, and (2) confirmation, from the
Issue/PR timeline produced by *this* run, that the `create-pr` job
created a PR automatically without a manually-pasted "Create a PR" link
being needed.

## Post-#91 live-acceptance result (externally verified, this round)

Item (2) above is now resolved. After `wayne19870129` configured
`CLAUDE_PR_TOKEN`, the previously-failed `create-pr` job in Actions run
`34809127882` (the same run as this task's Claude job) was rerun and
completed successfully. This session independently verified the
following, using the CI-status/workflow-run-details/job-log tooling
available here (this sandbox still has no `gh`/raw GitHub API network
access, so nothing below relies on that):

- Both jobs of run `34809127882` — `claude` and `create-pr` — show
  `conclusion: success`.
- The `create-pr` job's log records `CLAUDE_BRANCH:
  claude/issue-92-20260914-0518`, `PR_TOKEN_CONFIGURED: true`, and ends
  with `Created PR; normal pull_request workflows will evaluate it` at
  `2026-09-14T05:48:05Z`.
- PR #94 exists, is open, and was created from branch
  `claude/issue-92-20260914-0518` into `main` — the same branch this run
  pushed to, carrying exactly the two docs files this run touched.
- On PR #94's head SHA, all three automatically-triggered checks
  completed successfully with no `action_required` state observed:
  Risk classification (run `34810950593`), Security (run
  `34810950585`), and CI (run `34810950604`), all created
  `2026-09-14T05:48:07Z` — seconds after `create-pr` logged success,
  consistent with the `pull_request` event firing them automatically
  rather than a manual dispatch or an approval gate.

This confirms, as an observed fact rather than something a future
session needs to re-derive from the Actions timeline, that the
`create-pr` job did run and did open a PR automatically, and that its
resulting `pull_request`-triggered checks ran without a maintainer-
approval gate on this SHA.

What remains unresolved is unchanged from the checkpoint above: per
independent review of this PR, `arrickcherney-ops` still holds `write`
collaborator access on this repository; this session still has no
`gh`/GitHub API network access to re-query that permission directly, so
it continues to be recorded as reviewer/owner-observed current state,
not something this audit confirms itself (same limitation already
described in Findings above). Revoking it remains a GitHub Settings
action only `wayne19870129` can take directly, so Issue #92 stays open
and no closing keyword is used in this PR.

Also still pending: TASK-T24's later same-Issue branch → existing
automation-owned PR-head validation (landing a later same-Issue branch's
commits onto an already-open PR's own head/CI). This round pushed the
first commit to a brand-new branch/PR pair, not a second branch onto an
already-existing PR, so that specific path remains unexercised and is
explicitly left pending here.

## Post-#91 live-acceptance round 2: follow-up automation commit hit the approval gate (this round)

The round-1 fix commit above (`c98648b76de9aa3ed2b17c53e4641568cfbe7d70`)
was pushed to the already-open PR #94 by this session's own standard
`git-push.sh` step — the same-repository Claude Code Action identity, not
the separate `create-pr` job's `CLAUDE_PR_TOKEN` path that created the PR
in the first place. Per independent review of that commit, its three
`pull_request`-triggered checks (CI run `34811194942`, Risk classification
run `34811194960`, Security run `34811195021`) initially entered
`action_required`, and the maintainer UI showed "3 workflows awaiting
approval," before `wayne19870129` approved them. This session's own
CI-status tooling, queried after that approval, independently confirms
the resulting state: Risk classification and Security both show
`conclusion: success`, and CI is `in_progress` (running as a second
attempt on the same run IDs) — consistent with, though not a substitute
for re-observing, the pre-approval `action_required` transition itself,
which had already resolved by the time this session queried it (this
session has no `gh`/GitHub API network access to pull historical run-
attempt/status-transition data directly, the same category of limitation
already noted above for the `arrickcherney-ops` collaborator-permission
check).

This is a **material distinction from the round-1 record above, not a
contradiction of it**: round 1 correctly recorded that the `create-pr`
job's own PR-creation event and that event's resulting checks ran without
an approval gate on the initial SHA (`d45e21b`). This round's evidence
shows that a *subsequent* commit pushed to the same PR through the
standard Claude Code Action identity (rather than through the
`CLAUDE_PR_TOKEN`-backed `create-pr` job) re-triggers the approval gate
on its `pull_request`-triggered runs. This matches — and is now confirmed
against this automation path specifically — the already-documented
Issue #76 "failure class B" finding in `docs/83-project-continuity.md`
§5: `pull_request`-triggered runs enter `action_required` whenever a
workflow using the repository's own token creates/updates a PR; only the
narrow `CLAUDE_PR_TOKEN`-backed PR-creation call has been shown to avoid
it.

**Conclusion: approval-free automation is therefore validated only for
the initial PR-creation event, not for follow-up commits pushed to an
already-open PR through the standard Claude identity.** Do not describe
this workflow's approval behavior as fully resolved end-to-end; Issue #76
already tracks failure class B as open on exactly this basis, and this
round's evidence is additional confirmation of that open state, not a
new or separate problem to file.

What remains unchanged from the checkpoints above: `arrickcherney-ops`
still holds `write` collaborator access (unverifiable from this session,
recorded as reviewer/owner-observed state); TASK-T24's later same-Issue
branch → existing-PR-head validation remains pending; Issue #92 stays
open and this PR carries no closing keyword.

**Correction (round 3): the "Issue #76" framing above is superseded —
see the round 3 section below.** Round 2's conclusion described the
approval-gate observation as "additional confirmation of the
already-open Issue #76 'failure class B' finding" and said "Issue #76
already tracks failure class B as open." Per round-3 independent
review, Issue #76 is actually **closed** (`state_reason=completed`),
and Issue #79 (an earlier, separate proposal about the manual-approval
requirement) is also **closed** (`not_planned`). Neither is an open
tracker for this behavior. Do not read the round-2 text above as
correctly describing Issue #76's current tracking status.

## Post-#91 live-acceptance round 3: collaborator cleanup and issue-tracker correction (this round)

Two findings from round-3 independent review, both accepted as
**VALID**, with the same caveat stated for each: this session still has
no working `gh`/GitHub API network access and a `WebFetch` attempt on
the relevant public issue pages was not granted permission in this
session, so neither fact below is independently re-confirmed by this
session itself — both are recorded as externally/reviewer-reported
current state, the same epistemic status already used throughout this
document for the `arrickcherney-ops` collaborator-permission fact.

1. **Collaborator access change — VALID.** Per round-3 independent
   review, the repository owner (`wayne19870129`) has removed
   `arrickcherney-ops`'s elevated repository access; an independent
   permission query now reports **`read`**, not the `write` recorded in
   every earlier section of this file. This is a collaborator
   *permission* change, not a destructive account deletion and not a
   Git history rewrite — nothing about PR #91's historical authorship
   changes. This resolves the collaborator-access item that Findings and
   every prior checkpoint above listed as the reason Issue #92 stays
   partial.
2. **Issue #76 / Issue #79 tracker correction — VALID.** Issue #76 is
   closed (`completed`) and Issue #79 is closed (`not_planned`); neither
   should be cited as an open tracker for the `pull_request`-triggered
   approval-gate behavior ("failure class B") discussed in round 2 above
   and in `docs/83-project-continuity.md` §5. That behavior itself is
   not asserted resolved or unresolved by this correction — this round's
   own CI-status check on the pre-this-round head SHA
   (`2bd2749d2b2f7f5b4c0b947151b5aac59ba79358`) independently confirmed
   via this session's own CI-status tooling that CI, Security, and Risk
   classification were all `action_required` at review time, consistent
   with the approval-gate pattern round 2 already described. Going
   forward, track that observation under Issue #92 / this TASK file
   directly rather than citing #76 or #79, since neither is open.

With finding 1 resolved, the only remaining open item from the original
Findings section is the approval-gate/CI behavior itself: whether a
push made after this collaborator-permission change still requires
maintainer workflow approval on its `pull_request`-triggered checks.
This round's own commit is the fresh post-removal observation point;
per the reviewing instruction for this round, its resulting workflow
state is left for the next review round to observe and report, not
pre-claimed here. TASK-T24's later same-Issue branch →
existing-automation-owned-PR-head validation also remains pending and
unexercised.

**Issue #92 stays open**: the approval-gate acceptance criterion is
unresolved and TASK-T24's pending validation item is unresolved. This
commit/PR carries no `Closes`/`Fixes`/`Resolves #92` line.
