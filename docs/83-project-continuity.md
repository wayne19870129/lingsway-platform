# Project Continuity / Handoff

**Last reconciled with main:** `94258d4da6ef0477104df0671202e6501f2b9b94`
(committed 2026-09-13 01:11 UTC / 2026-09-12 18:11 -07:00 — dates in
this document are UTC unless stated otherwise, so a local-time reading
of a commit's own timestamp can legitimately differ by a day), during
Issue #74; corrected 2026-09-13 on this same PR (#75) after independent
review found the Marzban provider baseline, the "Open" task list, an
in-flight automation failure, and a role-wording contradiction all
needed fixing — see sections 3, 5, 6, and 7.

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
- **Claude Code**: the sole implementation executor. Edits code, runs
  tests/lint/build, creates branches, commits, and pushes; reads and
  judges review feedback and pushes fixes. Never merges, never deploys,
  never closes another party's PR/Issue. On the Issue-first background
  path (section 5), Claude itself does not call `gh`/create the PR —
  mechanical PR creation and CI/Security/Risk dispatch are the
  repository's own deterministic workflow step's responsibility, run
  after the Claude Code Action step completes (see section 5's
  `claude-ensure-pr-and-dispatch.sh` description, including its current
  known failure mode). When triggered directly on an already-open PR,
  Claude pushes new commits to that same PR's branch itself.
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

Current design of `.github/workflows/claude.yml`, established across
Issues #67, #71, #68 (TASK-T19, TASK-T20, TASK-T21 respectively — all
merged into `main` as of the SHA above):

- Owner-only `@claude` trigger (`github.actor == 'wayne19870129'`) with
  explicit bot-comment exclusion, so a status comment from `claude[bot]`
  cannot itself re-trigger a run.
- Actor-scoped concurrency group (issue/PR number **+** actor), so a bot
  status comment can never cancel the human-triggered run that produced
  it (TASK-T19 / Issue #67 — previously bot comments could
  self-cancel the real run via a too-broad concurrency group).
- Exact-instruction dedup: a hash of the triggering instruction is used
  to detect and skip duplicate delivery of the same event.
- One Issue cannot spawn a second PR: a preflight check (GraphQL
  `closedByPullRequestsReferences`) plus a branch-naming fallback check
  stop a second `@claude` instruction on the same still-open Issue from
  opening a second PR; follow-up work goes to the existing PR instead.
- A trusted `snapshot-scripts` step copies the deterministic helper
  scripts (`scripts/claude-ensure-pr-and-dispatch.sh`,
  `scripts/claude-run-summary.sh`) to `$RUNNER_TEMP` from the trusted
  default-branch checkout **before** Claude can switch the workspace to
  a PR branch — closing a trust-boundary gap where an attacker-controlled
  PR branch could otherwise get these scripts executed with the job's
  write token (TASK-T20 / Issue #71, Round-1 review Critical finding).
- PR creation and CI/Security/Risk workflow dispatch are done by a
  deterministic `if: always()` shell step
  (`scripts/claude-ensure-pr-and-dispatch.sh`) run **after** the Claude
  Code Action step, not by Claude calling `gh` itself — Claude's
  `--allowedTools` were never widened to include `gh`/generic shell.
- `pre_head_sha` protection: the PR's head SHA is captured before Claude
  runs; the ensure-pr step compares it against the post-run head so a
  run that produced no new commit is never mistaken for "done" (false
  completion marker, TASK-T20 Round-1 Major finding).
- A `workflow_dispatch`-triggered `claude-recovery` job (owner-only) can
  re-run the deterministic PR/dispatch logic without re-invoking the
  model, giving a cost-free recovery path if only the deterministic step
  failed.
- No auto-merge, no auto-deploy, anywhere in this workflow.
- Main session model selector: rolling alias `sonnet` (not a pinned
  dated model ID); effort: `medium`; `--max-turns 30` — all set via
  `claude_args` (`--model`/`--effort`/`--max-turns`), sourced from a
  single job-level `env:` block (TASK-T21 / Issue #68) so the value isn't
  duplicated and drift-prone across the run-summary step.
- Auxiliary/background model usage (e.g. Haiku appearing in
  `modelUsage` for subagent/internal calls) is expected and is **not** a
  main-model switch — the run summary treats `init.model` as the sole
  source of truth for "actual main model," and reports the full
  `modelUsage` key list separately for visibility.
- The run summary (`scripts/claude-run-summary.sh`) distinguishes
  configured selector/effort from the actually-initialized main
  model/effort and the full `modelUsage` list, alongside turns,
  estimated cost, permission-denial count, and exit status — without
  ever setting `display_report`/`show_full_output` or emitting raw
  transcript/tool-output/secrets.

See `docs/82-tasks/TASK-T19-claude-workflow-concurrency-actor-scoping.md`,
`docs/82-tasks/TASK-T20-claude-workflow-tool-permission-mismatch.md`, and
`docs/82-tasks/TASK-T21-claude-workflow-rolling-sonnet-medium-effort.md`
for the full root-cause analysis and verification detail — not
duplicated here.

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

**Issue #79 (durable identity-tooling finding, 2026-09-13):** two
distinct identities/permission boundaries are in play here and must not
be conflated:

1. **The existing Claude Code integration/App** that runs this
   repository's `@claude`-triggered sessions themselves (model turns,
   file edits, `git push` of the branch). That session's own stated
   capabilities confirm directly ("Modify files in the .github/workflows
   directory (GitHub App permissions do not allow workflow
   modifications)" under "What You CANNOT Do") that it does **not** hold
   write access to `.github/workflows/**` — this is what blocked
   applying TASK-T22's diff from within this Issue's own session.
2. **A separate, newly-provisioned automation GitHub App**
   (secrets `CLAUDE_AUTOMATION_APP_ID` / `CLAUDE_AUTOMATION_APP_PRIVATE_KEY`,
   installed only on `wayne19870129/lingsway-platform`) that Issue #79's
   human setup step created *for a different purpose*: to be the
   token-minting identity that `claude.yml`'s deterministic `ensure-pr`/
   `claude-recovery` steps would use *instead of* `GITHUB_TOKEN`, once
   the diff below is applied. As of this document, that App is **not**
   yet wired into `claude.yml` at all (confirmed: all `GH_TOKEN` lines
   in `claude.yml` still read `${{ github.token }}`) — it does not
   "back this repository's Claude Code integration" today; it is
   provisioned-but-unused infrastructure for the future deterministic
   ensure-pr/recovery token path.

The fix designed for Issue #79 — minting a token from App #2 inside
`claude.yml` and using it (instead of `GITHUB_TOKEN`) for
`scripts/claude-ensure-pr-and-dispatch.sh`'s `gh pr create`/`gh workflow
run` calls in both the `claude` job's `ensure-pr` step and the
`claude-recovery` job — cannot be applied by an `issue_comment`-triggered
Claude Code session itself, because that session runs under App #1's
permission grant, not App #2's. The exact diff is fully specified in
`docs/82-tasks/TASK-T22-claude-automation-app-token-identity.md` for the
repository owner (or a Claude session invoked through a path that does
hold `.github/workflows` write scope, e.g. a local CLI run) to apply
directly. **Under the current issue-comment-triggered Claude Code
integration/tooling boundary (App #1), any Issue whose fix requires
editing anything under `.github/workflows/**` will hit this same
"diff specified, not applied" stopping point** — this is a statement
about the current integration's permission grant, not an immutable
forever-rule; a future permission model or an explicitly assigned
executor with workflow-write scope could change it. Issue #79 remains
open: the App-token identity swap and its live validation (does the
resulting `pull_request`-triggered CI/Security/Risk run actually skip
`action_required`?) are both still outstanding pending that manual diff
application.

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
- **Actual next technical frontier**: TASK-T16 Phase 2C (real provider registry wiring/opt-in selection) —
  `build_registry()` is still mock/noop-only (verified directly against
  `registry.py`, section 6) while the Marzban accounting adapter and
  `ProviderRegistry` lifecycle/close() foundations already exist. Do not
  assume any real provider is live-selectable regardless of environment
  variables (see section 6).
- **Issue #79 (TASK-T22) remains open, superseding Issue #76's
  remaining failure-class-B scope.** The App-token identity design and
  minimum permission set are fully specified in
  `docs/82-tasks/TASK-T22-claude-automation-app-token-identity.md`,
  including the exact `.github/workflows/claude.yml` diff — but that
  diff could not be applied by the Issue-triggered Claude Code session
  itself (no `.github/workflows` write scope; see section 5's Issue #79
  entry). Next step is the repository owner applying that diff manually,
  then running the live validation plan in the same TASK file.
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
