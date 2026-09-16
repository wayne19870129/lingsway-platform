# Project Continuity / Handoff

**Last reconciled with main baseline:** `3943bfdb149bfdfa204ad627d11e37c1ce161abe`
(verified 2026-09-16 UTC after PR #113 was manually merged). PR #112 and
PR #113 are complete for their accepted slices; Phase 2B is **COMPLETE** and
Phase 2C is **ACTIVE**. The active vehicle is Phase 2C3B Xray runtime provider
wiring on branch `task/t16-2c3b-xray-runtime-provider-wiring`; its PR is
open pending independent exact-SHA review. Codex / GPT-5.6 Luna High is the
sole writer, Claude Code Web is fully stopped, ChatGPT is the independent
reviewer, and the User performs manual merge and production approval.
Dates in this document are UTC unless stated otherwise.

This document is maintained by whichever agent last touched a section
below; if it looks stale, the next agent should refresh the relevant
section rather than trust it blindly (see "Maintenance discipline").

## 1. Purpose and authority

This is a **living handoff/index**, not a replacement for ADR, `AGENTS.md`,
`CLAUDE.md`, TASK, or PR evidence. It exists so that a brand-new
Claude/ChatGPT account or session — with no memory of prior chats — can
reconstruct enough context from GitHub alone to resume work safely.

Authority order is unchanged and this document does not alter it:
**ADR > `AGENTS.md` > review feedback**, with `CLAUDE.md` providing the
day-to-day operating procedure on top of that order. Where anything below
conflicts with a more authoritative or more current source (an ADR, a
TASK file, `AGENTS.md`, or the actual state of `main`), the authoritative
source wins and this document is wrong and should be corrected in the
same PR that notices the drift.

## 2. Fresh-account bootstrap

A brand-new session should read, in order:

1. `CLAUDE.md` — day-to-day operating procedure and PR workflow.
2. `AGENTS.md` — iron rules, module write-boundaries, credential rules.
3. This document (`docs/83-project-continuity.md`).
4. The ADR(s) under `docs/80-decisions/` relevant to the area being
   touched (see section 6 for pointers into the current decision set).
5. The relevant `docs/82-tasks/TASK-*.md` file, or the open GitHub Issue,
   for the task at hand.
6. If continuing in-flight work: the current PR, its exact head SHA, and
   its CI/review status — a review or comment referencing an older SHA
   than the PR's current head is stale (see `CLAUDE.md` "Work review
   format, SHA scoping, and de-duplication").
7. `README.md` for the safety invariants, risk-classification table, and
   "Explicitly out of scope" list, plus `ARCHITECTURE.md` if touching
   cross-cutting structure.

**Do not rely on previous-chat memory.** Reconstruct current state from
GitHub (Issues, PRs, TASK/ADR files, and the actual code on `main`), not
from anything a prior conversation "remembers."

## 3. Collaboration model

- **User (human)**: owns requirements, final acceptance, manual PR merge,
  and any production/go-live decision. No automation replaces these
  three things.
- **Claude Code**: the standing abstract implementation role described by
  the repository process. It is paused for the current PR #112 and must not
  write concurrently with Codex.
  TASK-T24 adds a small isolated PAT-backed PR-creation job; normal
  `pull_request` events then run CI.
- **ChatGPT / ChatGPT Work**: requirements/acceptance-criteria
  orchestration and independent review of a PR at an exact commit SHA.
  Work is an **event-triggered reviewer**, not a continuous background
  controller, unless a specific automation says otherwise (none currently
  does — see `AGENTS.md` "协作角色与职责").
- **GitHub Issue / `docs/82-tasks/TASK-*.md`**: the canonical record of
  requirements and acceptance criteria. A requirement that only exists in
  chat is considered unrecorded.
- **GitHub PR**: the canonical record of implementation, review, and CI
  handoff — the full back-and-forth (commits, comments, checks) should be
  reconstructable from the PR timeline alone.
- One task, one PR; fixes from review feedback land as new commits on the
  same PR/branch, never a second PR for the same task.

**Current execution mode (updated for PR #112, `task/t16-2c2-patched-marzban-image`):**
Codex / GPT-5.6 Luna High is the sole implementation writer. Claude Code Web
is paused and must not write concurrently. ChatGPT is the independent exact-SHA
reviewer/planner, and the User retains final manual merge and production
approval. No `AGENTS.md` change is needed; this is a task-level operating note
and should be updated when PR #112 or the operating mode changes.
## 4. Review / merge policy

- Final merge is always a human action — no agent merges a PR, regardless
  of CI color or review verdict.
- Independent review happens against an **exact head SHA**; a review
  posted against an older SHA that a later push has superseded is stale
  and should be noted as such, not re-litigated.
- Required CI/Security/Risk checks must be green on that **same SHA**
  before a PR is "可以人工合并" (ready for human merge) — a `PASS` review
  plus incomplete CI is not the same fact as a `PASS` review plus green
  CI.
- Review output format: `Critical` / `Major` / `Minor` /
  `Checks performed` / `Verdict`, where `Verdict` is exactly `PASS` or
  `NEEDS_CHANGES`. This format is not itself proof of origin (Work and
  Claude may post through the same GitHub identity) — see `AGENTS.md`
  "身份识别注意事项" and `CLAUDE.md` "Work review format, SHA scoping,
  and de-duplication".
- Cap automatic rework at **5 rounds per PR**. Stop early on repeated
  failure of the same fix, a genuine architecture disagreement, a
  `NEEDS_CLARIFICATION` finding, or anything destructive/production-
  affecting — these are decision points for the user, not something to
  keep guessing at. There is no 6th automatic rework round.
- A one-time conditional auto-merge exception
  (`.github/workflows/claude-automerge.yml`,
  `docs/82-tasks/TASK-T18-conditional-auto-merge.md`) was tried and
  reverted by the user after PRs kept cycling through valid
  `NEEDS_CHANGES` rounds without ever reaching it. Do not reintroduce any
  form of auto-merge without the user explicitly asking again.

## 5. Background Claude Code automation baseline

TASK-T24 completes the minimal design after PR #88's live test:

- Only newly-created Issue comments are observed.
- Only `wayne19870129` can start a run, and the comment must contain
  `@claude`; this single job-level condition also excludes every bot and
  external user without separate loop-prevention logic.
- The workflow grants repository contents, pull requests, and issues
  write access, plus the OIDC permission required by the standard Claude
  Code GitHub App authentication path.
- Claude uses `CLAUDE_CODE_OAUTH_TOKEN` and standard OIDC authentication.
- A separate trusted runner uses `CLAUDE_PR_TOKEN` only to compare/list/
  create/update a PR, or merge a later same-Issue branch into an existing
  PR's own head, after successful Issue implementation. This token is
  never given to the interactive Claude step. Same-Issue creation jobs
  serialize without cancelling the running job; existing PRs with no new
  branch, or no diff, mean no new PR. No workflow dispatch is involved.
  **Resolved architectural decision (Issue #89 / PR #91 round 3):**
  GitHub's PR API cannot repoint an existing PR's head branch, so landing
  a later same-Issue branch's commits on an existing PR's own head/CI
  needs a write to that branch ref (`git merge`/`git push` equivalent),
  i.e. Contents:write. Round 1 of that PR's review added this but flagged
  the grant as broader than the intended small PR-creation tail; it was
  reverted to Contents:read + a pending-branch-pointer fallback; a later
  round then asked for the head/CI reachability back while keeping
  Contents read-only, which is mutually exclusive with the first ask under
  GitHub's REST surface and was recorded as `NEEDS_CLARIFICATION`. The
  repository owner has since approved a narrow Contents:write grant
  confined to this one branch-to-branch merge inside the trusted
  post-Claude job. The helper now merges a later same-Issue branch into an
  existing PR's head only when that PR's head is itself a same-repository
  branch this automation created for the same Issue (matching its
  `claude/issue-<n>-` prefix); a same-Issue PR matched only by body text,
  or a fork head, or a real merge conflict, still falls back to the
  idempotent pending-branch pointer rather than a forced write.
- The generated PR body carries a trustworthy full-vs-partial
  Issue-closure signal: `create-pr`'s existing Contents-read
  `compareCommitsWithBasehead` call already returns every commit message
  on the branch, and `scripts/claude-create-pr.cjs` trusts only a literal,
  anchored `Closes #<issue-number>` line for the same Issue (never prose)
  — otherwise it states the Issue stays open, never emitting a negated
  closing-keyword phrase (the #77/#76 failure mode). Raw commit-subject
  text quoted in the body's Summary section is defused (zero-width space)
  so an incidental `Closes #N` for an unrelated Issue in a commit subject
  can never act as a real closing reference. **This signal is now fully
  wired end to end:** `claude.yml`'s `--append-system-prompt` (added by
  the maintainer directly, since Claude sessions cannot push
  `.github/workflows/**` changes) instructs Claude to place a line
  containing exactly `Closes #<issue-number>` in a commit message if, and
  only if, its change fully resolves the triggering Issue, and to never
  write Closes/Fixes/Resolves next to an Issue number otherwise. TASK-T24
  is synchronized with this wired state.
- Scoped verification tools, preinstalled dependencies, Actions read
  permissions and a 30-turn limit replace the previous unbounded run.
- There is no custom App token, manual workflow dispatch, dedup job,
  completion marker, recovery job, automatic approval, automatic **PR**
  merge, automatic Issue close, or deployment step. (The one branch-to-
  branch `repos.merge` call above is a narrow exception scoped to
  consolidating a same-Issue automation-owned branch into an existing PR's
  head — it never merges a pull request itself, and human PR merge remains
  mandatory.)
- The earlier request to restore workflow-wide cancellation is superseded
  by the small serialized creation job in TASK-T24. No manual concurrency
  patch or old helper framework should be restored.

Historical TASK-T19/T20/T21 documents explain the superseded complex
design. TASK-T24 is the current workflow definition and setup guide.
Run 34792595873 (Issue #89) disproved the earlier claim that the standard
Action creates PRs: it pushed a branch and only supplied a Create PR link,
with 84 turns and 15 permission denials. New PAT setup and post-merge live
acceptance remain required; green CI on this repair PR does not prove
that the Issue-to-PR flow has been exercised.

**Issue #92 — legacy `arrickcherney-ops` identity audit (TASK-T25, PR
#93; originally filed as TASK-T24 and renumbered during review to avoid
colliding with PR #91's own unrelated `TASK-T24-claude-pr-and-
verification.md`):** a full case-insensitive search of the working tree
found **zero** references to `arrickcherney-ops` anywhere in workflows,
config, docs, or code. `.github/workflows/claude.yml`'s job-level gate
already checks `github.event.comment.user.login == 'wayne19870129'`
only, `.github/CODEOWNERS` already lists only `@wayne19870129`, and this
document's collaboration model (§3) already names only `wayne19870129`
as the owner. The only remaining link to that identity is that GitHub
recorded PR #91 as authored by it (a historical fact, not reproducible
from any file here) and its `pull_request`-triggered checks required
maintainer approval before running — the exact repository/Actions
setting that caused that requirement is not independently verified from
inside this session (no working `gh`/GitHub API network access), so
only the observed facts are stated, not an unverified causal claim. See
`docs/82-tasks/TASK-T25-remove-legacy-identity-audit.md` for the full
audit trail. **Collaborator access:** during PR #93 review,
`wayne19870129` reported directly querying repository collaborator
permissions and confirmed `arrickcherney-ops` currently holds **`write`**
collaborator access on this repository — recorded here as owner-reported
current state (this session could not independently re-query it).
Revoking that access remains an open GitHub Settings action for
`wayne19870129` to take directly; Issue #92 stays open until it's done,
and no closing keyword was used in PR #93.

**This Issue (#74) is itself the first real post-merge live validation of
the TASK-T21 configuration** (rolling `sonnet` + `medium` effort). Do not
change that configuration as part of unrelated work; if this run's own
Actions summary shows a different actual model/effort than configured,
that is a live finding worth its own Issue, not something to silently
patch here.

**Live finding from this same Issue #74 run — unresolved, recovery
path not yet exercised successfully:** Actions run `34730093694` shows
the `claude` job's Claude Code Action step completing successfully and
pushing branch `claude/issue-74-20260913-0116` at commit
`34555478466f66ac67aaa6c878238f7ba5ed10e4`, but the following
deterministic `scripts/claude-ensure-pr-and-dispatch.sh` step then
failed (exit code 1) with `pull request create failed: GraphQL: GitHub
Actions is not permitted to create or approve pull requests
(createPullRequest)`. No PR existed until a human/manual recovery
created PR #75 from that branch; the `claude-recovery` job in that same
run was skipped, not exercised. **Do not assume the ensure-PR step of
this automation is a closed, healthy loop** — this is the first
production evidence that it can fail on a GitHub Actions permission
restriction, and that failure mode has not yet been fixed or verified
recoverable via `claude-recovery`. Fixing this is a workflow-code change
and is explicitly out of scope for this docs-only PR; it should be
tracked as its own follow-up Issue/TASK.

**Follow-up already filed and sequenced: Issue #76.** The ensure-PR
failure above is tracked as Issue #76, which repairs the Issue-first
post-Claude PR/checks closed loop (including the manual Actions-approval
friction observed on this same PR). Per that Issue's own sequencing:
after this PR (#75) is independently accepted and manually merged,
Issue #76 is the immediate next queued workflow-reliability task —
**before** product work resumes at TASK-T16 Phase 2C (section 7). A
fresh session should not skip #76 and start T16 directly, or it risks
reproducing this same broken Issue→Claude→PR loop.

**Issue #76 current status (updated 2026-09-13, live evidence through
PR #78): failure class A is resolved and validated; failure class B
remains open.** This is the single current-status narrative for #76 —
treat any earlier "both failure classes closed out" / "failure class B
... resolved" wording in this document's history as superseded by this
section, not as still-current fact.

*Failure class A — `createPullRequest` denial — fixed and validated.*
Diagnosis (Issue #76 run `34732975891`) confirmed the `createPullRequest`
denial in run `34730093694` was caused by the repository setting
**Settings → Actions → General → Workflow permissions → "Allow GitHub
Actions to create and approve pull requests"** being off — a
repository-level policy GitHub enforces independently of the workflow's
own `permissions:` block, not fixable by widening YAML permissions. The
repository owner enabled that setting on 2026-09-13. PR #77 (the Issue
#76 implementation run, `scripts/claude-ensure-pr-and-dispatch.sh`
surfacing the original `gh pr create` error text plus an actionable hint
on non-zero exit) was created automatically end-to-end from an
Issue-first trigger, confirming failure class A is fixed and validated.

*Failure class B — `action_required` / protected-branch block on
same-repository Claude/Actions PR pushes — still open.* Live evidence
from PR #77 head `1df3441f92fc883fa358eb93c8659277126fbb84`: six
workflow runs fired for that one SHA. The three `pull_request`-event
runs entered `action_required` — expected GitHub behavior whenever a
workflow using the repository `GITHUB_TOKEN` creates/updates a PR, not a
defect and not evidence the deterministic checks failed. Separately, the
deterministic post-Claude dispatch path launched exactly three
`workflow_dispatch` runs on that same SHA, and all three completed
successfully with no maintainer approval required:

- CI run `34736481046` — `workflow_dispatch` — `success`
- Security run `34736481706` — `workflow_dispatch` — `success`
- Risk classification run `34736482463` — `workflow_dispatch` — `success`

At the time of that PR #77 evidence, this document mistakenly concluded
that dispatch-trio success meant failure class B was "resolved." Live
evidence from PR #78 on this same automation path disproved that: PR #78
head `c7d325ae96c3fa57e5058d6a2d3d51d13e7bad43` reproduced the same
dispatch-trio-success pattern, but GitHub still reported the PR as
`mergeable=true` / `mergeable_state=blocked`, while the three duplicate
`pull_request`-event runs on that same SHA remained `action_required`
(approval-gated, since the PR was created/updated through
`GITHUB_TOKEN`). The active `Protect main` ruleset requires a fixed list
of named GitHub Actions check contexts (`lint`, `shellcheck`, `backend`,
`backend-image`, `frontend`, `gitleaks`, `python-audit`, `npm-audit`,
`marzban-contract`); it does not bind those required contexts to a
specific triggering event, so record only what was directly observed —
dispatch-trio success, PR-run `action_required`, and
`mergeable_state=blocked` occurring together on the same SHA — without
asserting that the ruleset requires the `pull_request` event
specifically, since no platform evidence for that causal mechanism has
been checked.

Same-SHA `workflow_dispatch` success is real evidence that the code and
checks themselves execute correctly, but it does **not** remove GitHub's
approval-required gate on the `pull_request`-triggered runs, and it does
**not** unblock the protected-branch merge state on its own. Do not
report a PR as merge-ready on dispatch-trio success alone; the
`pull_request` runs on that same SHA still need to be approved (or the
ruleset/token path changed) before `mergeable_state` clears. Ordinary/
external PRs were never affected either way: they remain subject to
their normal PR-triggered checks and all existing external-contributor
approval protections. The durable fix for zero-manual-approval —
issuing the PR-creating/updating call from a GitHub App installation
token or a PAT instead of `GITHUB_TOKEN`, so GitHub does not classify
the resulting `pull_request` runs as needing approval — has **not**
been implemented. **Issue #76 remains open on that basis** until a PR
created/updated through this automation no longer needs per-SHA
maintainer approval to merge.

**Process lesson from PR #77 (Issue #76 reopened once because of this):**
GitHub parses a leading closing-keyword form (`Closes #N`, `Fixes #N`,
`Resolves #N`) as a closing reference regardless of surrounding negating
prose — a PR body containing the literal phrase "`Closes #76` is
intentionally NOT included" still closed #76 on merge. When an Issue
must stay open, PR bodies/commit messages/comments used for merge
linkage must avoid every literal closing-keyword form entirely; use
wording such as "Issue #76 remains open; no closing keyword is
included."

## 6. Durable technical baseline

High-level stack: Python/FastAPI backend (`backend/app/`), Next.js
frontend (`frontend/`), Debian 12 VPS deployment target,
provider-pluggable architecture behind explicit boundaries
(`backend/app/providers/`, `backend/app/domain/`). See
`ARCHITECTURE.md` and `README.md` for the full picture — not repeated
here.

- **ProviderRegistry is mock/noop-only today — MERGED fact, verified
  directly against `backend/app/providers/registry.py` on the SHA
  above.** `build_registry()` raises `_unsupported(...)` for any
  non-`mock`/`noop` value of `egress_provider`, `accounting_provider`,
  `gateway_provider`, `forwarder_provider`, `payment_provider`,
  `notify_provider`, `email_provider`, `captcha_provider`,
  `storage_provider`, and `transport_provider_mode`. No real provider
  (Webshare, Xray, Mihomo, Marzban) is imported or wired into the
  registry. Real-provider wiring ("Phase 2C") is **PLANNED**, not
  started — do not assume any live accounting/egress/gateway selection
  works today.
- Real provider implementations exist in partial form
  (`backend/app/providers/egress/webshare.py`,
  `backend/app/providers/gateway/xray_file.py`,
  `backend/app/providers/forwarder/mihomo.py`,
  `backend/app/providers/transport/subscription.py`,
  `backend/app/providers/accounting/marzban.py`) but are exercised only
  by their own unit/guard tests, never by `registry.py` or any request
  path. **Correction (verified directly against current `main`):**
  `backend/app/providers/accounting/marzban.py` (`MarzbanAccountingProvider`,
  TASK-T16 Phase 2B7) does exist and implements a production-capable
  HTTP adapter against the pinned Marzban v0.8.4 admin API, with its own
  offline HTTP contract tests. Its own module docstring is explicit that
  *implemented* and *wired/selectable* are different facts: nothing in
  this repository wires it into `registry.py` or selects it via
  `ACCOUNTING_PROVIDER=marzban` yet — that is deliberately a separate,
  later, explicit opt-in task (Phase 2C), so no real Marzban instance is
  contacted by any code path in this repository today. Do not conflate
  "adapter implemented" with "provider live/selectable."
- Full investigation trail (inventory → DB-sufficiency analysis → Marzban
  ownership/route-identity decisions → in-flight `close()`/registry-leak
  bugfix review) is in
  `docs/82-tasks/TASK-T16-real-provider-registry-wiring.md` (long,
  phased document — read the latest phase heading first, don't assume
  page 1 is current) plus:
  - `docs/80-decisions/ADR-014-xray-desired-state-ownership.md` — the
    database, as of this ADR, is `INSUFFICIENT` to express Xray's full
    desired state (no persisted `inbounds`/clients path, Reality
    `privateKey`/`shortIds` not persisted, `DesiredRoutingState` DTO
    lacks outbound connection detail).
  - `docs/80-decisions/ADR-015-marzban-ownership-and-route-identity.md` —
    Marzban is classified `ACCOUNTING` (not `TRANSPORT`, superseding a
    line in ADR-013); it does write the shared `xray_config.json` under
    specific admin-API paths, so this repo's renderer must be established
    as sole writer and a drift-detection mechanism is required before
    Phase 2B can proceed safely.
  - `docs/80-decisions/ADR-016-route-identity-architecture-unblock.md` —
    accepted, resolves the route-identity blocker Decision 3 left open.
  - `docs/80-decisions/ADR-017-provisioning-lock-hold-span-narrowing.md`
    — accepted, narrows the `GatewayRouteBinding` named-lock hold span in
    the paid-purchase provisioning saga.
  - `docs/80-decisions/ADR-018-accounting-create-user-failure-contract.md`
    — accepted, defines `AccountingProvider.create_user()` failure
    ownership.
- Provisioning-lock narrowing, desired-state ownership, unmatched-traffic
  BLOCK, and accounting disable-never-delete are **iron rules /
  ADR-settled decisions** — see `AGENTS.md`'s 铁律 list and the ADRs
  above for the authoritative wording; this document intentionally does
  not restate or paraphrase the rule text itself, to avoid drifting from
  it.
- Alembic history is append-only (`AGENTS.md` rule 7): a schema/code
  mismatch is fixed with a new idempotent migration, never an edit to an
  existing revision.
- Deployment is fail-closed: `deploy-*.yml` workflows run with
  `DRY_RUN=true` until T7 delivers real remote-deployment scripts and
  production Actions secrets are configured (`README.md`).

## 7. Current phase / next-work index

Verify against current GitHub state before trusting this section — it
decays quickly.

- **Merged / resolved** as of the SHA above: Issue #66 (background Claude
  workflow baseline), Issue #67 / TASK-T19 (concurrency self-cancellation
  fix), Issue #71 / TASK-T20 (tool-permission mismatch fix), Issue #68 /
  TASK-T21 (rolling `sonnet` + `medium` effort). **Issue #76 is only
  partially resolved** — failure class A (`createPullRequest` denial) is
  fixed and validated, but failure class B is not: live evidence from
  PR #78 shows same-SHA `workflow_dispatch` success does not clear the
  `action_required` `pull_request` runs or the protected-branch
  `mergeable_state=blocked` state (see section 5's correction above).
  Issue #76 stays open until a PR created/updated through this
  automation no longer needs per-SHA maintainer approval to merge.
- **Correction (reconciled against current `main` and each TASK file's
  own "现状复核/完成情况" section, not left as historical prose):**
  `TASK-T5G-scheduler-jobs.md`, `TASK-T14-ci-audit-retry.md`, and
  `TASK-T15-ip-availability-precheck.md` are already implemented on
  `main` — they are **not** open candidates:
  - `TASK-T5G-scheduler-jobs.md` — `backend/app/workers/drift_check.py`
    and the accounting/transport-sync failure→`DEGRADED` handling are
    landed and covered by `backend/tests/unit/test_drift_check.py` and
    `backend/tests/unit/test_scheduler_transport_failure.py`.
  - `TASK-T14-ci-audit-retry.md` — `.github/workflows/security.yml`'s
    `npm-audit` job already retries only on transient 5xx/network errors
    (bounded attempts, no `continue-on-error`), verified directly in the
    workflow file.
  - `TASK-T15-ip-availability-precheck.md` — the read-only IP-availability
    precheck is implemented in `backend/app/api/public.py` (order
    creation), with the confirm-payment-time re-check still in place.
- **Genuinely still open**: `TASK-T13-split-routing.md` — the code
  change is intentionally *not* started. `CORE_RULES` exist in
  `backend/app/domain/subscription_render.py` but are deliberately not
  wired into rendering (ADR-012); the task's own "现状复核" splits it
  into a Phase 0 (manual, real-client evidence collection — GEOSITE/GEOIP
  capability, single-subscription gray rollout data) that must land in
  `docs/70-external-facts.md` before any Phase 1 code change, per the
  task's own constraints. This is blocked on real-world evidence, not
  forgotten.
- **Issue #76 remains open.** Failure class A (ensure-PR
  `createPullRequest` denial) is fixed and validated. Failure class B is
  **not** resolved: `action_required` `pull_request` runs and the
  protected-branch merge block persist for PRs created/updated through
  `GITHUB_TOKEN`, even when the same-SHA `workflow_dispatch` CI/Security/
  Risk trio is green — see section 5's correction. The zero-manual-
  approval acceptance criterion is not yet satisfied.
- **Current technical frontier**: TASK-T16 Phase 2B writer-guard/three-state drift baseline.
  PR #103/remain-1B and PR #104/remain-3 are already merged. PR #105 was the
  Reality canonical persistence/ownership implementation vehicle and PR #106
  accepted ADR-020 on main. PR #107 was the full-config composition
  implementation vehicle and is now merged on main at
  `5c67aba469f7beb82df13750247c90e2d1820e7a`. PR #108 then completed
  preservation/legal-deletion and merged on main at
  `994fe50416afdbc00648eaf48a65b415b658df3b`.
  PR #104 performed the one coherent full-snapshot and
  `outbound_tags -> outbounds` cutover with current-read freshness,
  credential precedence, and full Xray outbound rendering. The Reality slice
  and ADR-020 are complete on main. Full-config composition is also complete on
  main. This writer-guard/drift PR is the current frontier; when its change is
  present on main, the active order becomes existing-data reconciliation and
  final Phase 2B current-main review. Phase 2B is still **not COMPLETE**.
  Issue #97 and PR #98 are historical Phase 2C attempt artifacts, both closed;
  PR #98 was never merged. Issue #101 is also closed as Not planned. Phase 2C
  remains technically **BLOCKED**, and `GATEWAY_PROVIDER=xray_file` registry
  wiring must remain paused until Phase 2B is complete under the remain-3 gate.
  Real providers remain non-selectable in
  `build_registry()` (see section 6). For future small T16 slices, TASK-T16
  plus the corresponding PR are the durable requirement/acceptance/hand-off
  record; do not create a separate Issue unless explicitly requested.

- **Current Phase 2B gate**: PR #106 is merged and ADR-020 is accepted. PR #107
  and PR #108 are merged on main; the current main baseline is
  `994fe50416afdbc00648eaf48a65b415b658df3b`. Full-config composition and
  preservation/legal-deletion are complete. While this writer-guard/drift PR is
  open, it is the active frontier; when its change is present on `main`, the
  active order becomes existing-data reconciliation and final Phase 2B
  current-main review.
  The concrete process-lifetime Xray provider may hold only immutable non-secret
  skeleton/deployment configuration. Each render receives the operation-scoped
  resolver through the existing `GatewayProvider.render()` argument and reads
  Reality identity through the Xray-only resolver capability; the provider and
  registry retain no Session, resolver, plaintext, or mutable operation state.
  `Settings.xray_log_level` is sourced from `XRAY_LOG_LEVEL`, normalized by
  trim/lowercase, and limited to `debug`/`info`/`warning`/`error`/`none`; blank or
  invalid values fail closed and it is independent from application `log_level`.
  `runtime.current()` remains observation/rollback/drift only; the candidate's
  repo-owned file skeleton includes `settings.clients=[]`, while Marzban
  runtime-only dynamic membership remains outside repo-owned equality. Phase 2B
  remains **NOT COMPLETE**; Phase 2C remains **BLOCKED**; no registry wiring is
  permitted.
  The Xray applied-state sidecar is part of the persistent VPS migration,
  backup, and restore set alongside the database, Secrets/Reality identity, and
  `xray_config.json`.
- **Issue #87 / TASK-T23 replaces the abandoned custom-App proposal.**
  The minimal workflow is implemented in PR #88 and needs post-merge
  live validation with a new Issue comment. TASK-T22 is retained only as
  a short superseded-design marker so older review links remain valid.
- **This Issue (#74)**: adds this continuity document and the
  `CLAUDE.md` startup-reading-order/maintenance-rule update described
  below; also serves as the first live validation of the TASK-T21
  background-automation config (see section 5).

Next real technical work should be picked from the "Open" list above or
from whatever Issues are open on GitHub at read time — this document
does not pre-commit to an ordering the user hasn't set.

## 8. Maintenance discipline

- Update this document only when a **material durable fact** changes —
  not for routine code changes. See `CLAUDE.md`'s maintenance rule for
  the trigger list (collaboration/role split, merge/review/rework policy,
  Claude Code model/effort/automation behavior, major architecture
  decisions or provider/runtime integration state, project
  phase/roadmap, safety/credential/deployment boundaries, or the
  canonical workflow needed to resume work).
- Never copy secrets, tokens, account credentials, personal
  billing/IP details, or ephemeral chat-only information into this file.
- Prefer links and file/Issue/PR references over duplicated prose — this
  document should point at ADRs/TASKs/code, not restate their content in
  a way that can drift out of sync.
- Keep the `Last reconciled with main` field at the top current; a future
  agent should treat a stale-looking SHA/date as a signal to re-verify
  before trusting a section, not as a reason to distrust the whole
  document.
- If a future agent detects drift between this document and the
  authoritative source (ADR, `AGENTS.md`, current code, or a merged PR),
  fix this document in the same PR that notices the drift.

## Current Phase 2B — Existing-data reconciliation

PR #109 已人工合并到 `main`；当前 main 基线为
`4bbcf585bf0fbfae885bec077645828dc7b58776`，writer-guard/drift 已完成。本
PR 是 explicit existing-data reconciliation 的当前 vehicle，目标是把 active
`GatewayRouteBinding` 的旧 `gateway_principal` 与 Marzban 已存在的
`routing_principal` 对齐；Phase 2B 仍 **NOT COMPLETE**，Phase 2C 仍
**BLOCKED**。

工具默认 dry-run，只有 `--confirm` 才允许 Phase B 写入。Phase A 全量读取并
调用 Marzban GET，任何缺失、异常、重复或外部错误整体 fail closed、零 DB
写入；Phase B 使用既有 `gateway_route_binding_write()` named lock，锁内重读并
严格比较 snapshot，再执行原值条件 UPDATE、rowcount 检查和单次 commit。它不
属于 startup/Alembic/deploy/scheduler，也不做真实 Marzban/Xray side effect。
审计只保留安全计数与 reason code。迁移/备份持久态清单为
`docs/90-migration/persistent-state-manifest.md`。

本轮审查补充：Compose 的 `marzban_data` 与
`${MARZBAN_DB_FILE}` / `data/marzban/db.sqlite3` 必须作为两个独立持久态
迁移。reconciliation 若 commit 已成功但 named-lock release 未确认，必须
报告 `committed_with_warning / lock_release_unverified` 并保留 committed
count；不得返回普通 aborted/rollback 叙述，CLI 以 non-zero 要求人工确认。


## Latest authoritative handoff — Phase 2C1

Current main baseline: `9b86d6c3b804c6d700a8f94fe559496f655a4cff`.
PR #109 and PR #110 are merged and the Phase 2B final current-main review is
PASS. Phase 2B is **COMPLETE**; Phase 2C is **UNBLOCKED**.

The active vehicle is PR `TASK-T16 Phase 2C: wire real Marzban accounting provider`
on `task/t16-2c1-marzban-registry-wiring`. `build_registry()` now permits
only the explicit accounting closed set `mock | marzban`; all other provider
categories remain at their existing mock/noop selections and the global default
is unchanged.

The Marzban provider is constructed from central Settings only. Its constructor
does local validation and creates an owned `httpx.Client` without making HTTP
requests; the application-scoped `ProviderRegistry` closes that client exactly
once. Invalid blank URL/credentials, malformed protocol/inbounds, and unsafe
production defaults fail closed before external I/O. The provider continues to
read the patched Marzban `routing_principal` response and never computes or
falls back to a local identity.

The repository patch set exists under `infrastructure/marzban/patches/`, but
`compose.transport.yml` defaults to `gozargah/marzban:v0.8.4` (with only a
`MARZBAN_IMAGE` override), not a built patched image. Record the follow-up
deployment state as `MARZBAN_PATCHED_IMAGE_DEPLOYMENT_PENDING`. No real
staging credentials or network test is used in this slice;
`REAL_STAGING_MARZBAN_NETWORK_TEST = PENDING`. Webshare, Xray, Mihomo,
transport registry wiring, deployment, schema/migration, workflow, and Phase
2C2 remain out of scope.


## Latest authoritative handoff — Phase 2C2 patched Marzban image path

Base `main` at the start of this slice: `5bdf5655d67da1185686d8560609874e4979f5c5`
(PR #111, Phase 2C1 Marzban registry wiring, is merged). The active vehicle
is PR `TASK-T16 Phase 2C: build patched Marzban image path` on branch
`task/t16-2c2-patched-marzban-image`. This slice does not change
`routing_principal`/registry/provider-selection architecture already
settled by ADR-016/Phase 2C1 -- it only builds the missing reproducible
path from the pinned upstream commit to a verifiable patched image, and
wires a fail-closed identity check in front of the one deployment path
that starts Marzban.

**What this slice adds**, all under `infrastructure/marzban/` unless noted:

- `patches/verify_local_source_tree.py` -- local-checkout counterpart to
  the existing network-fetching `verify_pinned_upstream.py`, sharing the
  same `pinned_upstream_manifest.py` constants and (via the new
  `check_pinned_content()`/`check_dependency_versions()` functions moved
  there) the same check logic, so the pinned-file/Xray-formula/dependency
  contract is defined in exactly one place either way.
- `patches/pinned_upstream_manifest.py` additions: `PINNED_TAG`,
  `PATCH_ID`, `ROUTING_PRINCIPAL_PATCH_MARKER`, `IMAGE_CONTRACT_VERSION`,
  the five `IMAGE_LABEL_*` OCI label key constants,
  `REQUIRED_IMAGE_LABELS`, and `validate_image_labels()` -- the single
  source of truth for both what `build_patched_image.sh` attaches and
  what `verify_patched_image.py` checks for.
- `build_patched_image.sh` -- the build entrypoint: fetches the exact
  pinned commit (`git fetch --depth 1 origin <40-char pinned SHA>`, no
  branch/tag/latest fallback) into a `mktemp -d` working directory,
  verifies checked-out HEAD equals the pinned commit, runs
  `verify_local_source_tree.py`, runs `apply_patch.sh --check` then
  `apply_patch.sh`, greps the patched `app/models/user.py` for the three
  expected `routing_principal` markers, `docker build`s the unmodified
  upstream `Dockerfile` (from the fetched tree, `.git` already excluded
  by Marzban's own `.dockerignore`) with the five immutable identity
  labels attached via `--label`, and deletes the temporary source tree on
  exit (trap-based, runs even on failure). No Marzban source is committed
  to this repository at any point. Default tag:
  `lingsway/marzban:v0.8.4-routing-principal`; no
  force/override/skip_patch_check/unsafe/bypass flag exists or may be
  added (`AGENTS.md` rule 3).
- `verify_patched_image.py` -- the deployment-side fail-closed gate:
  `docker inspect`s a candidate image's OCI labels and checks them
  against `REQUIRED_IMAGE_LABELS`. A missing `docker` binary, an image
  that doesn't exist locally, malformed inspect output, or any label
  mismatch (including no labels at all) all fail closed (non-zero exit).
- `deploy/lib/40_stack_up.sh`: new `verify_marzban_image_identity()`,
  called once, right before `compose up --detach` (after
  `validate_runtime_files`, before the nine-step Xray
  render/up/restart/verify/promote sequence starts touching Marzban).
  It reads `ACCOUNTING_PROVIDER` from the actual deployment `.env` file
  (not the inventory-derived shell variable, since that is what the
  backend container actually runs with); when it is `marzban`, it
  resolves the exact image reference Compose will use
  (`${MARZBAN_IMAGE:-lingsway/marzban:v0.8.4-routing-principal}` -- same
  default/override precedence as the compose files) and calls
  `verify_patched_image.py` on it, `die`-ing the deploy on any failure.
  When `ACCOUNTING_PROVIDER` is not `marzban` (e.g. the default `mock`),
  this check is skipped -- it targets specifically the dangerous
  combination the task called out: real Marzban accounting selected
  while the container silently runs unpatched. A custom `MARZBAN_IMAGE`
  override goes through the identical check; there is no bypass.
- `infrastructure/compose/compose.transport.yml` and `compose.probe.yml`:
  default `MARZBAN_IMAGE` changed from `gozargah/marzban:v0.8.4` to
  `lingsway/marzban:v0.8.4-routing-principal`. This changes which image a
  fresh deploy will *try* to start by default (the local patched build,
  which must exist -- built via `build_patched_image.sh` -- or the
  identity check above fails closed); it does not change any provider
  selection default.
- `.github/workflows/ci.yml`: new `marzban-image-contract` job (separate
  runner from `marzban-contract`, since it needs Docker + network to
  fetch upstream and pull a comparison image) that actually runs
  `build_patched_image.sh`, verifies the resulting image's labels pass,
  and verifies the unpatched `gozargah/marzban:v0.8.4` image's labels
  fail. Plus new pytest coverage in `patches/tests/`
  (`test_verify_local_source_tree.py`, `test_image_identity.py`) folded
  into the existing `marzban-contract` job's `pytest infrastructure/marzban/patches/tests`
  run.

**What this slice deliberately does not do**: no real VPS
deployment, no DNS, no real Marzban credentials/user creation, no
Webshare/Xray/Mihomo registry wiring, no payment/schema/migration
changes, no public container registry publishing, no `AGENTS.md` edit, no
change to the `routing_principal`/ADR-016 architecture itself.

**Actual Docker build result in this development environment**: the
patched-image build path (fetch pinned commit -> verify HEAD ->
`verify_local_source_tree.py` -> `apply_patch.sh --check`/`apply_patch.sh`
-> routing_principal marker grep) was run for real against the genuine
pinned commit and passed every step. The final `docker build` layer in
*this* sandboxed development session could not complete because its
outbound network policy returns `403 Forbidden` for `docker.io` registry
blob storage (`production.cloudfront.docker.com`) -- confirmed
independent of this patch by the same failure pulling unrelated public
images (`hello-world:latest`, `python:3.12-slim`). This is an environment
restriction, not a defect in the build script. The full label-attach +
`docker inspect` + `verify_patched_image.py` mechanism (build, positive
verification, and negative verification against an unlabeled image and a
nonexistent image reference) was still proven end-to-end in this same
session using a `FROM scratch` image built by the real local Docker
daemon (no registry pull required) carrying the same five labels
`build_patched_image.sh` attaches -- this is a smoke test of the identity
mechanism, not a substitute for actually building the real patched
Marzban image, which the new `marzban-image-contract` CI job (GitHub-
hosted runners, unrestricted `docker.io` access) does on every PR going
forward. Record: `DOCKER_BUILD_ENVIRONMENT_UNAVAILABLE` (this development
session only; not a statement about CI or any deployment host).

**AGPL / licensing status**: unchanged from Phase 2B4's existing position
in `infrastructure/marzban/patches/README.md` ("Upstream source: fetched,
not vendored") -- this slice fetches upstream source only into a
`mktemp -d` build-time working directory, never commits it, and does not
touch the repository's root `LICENSE`. This slice does not resolve, and
does not attempt to resolve, the operational AGPL-3.0 source-offer /
corresponding-source requirement that actually running a patched Marzban
binary in staging/production will raise (the patched image itself, if
ever started, is a modified AGPL-3.0-licensed work). That decision needs
the repository owner or counsel, not an agent's inference. Record:
`MARZBAN_AGPL_DEPLOYMENT_COMPLIANCE_PENDING` -- this must be resolved
before any real staging/production deployment runs the patched image,
separately from and in addition to `MARZBAN_PATCHED_IMAGE_DEPLOYMENT_PENDING`
being closed by this slice's build path existing.

Phase 2B remains **COMPLETE**. Phase 2C is **ACTIVE**. Phase 2C1 (Marzban
registry wiring) is **COMPLETE**. Phase 2C2 (this slice, the patched image
build/deployment-verification path) awaits independent review; PR remains
**OPEN**, not merged. Real VPS deployment, real Marzban credentials, and
the AGPL operational-compliance decision above all remain open follow-up
work beyond this slice's scope.


## Latest authoritative handoff — Phase 2C2 review follow-up

PR #112 remains the active vehicle on `task/t16-2c2-patched-marzban-image`, based on main `5bdf5655d67da1185686d8560609874e4979f5c5`. The compose defaults remain `gozargah/marzban:v0.8.4` for mock/default deployments. Only an effective `ACCOUNTING_PROVIDER=marzban` selection causes `deploy/lib/40_stack_up.sh` to select and verify `lingsway/marzban:v0.8.4-routing-principal`; custom `MARZBAN_IMAGE` values are verified by the same gate.

The verifier obtains `ACCOUNTING_PROVIDER` from Docker Compose's resolved `backend-api` environment using the same `LINGSWAY_ENV_FILE` source as Compose. It accepts only `mock` or `marzban`; malformed, ambiguous, or unsupported resolution fails closed before `compose up`. Verification precedes stack startup. Codex / GPT-5.6 Luna High is the sole writer for this task; Claude Code Web is paused; ChatGPT independently reviews the exact head; the User performs any final manual merge. Phase 2B is **COMPLETE**, Phase 2C is **ACTIVE**, and Phase 2C2 awaits renewed exact-SHA review.

## Final authoritative handoff — Phase 2C2 Compose v1/v2 provider probe

The accepted baseline for this review was `8b88e875159898a299c1250938272c8bc0fef1fc`. Codex then completed the final self-audit follow-up on PR #112 at exact head `287214a19dcb1d40fc1defc9e9b1e7fa9376058b`.

The Compose portability Major is now closed without changing provider architecture: `deploy/lib/40_stack_up.sh` invokes `compose run -T --rm --no-deps backend-api` after `compose build backend-api`, and the probe imports `Settings` and calls `Settings.from_env().accounting_provider` inside the backend image. It emits exactly one `__LINGSWAY_ACCOUNTING_PROVIDER__=mock|marzban` sentinel. The host accepts only one exact sentinel and fails closed on zero, multiple, blank, noisy, malformed, unsupported, Settings-validation, or command failure. This works through both the repository's Compose v2 (`docker compose`) path and v1 (`docker-compose`) fallback, without dotenv hand-parsing or the v2-only JSON config flag; `LINGSWAY_ENV_FILE`, including quoted dotenv values, remains Compose-owned.

The mock path leaves the upstream Marzban default unchanged. The effective `marzban` path still selects the patched image (or verifies an operator-selected `MARZBAN_IMAGE`) before `compose up`; no registry/provider/network/DB/write/Xray side effect is performed by the probe.

Evidence on this exact head: CI run #366 passed all jobs; the Marzban contract suite passed 69 tests; Security run #370 passed; Risk classification run #263 passed. PR #112 remains OPEN and unmerged, awaiting renewed independent exact-head review. Phase 2B remains COMPLETE; Phase 2C is ACTIVE; Phase 2C1 is COMPLETE; Phase 2C2 awaits review.


## Latest authoritative handoff — Phase 2C3A Xray runtime-control ADR

Authoritative base `main`: `99d299b1df1172583d221c0e19f6c421480c2c8c` (PR #112
merged). This ADR-only vehicle is PR `TASK-T16 Phase 2C: define Xray runtime
control boundary` on branch
`task/t16-2c3a-xray-runtime-control-adr`. Codex / GPT-5.6 Luna High is the
sole implementation writer; Claude Code Web is fully stopped. The User retains
manual merge and production approval. No production code, deployment script,
infrastructure file, test, workflow, AGENTS.md, Issue, or schema was changed.

Phase 2C3's Xray and Mihomo audit found two independent blockers. This slice
only resolves the Xray runtime-control architecture question in
`docs/80-decisions/ADR-021-xray-runtime-control-boundary.md`; it does not
wire a real provider and does not resolve Mihomo.

ADR-021 is a Proposed / Accepted-candidate decision, not formally Accepted
until human merge. It records the verified current topology: backend-api is a
normal Python container that may run the pinned Xray binary for candidate
`xray run -test`, while the Marzban container owns the running Xray process
and mounts the same persistent `xray_config.json`. It rejects
`systemctl reload xray` from backend-api, Docker socket/API, privileged or
host-PID access, application-driven Compose/Docker restart, and any arbitrary
host command.

The selected future activation boundary is the authenticated Marzban
`PUT /api/core/config` endpoint. The exact upstream Marzban v0.8.4 source
at commit
`7f396db3e703d71a28060bc9ce4a532ec64cb1f4` was rechecked in
`app/routers/core.py`: `POST /api/core/restart` starts from Marzban's
in-memory `xray.config` and cannot prove activation of a file externally
changed by Lingsway; `PUT /api/core/config` validates the payload, replaces
in-memory configuration, writes `XRAY_JSON`, includes DB users, restarts the
core, and returns the submitted payload. The next implementation must send
the exact candidate already validated and atomically installed by Lingsway,
verify API acceptance, verify Marzban core/API health plus backend-network
`marzban:8443`, verify the repo-owned projection, and persist APPLIED only
after all checks. Failure uses the existing exact-backup restore and the same
PUT activation path; inability to prove rollback remains DEGRADED/fail-closed.

Lingsway retains DB desired-state, credential-resolution, full composition,
candidate validation, shared-file projection, writer baseline, backup,
rollback, drift, and post-activation verification ownership. Marzban retains
runtime process lifecycle and dynamic user membership. `include_db_users()`
does not transfer repo desired-state ownership, and `runtime.current()` is
not a desired-state source. Process-lifetime provider configuration remains process-stable and immutable after construction. Concrete Marzban clients may privately retain Settings-derived secret fields such as the admin password only under the strict redaction contract defined by ADR-021. No Session, operation resolver, mutable operation context, or bearer token may be retained.
No generic control-plane framework or new generic gateway DTO is introduced.

Mihomo remains blocked under `MIHOMO_RENDER_BOUNDARY_BLOCKER`, plus two
additional blockers recorded for its dedicated ADR: downstream forwarder
compensation after a later saga step fails, and serialization/freshness/
commit-order rules for its global full-config writer. No Mihomo implementation
or registry wiring is part of this slice.

Status: Phase 2B COMPLETE; Phase 2C ACTIVE; Phase 2C1 COMPLETE; Phase 2C2
COMPLETE; Phase 2C3A (Xray runtime-control ADR) ACTIVE and awaiting
independent exact-head review. Phase 2C3B Xray registry implementation is
gated on human acceptance of ADR-021. Phase 2C4A/2C4B Mihomo ADR and
implementation remain pending.


## Review follow-up — Phase 2C3A credential lifetime and verified internal TLS

The review of `61e4b75505ca98472acf2f974e24b25e9a54ac73` identified and this
follow-up closes two documentation Majors without changing implementation.

The concrete Marzban Xray-control helper may retain process-stable Settings-derived
`base/control URL`, admin username, admin password, and TLS trust configuration
as private fields. This follows the existing MarzbanAccountingProvider safety
pattern: zero-network construction, custom safe representation, registry
lifecycle ownership, and strict redaction. The admin password must never appear
in repr, logs, exceptions, audit, metrics, PR/docs text, or plaintext tests.
Authentication is lazy; a bearer token is operation-local, used for the
activation/health operation, then discarded. No hidden global environment read,
Session/resolver capture, or generic credential abstraction is introduced.

The `MARZBAN_CA_CERT_PATH` trust configuration is shared by all Lingsway
clients accessing the same Marzban internal HTTPS endpoint, including the
existing accounting provider and the future Xray control helper. Provider
categories remain independent and share no concrete provider, client
instance, or token cache; they share only central Settings-derived
connection/TLS configuration.


The application-time control URL is `https://marzban:8000` on backend_net.
This is distinct from the Xray health target `marzban:8443`. Verified internal
TLS is required for the normal production architecture. The certificate must
contain `DNS:marzban`, `DNS:localhost`, and `IP:127.0.0.1`; the backend uses
the explicitly configured mounted trust anchor
`/app/data/marzban/internal.crt`, with a central `MARZBAN_CA_CERT_PATH`
setting permitted for the next implementation. `verify=False` is not the
production default.

The existing localhost-only certificate must not be silently overwritten. A
future implementation/deployment must inspect existing SANs and fail closed
with `MARZBAN_INTERNAL_TLS_MIGRATION_REQUIRED` when `DNS:marzban` is absent.
A separately authorized certificate rotation step is required. Fresh VPS
generation must create the complete SAN set. The prior Markdown fence typos
were corrected: `xray run -test` and `xray_config.json`.

Phase status remains unchanged: Phase 2B COMPLETE; Phase 2C ACTIVE; Phase 2C1
COMPLETE; Phase 2C2 COMPLETE; Phase 2C3A awaits renewed independent review;
Mihomo remains blocked.


## Latest authoritative handoff — Phase 2C3B Xray runtime provider wiring

The Phase 2C3A ADR was manually accepted through PR #113 and is effective
from main `3943bfdb149bfdfa204ad627d11e37c1ce161abe`. This slice wires only
`GATEWAY_PROVIDER=xray_file` through the existing provider contract. The
concrete runtime uses Marzban's authenticated `PUT /api/core/config` for
the exact installed candidate, authenticated `GET /api/core` plus TCP
`marzban:8443` for health, and the existing XrayFileProvider backup,
rollback, projection, writer-baseline, and APPLIED/DEGRADED semantics.

Central `MARZBAN_CA_CERT_PATH` is shared configuration for accounting and
Xray control TLS only; providers do not share clients, bearer tokens,
Sessions, or mutable operation state. CA loading and accounting client
creation are lazy, while registry construction remains zero external I/O.
Fresh internal TLS generation now includes `DNS:marzban`,
`DNS:localhost`, and `IP:127.0.0.1`; an existing certificate without
`DNS:marzban`, or an incomplete pair, fails closed with no automatic
rotation. The default provider selections remain mock/noop, Mihomo remains
architecture-blocked, and no real Marzban operation or deployment is part of
this slice.

## Latest authoritative handoff — PR #114 remediation and ADR-022 gate

PR #114 exact head `d8dbe7f1bb1bdef64661ced733c8e4bbb2c6fb3a`
(canonical review `5221920838`) was reviewed with five Majors and one Minor.
This slice is limited to the scheduler CA mount, local TLS pre-dispatch
certainty, production HTTPS enforcement, exact certificate SAN migration
matching, and the lazy accounting-client post-close guard. It also adds the
proposed, not accepted,
`docs/80-decisions/ADR-022-gateway-runtime-reconciliation.md`.

The scheduler receives only the shared `/app/data/marzban/internal.crt`
certificate read-only; it receives no Marzban key/DB mount and publishes no
control port. Local TLS trust setup failure is a typed pre-dispatch error
mapped to `AccountingCreateEffect.NO_SIDE_EFFECT`, while dispatched transport
failure remains ambiguous. Production Marzban selections require a valid HTTPS
URL without embedded credentials or fragments. Existing certificate migration
accepts only the exact SAN token `DNS:marzban`; prefix/suffix lookalikes fail
closed without automatic rotation. A provider closed before lazy client
creation cannot reopen through a stale reference, and injected-client
ownership is unchanged.

ADR-022 audits provisioning binding creation/update/enable,
`release_egress()` disable/release, and confirmed manual principal
reconciliation. It proposes apply-before-commit with explicit downstream
compensation as the minimal launch direction, compares commit-first durable
reconciliation, requires the named lock through compensation, and makes
compensation failure DEGRADED/manual-intervention rather than a stale APPLIED
baseline. It does not implement domain/application changes in this slice.
PR #114 remains OPEN and unmerged; no real Marzban operation, Xray reload,
certificate rotation, or deployment was performed.

## Latest authoritative handoff — PR #114 ADR-022 crash-consistency review

The renewed review of exact head `c18231e13566e82248533ca53c5b611be941a8c7` (canonical review `5223152268`) found no Critical, but three Majors and two Minors. This slice addresses only the documented ADR-022 crash-consistency/projection-wide audit, the reconciliation CA trust-path omission, and Marzban lazy-client lifecycle serialization.

ADR-022 remains `Proposed`, not Accepted. It now rejects the current provider's APPLIED-before-DB-commit behavior as insufficient for process-crash safety, compares durable staged apply/finalization against commit-first durable reconciliation, proposes commit-first based on crash consistency, and specifies that fresh committed DB desired state is the sole restart authority. It inventories GatewayRouteBinding, EgressEndpoint, EgressBinding, referenced credential values, Reality identity, and deployment Settings, classifies current/future writers, makes the existing gateway named lock the single projection-writer boundary, and includes the required A-H crash matrix. No domain/base reconciliation implementation, schema, durable job, or release orchestration is included.

`_marzban_from_settings()` now passes `settings.marzban_ca_cert_path`. `MarzbanAccountingProvider` uses one minimal lock around lazy owned-client creation and close, preserving zero-I/O construction and injected-client ownership. PR #114 remains OPEN and unmerged; no deployment or real Marzban/Xray side effect occurred.

Phase status: Phase 2B COMPLETE; Phase 2C ACTIVE; Phase 2C1 COMPLETE; Phase 2C2 COMPLETE; Phase 2C3A COMPLETE; Phase 2C3B remains ACTIVE/GATED by ADR-022. Mihomo remains separately blocked.

## Latest authoritative handoff — PR #114 ADR-022 Direction B wording review

The renewed review of exact head `aadd9670242e3806f7961ab10f82cd5b2071501e` (canonical review `5223626712`) found no Critical, three documentation Majors, and no Minors. This docs-only follow-up unifies ADR-022 around the selected Direction B commit-first ordering, states its precise partial supersession of ADR-017, and corrects the prospective-versus-live projection writer boundary.

The final ADR sequence is Phase A prospective preparation without the projection lock; projection lock plus fresh read and active-route establishment; first durable commit of DB B, required token state, and pending reconciliation; runtime render/apply/verify; second durable finalization of pending/Subscription/Order; unlock; then best-effort NOTIFY. Gateway failure after commit leaves DB B authoritative and pending/degraded; it does not trigger ordinary rollback or automatic accounting-user disable. The Direction-B-only crash matrix, corrected host/port/protocol credential-source inventory, and future Webshare write gate are documented.

ADR-022 remains `Proposed` and no implementation is included. PR #114 remains OPEN and unmerged. Phase 2B COMPLETE; Phase 2C ACTIVE; Phase 2C1 COMPLETE; Phase 2C2 COMPLETE; Phase 2C3A COMPLETE; Phase 2C3B remains ACTIVE/GATED by ADR-022. Mihomo remains separately architecture-blocked.

## Latest authoritative handoff — PR #114 production Xray hard gate

The renewed architecture review accepted ADR-022's Direction B contract but required a production hard gate because durable reconciliation is not yet implemented. At reviewed head `6f0bde97f5e4e995c55410110e6dc18cd3f06c60` (canonical review `5223833065`), Settings validation now rejects only production `GATEWAY_PROVIDER=xray_file` before registry/provider construction. It does not disable production Marzban accounting when the gateway remains mock, and it does not block development/test Xray contract construction.

This is a safety-only boundary; no ADR-022 state machine or production reconciliation implementation is part of PR #114. The next independent vehicle is Phase 2C3C — ADR-022 durable gateway reconciliation. PR #114 remains OPEN and unmerged. Phase 2B COMPLETE; Phase 2C ACTIVE; Phase 2C1 COMPLETE; Phase 2C2 COMPLETE; Phase 2C3A COMPLETE; Phase 2C3B runtime plumbing COMPLETE but production-gated. Mihomo remains separately architecture-blocked.