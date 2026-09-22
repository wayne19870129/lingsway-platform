# Project Continuity / Handoff

## 1. Authority and resume contract

Authority is **ADR > `AGENTS.md` > REVIEW**. `CLAUDE.md` supplies the daily
procedure and reading order; it is not above an accepted ADR or `AGENTS.md`.
For a fresh session, re-read `CLAUDE.md`, `AGENTS.md`, this continuity index,
the relevant ADRs, TASK, README, and current PR evidence. Before resuming,
re-read mutable GitHub state: current `main`, branch head, changed files, PR
state, reviews, and checks. Do not rely on an older chat or SHA snapshot.

## 2. Roles and workflow

> ## ADR-036 (2026-09-20) is the current split — read this before the rest
>
> **The fixed per-stage checkpoint back to Claude is gone.** ChatGPT now owns
> day-to-day execution end to end: it writes the TASK files, maintains the
> dispatch queue, splits the execution stages, dispatches Codex desktop,
> reviews the result, handles rework, and **moves straight on to the next
> stage in the already-accepted plan** when one finishes. Claude owns the
> overall architecture, the overall plan, and the ADRs, and runs **only when
> the user triggers it**. Every PR is merged by the user by hand.
>
> | Role | Owns |
> |---|---|
> | **Claude** | overall architecture; overall plan; **ADRs**; stage review/adjustment **when the user asks**. No per-checkpoint gate back to Claude. |
> | **ChatGPT** | drives execution within the accepted architecture/plan; splits stages; **writes TASK files**; dispatches Codex; reviews Codex PRs; handles rework; proceeds to the next planned stage |
> | **Codex** | implements; tests; opens PRs; reworks per review; **never widens architectural scope on its own** |
> | **User** | final decisions; **manual merge**; decides when to call Claude |
>
> **What moved**: `docs/82-tasks/**` (Claude-exclusive → ChatGPT writes it),
> the "must have a TASK file first" threshold (was a gate on Claude → ChatGPT
> judges it), `docs/85` §5's dispatch queue, and the end-of-stage
> confirmation. **What did not move**: `docs/80-decisions/**` and
> `docs/81-reviews/**` stay Claude-exclusive; `AGENTS.md`'s iron rule 5 (an
> ADR before any architecture change) is untouched.
>
> **The one remaining architectural stop does not point at Claude.** When
> ChatGPT concludes an accepted ADR's conclusion must change, it **stops
> pushing that change forward, reports to the user**, and the user decides
> whether to wake Claude. The old "every checkpoint automatically goes back to
> Claude" is **not** restored. Nothing else is a stop: how to split a task,
> how fine an instruction card should be, which file list to authorize, which
> implementation to pick, whether to write a TASK at all, how to handle
> rework, which planned stage to start next — ChatGPT decides.
>
> **The cost, stated rather than hidden** (ADR-036 §4): the paragraph below
> about "the specifier and the reviewer are still different agents" **is no
> longer true**. ChatGPT now specifies, dispatches and reviews, so spec-level
> defects have lost their independent finder. Historically that class of
> defect appeared three times (S07's allowed-file list twice, S11's missing
> `risk-classify.yml` checkout premise) and **none of the three was caught by
> review** — two by CI, one by the executor stopping and reporting. What still
> backstops it: the implementer is a different agent; ADRs stay
> Claude-exclusive so ChatGPT cannot self-authorize an architecture change;
> **the user merges every PR by hand** (mechanical since PR #158); nine
> required status checks; and the user's occasional stage review. Consequence
> for ChatGPT: the four-axis scope-closure check when writing a TASK matters
> more than before, not less.
>
> **`AGENTS.md` and `CLAUDE.md` were not edited** (both require the user's
> confirmation). `AGENTS.md` lines 99 and 112 and `CLAUDE.md`'s "Before
> implementation" item 3 still say `docs/82-tasks/**` is Claude-exclusive;
> ADR-036 overrides them under the repository's own ADR > AGENTS.md order, and
> ADR-036 §3 records the exact three-line edit if the user wants them cleaned
> up.
>
> **No background Claude exists, and none may be added** (verified 2026-09-20,
> ADR-036 §5): zero Routines; `claude.yml` triggers only on an
> `issue_comment` containing `@claude` from the user; `pipeline-health.yml`
> runs every two hours but contains no LLM and no Claude credential, by
> deliberate design. ADR-036 forbids adding any Claude-side Routine, schedule,
> auto-wake or periodic review.
>
> **Unchanged by ADR-036**: one task / one PR; `AGENTS.md`'s 禁止自主执行
> table; credential and secret boundaries; the auto-merge breaker at `false`;
> manual production deployment; and every technical conclusion in ADR-035 and
> S04.
>
> ---
>
> **The rest of this section is the pre-ADR-036 state.** It is kept because its
> reasoning is still the reason the current arrangement costs what it costs.
>
> **ADR-032 + ADR-033 (both 2026-09-19) are the current split; together they
> supersede ADR-030's.** **ChatGPT directs and reviews; Codex *desktop*
> implements; Claude Code designs the architecture, writes the ADRs/TASKs,
> **reviews when the user asks for a review**, and can halt the pipeline**;
> a GitHub workflow merges.
>
> ADR-030 had moved dispatch to Claude, but that was designed around Codex
> *Cloud*, which turned out to offer no model or reasoning-effort selection and
> no way to push from its sandbox. Cloud was abandoned; dispatch went back to
> ChatGPT. **Cloud's code review is off too** (ADR-032 §5, corrected): the
> first draft of that section claimed it cost nothing on the executor side —
> wrong, the analytics page states Codex and chat share one quota pool, so
> every review run costs an execution run. It caught two real defects that
> nothing else did (a cross-step branch-comparison bug on PR #142, a live
> `@codex` dispatch instruction left behind on PR #148), so turning it off
> trades capability for quota rather than discarding something useless; ADR-032
> §5 states the condition for switching it back on. It never carried
> authority either way: its `author_association` is `NONE`, so the merge gate
> ignores its verdict.
>
> **Three consequences to not re-derive.** First, ADR-030's independence
> argument now rests entirely on `docs/82-tasks/` and `docs/80-decisions/`
> staying Claude-exclusive: ChatGPT reviews against a TASK it did not write, so
> the specifier and the reviewer are still different agents even though the
> dispatcher and the reviewer are the same one. Relaxing that write boundary
> silently removes the only thing making the review independent.
> **→ ADR-036 (2026-09-20) relaxed exactly that boundary, deliberately and
> with the cost stated. This sentence is therefore no longer a description of
> the current arrangement — it is the reason ADR-036 costs what it costs. See
> the box at the top of this section.** Second,
> **unattended operation is off the table** (ADR-032 §4) — Codex desktop runs
> on the user's machine, so nothing advances while it is off. CI, auto-merge
> and the health digest still run without it. Third, **nobody reads the health
> digest on a schedule any more** (ADR-033 §5): it is still collected every two
> hours, but its `alerts` only reach a person when the user calls for a review.
> Between `main` and that call there is no independent check — which is
> tolerable only because **the user currently merges every PR by hand**
> (ADR-033 §3), and stops being tolerable the moment the workflow starts
> merging PRs the user has not seen (ADR-033 §8).
> **That "moment" has since arrived and been answered** — PR #155 was merged by
> the workflow, and auto-merge is now off; see the resolution note below. So
> "the user merges every PR by hand" is no longer a description of current
> practice that could drift, it is the mechanical state.
>
> **ADR-034 (same day) went one step further and is the current rule:** the
> three remaining mandatory audit triggers are gone too, so **an audit happens
> only when the user asks**, and the user's stated plan is to let ChatGPT and
> Codex run for a long stretch before asking. Two things not to misread.
> **First, what was removed is "Claude must look", not "a human must
> approve"**: `AGENTS.md`'s 禁止自主执行 table still requires human
> confirmation for every production-destructive action, and deploys are still
> manual (ADR-034 §2). **Second, the cost is real and specific** (ADR-034 §3):
> drift that lands early gets built on by everything after it, so a bigger
> batch makes the eventual fix proportionally more expensive. The compensation
> that survives is cheap and passive — the digest keeps collecting, and §10's
> table records each audit's starting SHA so the next one has a span rather
> than a blank page.
>
> **That tension is now resolved: auto-merge is OFF.** ADR-034 §5a had accepted
> the missing audit on one countable fact — that the workflow had never merged
> a PR — and bound it to a condition: the first workflow-merged PR the user had
> not seen forces a choice between restoring the independent audit and turning
> auto-merge off.
>
> **The condition fired. `PR #155` was merged by `github-actions[bot]`**
> (`merged_by` on the API, 2026-09-19T10:02:34Z), with no prior user review of
> that merge. It is the first and so far only PR the workflow merged; #154 was
> merged by the user and #152 carried `no-automerge`. **The user chose to turn
> auto-merge off rather than restore periodic or forced Claude review**, so
> `.github/automerge-enabled` is now `false` and **every PR is merged by the
> user by hand**. ADR-034's on-demand review principle is untouched: no
> cadence, no forced review node.
>
> Two things this does not mean. It does not revoke ADR-029 — the workflow,
> the gate function and its truth table all stay in place and stay tested; the
> breaker simply reads `false`, which is the mechanism ADR-029 §5 exists for.
> And it does not restore the audit: the honest position is that `main` now has
> **no independent post-merge check at all**, and the compensation is that a
> human approves every merge (ADR-034 §2's distinction — what was removed is
> "Claude must look", not "a human must approve").
>
> **Turning it back on is a user-merged PR** (ADR-030 §5a's asymmetry), and
> ADR-034 §5a's condition would have to be answered first: restore the
> independent audit, or explain what replaced it.
>
> One ADR-030 property survives both pivots unchanged: **Claude can stop the
> pipeline alone but cannot restart it alone** (§5a) — restoring the breaker is
> a user-merged PR. Automation that can switch itself back on has no breaker.
> The other one, *supervision is periodic*, is exactly what ADR-033 removed.
> **The thing that puts a person back in the loop is now manual merge itself**,
> not `docs/85` §6.1's third breaker (pause and report after three consecutive
> auto-merges): with auto-merge off that breaker can no longer fire at all. It
> is kept rather than deleted because it would be a precondition for any future
> re-enable.
>
> It is written out in two standing instruction files, both subordinate to
> `AGENTS.md`:
>
> - [`85-agent-operating-model.md`](85-agent-operating-model.md) — ChatGPT's,
>   including §5's dispatch queue and §6's loop breakers.
> - [`86-codex-operating-instructions.md`](86-codex-operating-instructions.md)
>   — Codex's. Written after two number collisions and one out-of-scope
>   dispatch showed that rules living only in documents the executor never
>   reads do not bind it.
>
> Merge policy itself is **ADR-029**: business PRs merge automatically on a
> `PASS` review plus green checks on the same head SHA, gated by the
> `.github/automerge-enabled` circuit breaker; governance and audit PRs stay
> human-merged.
>
> **⚠️ As of 2026-09-19 the breaker is `false`, so nothing merges
> automatically — the user merges every PR by hand.** ADR-029's machinery is
> intact and still tested; it is simply gated off. See the resolution note
> below for why.
>
> **Where that policy actually lives, as of the S08 PR:**
>
> | Part | File |
> |---|---|
> | Trigger → facts collection → merge | `.github/workflows/auto-merge.yml` |
> | The decision itself (a pure function) | `scripts/automerge_gate.py` |
> | The eight-case truth table, run in CI | `backend/tests/guards/test_automerge_gate.py` |
> | Circuit breaker | `.github/automerge-enabled` — **currently `false` (off)** |
> | ~~Dispatch issue template~~ | **Removed 2026-09-19** — see below |
>
> Two properties worth not re-deriving: the workflow deliberately has **no
> `pull_request` trigger**, so a PR cannot rewrite its own gate — every
> trigger runs the default branch's copy, and the breaker is read from the
> default branch, not the PR head. And the merge call passes the decided
> `sha`, so a push landing between the decision and the merge fails the call
> instead of merging an unreviewed commit.
>
> **To pull the breaker:** set `.github/automerge-enabled` to `false` (or
> delete it) on the default branch. That is a normal PR, so it is recorded
> and revertible. A single PR can also be excluded with the `no-automerge`
> label without touching the breaker.

- User owns acceptance, manual merge, and production approval.
- The task-assigned execution agent is the sole writer for its task and branch;
  agents do not modify another active workstream concurrently.
- ChatGPT/ChatGPT Work provides independent exact-head-SHA review and
  acceptance coordination.
- One task uses one branch and one PR. No agent creates a second PR for review
  rework, and no agent merges, force-pushes, or enables auto-merge.
- Reviews are valid only for the exact current PR head SHA. A superseded-SHA
  review is stale and must be rechecked against the current head.
- Human merge-readiness requires CI, Security, and Risk all green on that same
  exact head SHA; review verdict and check status remain separate facts.

### `main` is mechanically protected — corrected 2026-09-19

`AGENTS.md` rule 8 and `CLAUDE.md` both used to state that branch protection on
`main` was unavailable on this plan, so discipline was the only guardrail.
**That was wrong, and it was wrong in the safe direction.** The first live run
of `pipeline-health.yml` tried to push and was rejected:

```
remote: error: GH013: Repository rule violations found for refs/heads/main.
remote: - Changes must be made through a pull request.
remote: - 9 of 9 required status checks are expected.
```

`main` carries a ruleset, and the repository is public, so the "private
personal plan" premise never held either. Two consequences a fresh session
should not re-derive:

1. **Any design that has a machine write to `main` directly will fail.** The
   health digest therefore lives on its own `pipeline-health` branch, written
   through the Contents API. No iron-rule exception is needed, and the one
   briefly added for it has been removed — `main`'s protection stays absolute
   rather than carrying a carve-out that would have to be trusted.
2. **The discipline is not relaxed by this.** Who configured that ruleset and
   when it might change are not under this repository's version control.
   Resting the rule on a setting outside the repo is what rule 8 existed to
   avoid; the backstop is a second line, not a replacement for the first.

## 3. Safety and production boundary

Never place plaintext credentials, tokens, subscription URLs, or secret values
in code, logs, exceptions, reprs, commits, PR text, or continuity notes.
Provider construction and documentation work must not perform real external
requests or runtime activation. Production credentials, deployment, database
destructive actions, Mihomo/Xray install/reload/apply, and external writes
remain subject to the repository safeguards and explicit approval boundaries.

## 4. Durable provider state

Registry selection is explicit and construction is intended to remain zero-I/O.
Defaults are mock/noop.

**Exact `build_registry()` selection matrix** (verified against
`backend/app/providers/registry.py` on 2026-09-18 — re-verify in code, not from
this table, before relying on it):

| Setting | Accepted values | Real implementation reachable? |
|---|---|---|
| `EGRESS_PROVIDER` | `mock` only | ❌ `webshare.py` exists but is **unreachable** |
| `ACCOUNTING_PROVIDER` | `mock`, `marzban` | ✅ Marzban wired |
| `GATEWAY_PROVIDER` | `mock`, `xray_file` | ✅ Xray wired |
| `FORWARDER_PROVIDER` | `mock` only | ❌ `mihomo.py` exists but is **unreachable** (S04 gate) |
| `TRANSPORT_PROVIDER_MODE` | `mock`, `subscription` | ✅ subscription resolver wired |
| `PAYMENT` / `NOTIFY` / `EMAIL` / `CAPTCHA` / `STORAGE` | mock / noop only | ❌ no real implementation exists yet |

Two consequences a fresh session must not re-derive:

1. **Webshare is blocked by more than registry wiring.**
   `WebshareEgressProvider.capacity()` and `.get_tenant_usage()` deliberately
   raise `WebshareContractError` because the recorded API contract does not
   establish the DTO fields/units. Since `ProvisionStep.CAPACITY` calls
   `egress.capacity()` first, wiring `EGRESS_PROVIDER=webshare` today would fail
   every provisioning run at step 1. Closing the `docs/70-external-facts.md`
   evidence gap is a prerequisite, not a follow-up.
2. `docs/84-implementation-roadmap-2026-09.md`'s "registry is stub-only, no real
   adapter is selectable" verdict is **stale** — it predates the Marzban, Xray,
   and subscription-transport wiring above. See that file's own correction banner.

Mihomo
activation, production deployment, and real-provider production authorization
remain independent gates. Mihomo has an accepted full-config and activation
boundary in ADR-023, but implementation, durable activation/recovery, and
production readiness remain separate work. Transport subscription cache
materialization is restricted input, not Mihomo activation — and per ADR-025
§3a it is only usable once anchored by a committed DB receipt; the cache file
is never authority for its own contents.

## 4b. Task numbering: T-series vs S-series

Two numbering schemes coexist and a fresh session must not read this as a
contradiction:

- **T-series (`TASK-T0`…`TASK-T25`)** is the original build-out sequence from
  `ARCHITECTURE.md` §12 / `docs/CODEX_PROMPT.md`. `docs/82-tasks/` holds files
  for T5G and T13–T25 only; the earlier ones were never written as files. T17–T25
  are all **automation/tooling** work (the Claude Code GitHub Action), not product
  work. T13 remains deliberately blocked on `docs/70-external-facts.md` evidence.
- **S-series (`S02`, `S03-A/B`, `S04-A/B1/B2`, `S04-C`)** is the current product
  workstream, started after TASK-T16 Phase 2C. Its boundaries are recorded in
  section 5 below and in ADR-023/ADR-024.

**Process gap, and what was done about it:** S02→S04-B1 (eight merged PRs,
#120–#127) were delivered with **no** TASK file at all, though `AGENTS.md`
named one as the sole record carrier. Three things changed on 2026-09-18:

- Forward coverage: `TASK-S04-mihomo-activation.md` and
  `TASK-S05-compensation-failure-contract.md` are the first S-series TASK
  files. Already-merged S02/S03 scope is **not** retro-filed — writing
  acceptance criteria after delivery proves nothing.
- The rule itself was made enforceable rather than aspirational.
  `AGENTS.md`'s requirement used to be "anything that isn't a small change
  needs an Issue/TASK first", which nobody followed for eight PRs. It now
  lists explicit triggers (multi-PR workstreams; `domain/`,
  `providers/base.py`, migrations, `deploy/`, real external write side
  effects; auth/payment/credentials) and explicitly exempts single-PR bug
  fixes, refactors, tests, and docs.
- **GitHub Issues were dropped as a record carrier** (product owner's call).
  The working method is manual copy-paste between ChatGPT and Codex; nothing
  is dispatched through Issues and the repository has none open. A `TASK-*.md`
  file lives in the repo, is reviewed alongside the PR, and is readable by
  every executing agent, so it was already the better source of truth.
  **`codex-dispatch.md` was deleted on 2026-09-19** (ADR-032 §6b) — it was
  the one template that served dispatch rather than recording, and `@codex`
  on an Issue was measured to trigger nothing. The two that remain,
  `incident.md` and `external-fact.md`, are unaffected: incident write-ups
  and external-fact verification are records, not task dispatch.

**ADR numbering:** ADR-010 does not exist and is not referenced anywhere. The
sequence runs 001–009, 011–026. This is a numbering hole, not a missing document
— do not go looking for it.

**Second collision, this one reached `main` (2026-09-18):** PR #132 added
`TASK-S07-backup-restore.md` while `main` already carried
`TASK-S07-order-queue-billing.md`. Different filenames, so git raised no
conflict and it merged silently — `main` genuinely had two S07 files until the
backup one was renamed to `TASK-S09-backup-restore.md`. This is the same
failure as the ADR-025 case below, and it happened **after** the lesson was
written down, because the rule lived in a document the executing agent never
read. It is now in `docs/86-codex-operating-instructions.md` §2.2, which is
Codex's standing project instruction.

**Resolved collision (2026-09-18):** two unmerged branches independently
claimed ADR-025. PR #128 kept it (`ADR-025-mihomo-projection-generation-authority.md`,
reviewed under that number); the audit branch's compensation-failure ADR was
renumbered to **ADR-026** (`ADR-026-compensation-failure-ownership.md`) before
merge. Neither file existed on `main` at the time, so nothing merged carries
the old number. **Lesson for concurrent branches: claim the next ADR/TASK
number against current `main` plus every open PR, not against `main` alone** —
`TASK-S04-mihomo-activation.md` collided the same way (same filename, two
different contents) and the PR #128 version won.

## 5. Current phase and S03/S04 boundary

> **Status convention (added 2026-09-18):** a PR cannot mark its own work
> COMPLETE, because the merge happens after the PR's last commit. Statuses below
> are therefore written against **merge evidence** (merge commit SHA), and the
> phrase "ACTIVE on `<branch>`" means only "open PR at the time of writing" —
> always re-verify against current `main` and open PRs before trusting it.

- **S02:** COMPLETE.
- **S03:** COMPLETE.
- **S03-A:** COMPLETE. Accepted architecture contract in ADR-024. It preserves multiple
  simultaneous subscription records and defines deterministic descriptor-based
  mapping, opaque secret references, per-record cache isolation, registry-owned
  lazy provider lifecycle, zero-I/O construction, and scheduler failure
  isolation.
- **S03-B:** COMPLETE / merged. It implements the accepted descriptor-based resolver,
  secret revision/snapshot boundary, lazy registry ownership, isolated cache
  identity, and scheduler record isolation. It must not invent DB-aware generic
  provider contracts, plaintext URL configuration, mutable current-provider
  state, mock fallback, or Mihomo activation. Its exact-head review and check
  requirements were satisfied before merge; subscription registry wiring is
  implemented. This does not authorize Mihomo activation or production
  deployment.
- **S04-A:** COMPLETE / merged. Merge commit:
  `e9b4db7b8e77d79fa4f526fbd2cba4262d82e931`. Its exact-head review passed and
  CI, Security, and Risk were green before merge. It establishes the
  ADR-023 immutable/versioned desired snapshot, deterministic full-document
  composer, and exact transport reference/materialization identity and
  freshness proof. It does not activate
  Mihomo, wire `FORWARDER_PROVIDER=mihomo` into production, deploy, or authorize
  real-provider production use.
- **S04-B:** ACTIVE. B1 merged; B2-A merged (PR #128); **B2-B1 merged
  (PR #156, 2026-09-19); B2-B2 next and unblocked** —
  see `TASK-S04-mihomo-activation.md` for the authorized scope of each stage.
- **S04-B1:** COMPLETE / merged from `task/s04-b1-mihomo-durable-reconciliation`.
  Merge commit: `c494ade7d40419afd1a812c8b5ddefb154b1c14c`. It adds only
  the durable Job-backed Mihomo intent, global named writer lock, retry and
  stale-running recovery, commit-outcome certainty, unresolved-intent guard,
  durable global manual blockers for ambiguous finalization and lock-release
  outcomes, injectable reconciliation orchestration, and crash/concurrency
  semantics. An unresolved blocker stops every later Mihomo writer, and a
  release-uncertain blocker is retained until controlled recovery. Finalization
  success and the release-pending blocker share one durable boundary; clean
  release requires confirmed `RELEASE_LOCK == 1` and blocker resolution. It
  remains fail-closed and is not automatically repaired.
  It does not claim S04-B2 or S04-C complete, and does not wire production
  lifespan, real snapshot loading, real secret resolution, runtime readback, or
  Mihomo activation. S04-A guarantees canonical mapping/value ownership for DNS
  only; full
  Mihomo DNS field/type validation remains a later candidate-schema/runtime
  validation gate before production activation.

- **S04-B2-A:** **COMPLETE.** PR #128 merged. It delivered **ADR-025**
  (`ADR-025-mihomo-projection-generation-authority.md`) and the S-series
  implementation TASK (`TASK-S04-mihomo-activation.md`). Architecture and
  documentation only — no Python, no ORM, no migration, as specified.
- **S04-B2-B:** **IN FLIGHT. Current order (2026-09-22):
  `B2-B1 ✅ -> B2-B2a ✅ -> B2-B2.1 ✅ -> .2 ✅ -> .3 ✅ -> .4 ✅ -> .5 ✅ -> .6 ✅
  -> B2-B2c (next) -> B2-B2d -> B2-B3 -> B2-B4`.**
  `B2-B2.6` (preparation + production wiring) merged as **PR #174**.

  **B2-B2c's scope was re-closed on 2026-09-22 and it now gates on
  `ADR-038`.** Its old four-file allowed list could not implement ADR-037 §6:
  `SqlAlchemyCredentialResolver.resolve()` returns a bare `CredentialDTO` and
  never exposes the `Secret.revision` it just read
  (`credential_resolver.py:73-102`), `MihomoForwarderProvider.finalize()`
  accepts a single controller resolver (`mihomo.py:217-219`), and
  `reconcile_mihomo_job()` only ever constructs
  `SqlMihomoControllerSecretResolver(db)` (`mihomo_reconciliation.py:1004-1005`).
  A hand-written fake resolver in `test_mihomo_projection.py` would have made
  all five of B2-B2c's tests pass while the production path still violated
  §6 — **false green, not accepted.** `ADR-038` fixes the interface (a frozen
  `MihomoFinalizationResolvers` bundle; one new method on the *existing*
  resolver with `resolve()` delegating to it, so the decryption logic exists
  exactly once), the allowed list went from 4 files to 8 (all already inside
  the S04-B2-B master union — **the overall scope did not grow**), and three
  new tests in `test_mihomo_reconciliation.py` prove the wiring rather than
  the fake.
  The 2026-09-19 four-checkpoint split is history; ADR-037 extended it to six,
  and the 2026-09-20 re-split broke `B2-B2` itself into six sub-checkpoints.
  Both
  original gates are satisfied: ADR-025 reads `Accepted` and
  `TASK-S04-mihomo-activation.md` is merged. **ADR-025's status is no longer
  a blocker; do not cite it as one.**

  **B2-B1** (receipt binding, SQL controller-secret resolver, canonical
  manifest + generation allocator, DNS candidate gate) **merged as PR #156 on
  2026-09-19**. **B2-B2a (contract substrate: `ForwarderEgressProxyDTO`,
  `DesiredForwarderState.egress_proxies`, the manifest key, and
  `MANIFEST_VERSION` "1"->"2") merged as PR #167 on 2026-09-20.**
  The checkpoints' union
  equals the original B2-B; nothing was deferred to S04-C and no acceptance
  criterion was lowered. The split followed the §6.1 breaker: Codex failed the
  same Round 1 finding set twice, so retrying it unchanged was not allowed —
  and the split worked: the first checkpoint landed.

  **⚠️ `B2-B2` itself hit the same breaker and was re-split on 2026-09-20.**
  Its 12 precise tests went two full rounds without converging; the second
  round already tried splitting the 11 outstanding ones into batches A/B/C and
  still did not finish. The root cause is a **spec defect, not a prompting
  one**: those 12 tests are not 12 independent units — every one of them first
  has to rebuild the same ~130-line fixture graph (12 tables, a transport
  cache file on disk whose sha256 must equal the receipt's `content_hash`, a
  `Secret` whose `revision` must equal the receipt's `source_revision`), so
  batching merely made each batch pay the full fixture cost again. `B2-B2` is
  now six checkpoints: **`.1`** loader core plus a *parameterizable
  fixture base* (PR #163, deliberately **not** wired to production),
  **`.2` / `.4` / `.5`** tests-only checkpoints that may touch
  `backend/tests/unit/test_mihomo_reconciliation.py` and nothing else,
  **`.3`** assignment validation *plus* receipt-verified transport-proxy
  resolution (the one intermediate checkpoint that carries production code),
  and **`.6`** the preparation transaction plus production wiring. Precise
  per-checkpoint allowed files, tests and preconditions live in the TASK's
  「B2-B2 的再切分」section; the dispatch queue is `docs/85` §5.1.

  **`.3` carries production code on purpose (2026-09-20 review correction).**
  #163 currently derives `transport_proxy_name` straight from
  `TransportEndpointRecord.name/host/port` and never compares it against the
  receipt-verified cache, but ADR-037 §2.2 and §4 require matching
  `(name, host, port)` to exactly one materialized proxy inside that
  provider's receipt-verified cache, with 0 or >1 matches failing closed as
  `MIHOMO_TRANSPORT_PROXY_UNRESOLVED`. That check and its test
  (`test_transport_proxy_unresolved_fails_closed`) were originally parked in
  B2-B2c — which no longer works, because under the new order **`.6` wires the
  generation producer before B2-B2c runs**, leaving a window where a live
  producer emits fingerprints for assignments whose transport proxy was never
  verified to exist. Both moved to `.3`, so **`.3` must land before `.6`**.
  The earlier "unresolved ownership" item about this test is closed.

  **Why ADR-037 §5a still holds under that order** (measured on `main`
  `5ae40a6`, not inferred): `allocate_generation()` has **no non-test caller**,
  and `prepare_mihomo_reconciliation()` / `SqlMihomoDesiredSnapshotLoader` do
  **not exist on `main` at all**. Deferring `prepare` and
  `reconcile_mihomo_job()`'s `None`-default wiring to **`.6`** therefore makes
  `.1`–`.5` *structurally incapable* of producing a generation, and by the time
  `.6` merges the manifest has been complete since B2-B2a and the ADR's three
  core invariants have been proven by `.4` and `.5`. There is no window in
  which a generation producer is live while an effective input is missing from
  the manifest. The cost paid on purpose: #163 must **delete ~50 lines of
  already-passing code** (`prepare` + the wiring + its two tests) and re-add
  them verbatim in `.6`.

- **✅ S04-B2-B2 unblocked — `ADR-035` is written and accepted (2026-09-20).**
  **The authority class was never what was open.** ADR-025 §3's taxonomy rules
  deployment-owned validated constants as **`deployment, not DB`**, read from
  **validated `Settings` / repo-owned deployment constants**, and states
  outright that **"Deployment constants are never migrated into the
  database."** A DB table was therefore never an available option; proposing
  one would require superseding ADR-025.

  What was genuinely open sat *inside* that class: three of the seven
  `_validate_deployment_constants()` keys have no default and must be supplied
  by the caller — `external-controller`, `api-secret-ref` and
  `api-secret-revision`; only `mode`, `mixed-port`, `allow-lan` and
  `log-level` default. `api-secret-revision`'s source was already decided (the
  fresh purpose-bound `Secret.revision`), leaving two.

  **ADR-035's answers, so nobody re-derives them:**

  | key | placement |
  |---|---|
  | `external-controller` | **validated `Settings`** — new field `mihomo_external_controller: str = ""`, non-blank check in `validate_runtime_safety()` gated on `forwarder_provider == "mihomo"`; the **format** authority stays `_validate_controller_address()` and is not duplicated into `config.py` |
  | `api-secret-ref` | **repo-owned constant** `MIHOMO_CONTROLLER_SECRET_REF = "mihomo/api-secret"` beside the existing `MIHOMO_CONTROLLER_SECRET_PURPOSE` in `infra/credential_resolver.py` |

  The discriminator ADR-035 sets for any future case: **would two correct
  deployments hold different values? Yes → `Settings`; no → repo constant.**
  `api-secret-ref` is half of the composite `(secret_ref, purpose)` lookup in
  `core/secrets.py:185-190`, and its other half is already a repo constant —
  splitting the pair across two ownership domains would allow a mismatch that
  no configuration check could detect, because `Settings` validation does not
  touch the database.

  **Consequence for the file lists**: B2-B2 gains `backend/app/core/config.py`
  and `backend/tests/unit/test_registry.py`, which **also stay in S04-C's
  list**. The overlap is deliberate and the TASK carries the exact boundary
  table: B2-B2 adds only the field and its non-blank check; S04-C keeps
  `build_registry()` acceptance, lifecycle/factory, and `.env.example` (whose
  exact line is written into the TASK's S04-C section). The field and its
  check must land together because ADR-025 §3 says **validated** `Settings`
  and the value enters the fingerprinted manifest verbatim.

  > **An earlier version of this entry was wrong** and said both ADRs left the
  > constants unlocated, listing a versioned DB table as a candidate. ADR-025
  > §3 had located them; only §4 had been read. Corrected 2026-09-19 after
  > review.

  Two things to carry in rather than re-derive. **Migration numbers moved**:
  S07 took `0024_usage_period_queue`, so B2-B's two migrations are
  `0025_mihomo_projection_generations` (down_revision
  `0024_usage_period_queue`) and `0026_mihomo_transport_materializations`
  (down_revision `0025_mihomo_projection_generations`). **And the TASK's
  allowed-file list gained `backend/tests/integration/test_models.py`**,
  because that file asserts an exact table count (38 today) that two new
  tables necessarily break — found by the preflight scope-closure check, not
  by CI this time.

  `TASK-T16` is explicitly **not** the execution vehicle — its allowed-file
  list does not authorize `backend/app/models/`, `backend/app/infra/`, or
  `infrastructure/alembic/versions/`.
- **✅ S04-B2-B2c added (2026-09-20, ADR-037).** The final chain is decided:
  **client -> Xray (per-user routing) -> Mihomo listener
  (127.0.0.1:{mihomo_listen_port}, one per egress) -> transport node ->
  residential ISP egress -> target.** The rejected alternative had Mihomo
  dialing the residential egress directly, which would have left transport
  materialization and `EgressTransportAssignment` as dead weight.

  Two questions that PR #164 first answered NO are now both **YES**, and the
  reasons they were wrong differ. For the credential, the trace to the Xray
  path was correct but it answered an implementation question in place of an
  architecture one. For the assignment, "the semantics were never defined"
  was a fact used as a conclusion — defining them is what an ADR is for.
  ADR-037 defines both: `ForwarderEgressProxyDTO` +
  `DesiredForwarderState.egress_proxies`, `dialer-proxy` as the only chain
  encoding, ADR-019 §7's precedence reused verbatim for the credential, and
  opaque ref + `Secret.revision` in the manifest with plaintext resolved only
  at the forwarder render boundary.

  **Four consequences worth carrying in.** B2-B2 keeps its original
  five allowed files but **no longer stays free of this** — after the ordering
  fix its loader must populate `egress_proxies`. (After the 2026-09-20
  re-split those five files are the *union* of `.1`…`.6`, not any one PR's
  list, and the first checkpoint that can produce a generation is `.6`, not
  #163.) `providers/base.py` belongs to
  B2-B2a (the DTO) and B2-B2d (the Xray outbound change), not to B2-B2c, which
  is render/finalization only. `dialer-proxy` has **not** been tested
  against the pinned `metacubex/mihomo:v1.19.27` — B2-B2c must confirm it
  during candidate validation and stop rather than substitute an encoding.

  Third, chain B **requires changing Xray**, which the ADR's first draft
  denied in the same breath as choosing chain B.
  `provisioning_state.py:398-403` still builds
  `XrayOutboundDTO(host=endpoint.host, port=endpoint.port, ...)`, so Xray
  dials the residential egress directly and never reaches
  `127.0.0.1:{mihomo_listen_port}` — chain B was unreachable as specified.
  ADR-037 §1a now pins it: under Mihomo mode the outbound targets the
  loopback listener with **no credential on that hop**, and
  `XrayOutboundDTO.credential_secret_ref` becomes `str | None`, which
  supersedes that one clause of ADR-019 §1 while everything else in ADR-019
  is carried over unchanged. That work is its own checkpoint, **B2-B2d**.

  Fourth, and the one with no owner: **nothing writes
  `egress_transport_assignments`**. ADR-037 §2.1b deliberately does not
  invent an allocation policy — capacity, health, region and rebalancing are
  undecided. It is not deferred, though: §2.1a makes "exactly one ACTIVE
  assignment per in-scope egress" a fail-closed gate on activation, so
  Mihomo cannot go to production until a writer exists. Per ADR-036 §2 that
  writer's spec goes to the user, not straight to Claude.

  **The checkpoint order was wrong and is now fixed (ADR-037 §5a).** Because
  this ADR makes assignment and credential effective manifest inputs, and
  #163's `prepare_mihomo_reconciliation()` is the production-wired generation
  producer, the old order (B2-B2 then B2-B2c) would have left a `main` where a
  live producer allocates fingerprints that knowingly omit those inputs. That
  is not cosmetic: `allocate_generation()` reuses the latest generation when
  `manifest_version` and fingerprint both match
  (`mihomo_generation.py:104-109`), so an assignment or credential change
  would not move the fingerprint and the stale generation would be reused --
  and those rows persist.

  The invariant is now stated: **any merged checkpoint able to produce a
  generation must already cover every ADR-037 effective input.** The order is
  B2-B2a ✅ (types + manifest key + `MANIFEST_VERSION` "1"->"2", producing
  nothing) -> B2-B2 -> B2-B2c (render/finalization) -> B2-B2d (Xray handoff)
  -> B2-B3 -> B2-B4 -> writer -> S04-C. The version bump matters on its own:
  leaving it at "1" would make one version string mean two manifest shapes.

  **Updated 2026-09-20 (fourth revision):** `B2-B2` is now `.1` … `.6`, and
  the generation producer moved from #163 to **`.6`**, so the sentence above
  about "#163's `prepare_mihomo_reconciliation()` is the production-wired
  generation producer" describes the *pre-re-split* plan. Under the current
  plan #163 (`.1`) lands the loader but not `prepare` and not the
  `reconcile_mihomo_job()` `None`-default wiring. The invariant itself is
  unchanged and is satisfied more strongly — see the S04-B2-B entry above.

- **S04-C:** **NOT STARTED.** `FORWARDER_PROVIDER=mihomo` remains NOT
  selectable; no deployment authorization exists.

### Two durable blockers S04-B2-B must close (do not re-derive)

1. **Generation-revision blocker.** There is no durable globally monotonic
   revision for one complete Mihomo desired projection.
   `DesiredForwarderState.snapshot_revision` defaults to `1` and is
   caller-supplied; `TransportVersion` and `EgressVersion` are per-`route_group`
   domain-local; `Secret.revision` is per-secret; `updated_at` is not globally
   monotonic. The revision must not be fabricated from timestamps, hashes,
   process counters, cache mtime, runtime state, or combined domain versions.
   Accepted contract: append-only DB-allocated `mihomo_projection_generations`
   (ADR-025 §2, §8.1).

2. **Durable materialization-receipt blocker.** The subscription cache file has
   **no DB anchor**. `SubscriptionTransportProvider.sync_nodes()` writes it
   atomically (`write`→`flush`→`fsync`→`os.replace`), and
   `refresh_provider_inventory()` then commits endpoint inventory, capacity,
   and status — but nothing committed carries `content_hash`, `cache_identity`,
   `source_revision`, or `freshness_deadline`. `TransportMaterialization`
   (which holds exactly that proof tuple) exists only as an in-memory dataclass
   in `providers/forwarder/mihomo_projection.py`. Consequence: a loader that
   hashed whatever file is on disk would let a modified/torn/out-of-band cache
   authenticate itself. Accepted contract: a receipt produced **only** by a
   successful sync, committed in the same transaction as that sync's inventory,
   after the atomic file write; new file + old receipt ⇒ hash mismatch ⇒
   **fail closed**, never auto-trusted and never re-minted from the file
   (ADR-025 §3a, §8.2).

## 6. Current blockers and carry-over

For any future S03 maintenance, independently verify the exact mutable GitHub
state and confirm ADR-024 remains the accepted contract. The implementation produces
immutable provider-neutral descriptors from current DB records, lets a
process-lifetime registry resolver lazily create and retain providers, resolves
purpose-bound secrets in a short transaction before network I/O, isolates cache
identity, and fails closed on descriptor or secret-revision drift. New records
may be created lazily; unchanged descriptors reuse their instance; incompatible
changes require controlled replacement or restart. No Mihomo activation,
deployment, or real provider request is included.

## 7. S03-A delivery reference

S03-A base: `6162fefaa63639bf896daad8349dbe883baaa298`. This is the S03-A base,
not a permanent claim about current `main`; current GitHub `main` must always
be re-verified before starting or resuming work. The S03-A PR and its current
head/review/check state are the mutable source of delivery truth.

## 8. Open defects carried forward (audit 2026-09-18)

These were found by a repository-wide audit, reproduced against the current
working tree, and are **not** fixed by any merged PR. Each is recorded here so a
later session does not have to rediscover it.

1. ~~**Compensation-failure paths in `domain/provisioning.py` mask the original
   error and skip terminal bookkeeping.**~~ **FIXED 2026-09-18** by ADR-026 +
   `TASK-S05-compensation-failure-contract.md`. When a compensating action
   itself raised — the `APPLY_FORWARDER` restore, the `APPLY_GATEWAY`
   `disable_user()`, or the same call in
   `fail_apply_gateway_lock_acquisition()` — the compensation exception replaced
   the real failure, `state.rollback_database()` / `_failed()` never ran, and
   the run stayed at `RUNNING` forever with an uncompensated external side
   effect reported as a clean `FAILED`. All three now end `PENDING_MANUAL` with
   a typed reason. 8 new tests; 4 of them verified failing against the pre-fix
   code.

2. ~~**`ops/**` and `infrastructure/**` Python is never linted.**~~
   **FIXED 2026-09-18.** `make lint` now runs
   `ruff check backend ops infrastructure scripts`.
   `infrastructure/alembic/versions/` is excluded via `pyproject.toml`'s
   `extend-exclude`, because 铁律 7 freezes existing revisions — linting them
   can only ever produce findings that must not be fixed. **Still open:** `mypy`
   coverage stops at `backend/`; `ops/**` has no type checking. That is a
   larger change (the ops scripts are not annotated) and is not scheduled.

3. ~~**`ops/backup/` is empty**。~~ **TASK-S07 实现于 2026-09-18。**
   `backup.sh` 现在生成唯一命名的 GPG 加密 MySQL 备份，保存 SHA-256 证据并
   上传既有 R2；`--verify` 只读核对本地和 R2 证据；`restore.sh` 要求明确的
   空目标数据库；`install-cron.sh` 通过 root-owned wrapper 解决 deploy 用户
   与 root:root 0600 backup 配置的权限冲突。`check_13_backup` 已接入真实
   验证并保持 fail-closed。本任务不执行真实生产备份、恢复、migration 或
   production deployment workflow。

4. ~~**Plan-tier configuration is inconsistent.**~~ **MOSTLY FIXED 2026-09-18**
   by `TASK-S06-plan-catalogue.md`. The product owner settled the catalogue:
   four tiers on a 30-day cycle, priced in **CNY** — 50GB/¥30, 100GB/¥50,
   200GB/¥80, 500GB/¥120. `backend/app/catalog.py` is now the single source;
   `ADMIN_CAPACITY_PLAN_CODES` derives from it, `GET /plans` filters to it, and
   both frontend pages dropped their duplicated lists. `PLAN_1000GB` and
   `PLAN_300GB` are withdrawn and their orphaned config removed.

   **Still open, and important:** the catalogue does **not** reach the
   database. `deploy/lib/60_seed.sh` deliberately refuses implicit seed data
   (`SEED_COMMAND must be explicitly configured`), and nothing else in this
   repository creates `Plan` rows — the `plans` table is populated entirely
   out-of-band. So the agreed prices are declared but not yet in effect
   anywhere. Wiring the catalogue into a seed path needs the product owner's
   sign-off first, because it means this repository would start writing
   business data during deployment.

   **Add-on pricing: answered 2026-09-18.** There is exactly one price list —
   the four tiers. Buying more traffic means buying another order from that
   same list; there is no add-on SKU and no add-on price, so
   `addon_20gb_price` / `addon_50gb_price` / `addon_100gb_price` were deleted
   along with their `.env.example` entries. The billing model this implies
   (orders queue on one subscription; a period ends on quota exhaustion **or**
   expiry, whichever comes first; the subscription link never changes) is
   **ADR-027**, implemented by **`TASK-S07-order-queue-billing.md`**.

   ~~Note that `RENEWAL`, `UPGRADE` and `ADDON` are all still unimplemented
   stubs in `api/admin.py:159-169`, and there is **no usage-period rollover
   code at all** — `apply_expiry_policy()`'s own docstring says it does not
   touch period boundaries.~~ **Both sentences are stale — corrected
   2026-09-21 (`REVIEW-089` m1).** TASK-S07 (PR #152) closed them and
   nothing went back to fix this paragraph: `api/admin.py:161-168` now
   routes all three to `enqueue_subscription()`, and
   `workers/accounting_sync.py:181` `apply_expiry_policy()` is real — its
   docstring now reads "Close an exhausted/expired period and activate the
   FIFO queued period" and it selects the head of the
   `UsagePeriodStatus.QUEUED` queue. Same "changed one place, left the
   current-state text stale elsewhere" defect class this repo keeps hitting.

   **Severity of the seeding gap was raised on 2026-09-21** — see
   `REVIEW-089` M1 and `docs/85` §5.6 item 3. It is not "prices declared but
   not in effect"; `GET /plans` reads the database, so on a fresh deployment
   the customer sees **an empty catalogue and cannot order at all**.

5. **`.env.example` omitted `XRAY_CONFIG_PATH`, `XRAY_BACKUP_DIR`, and
   `XRAY_LOG_LEVEL`** — real `Settings` fields consumed by the now-wired
   `xray_file` gateway. **Fixed 2026-09-18**: added with their code defaults
   rather than blank, because an empty `XRAY_LOG_LEVEL` raises at startup
   instead of falling back (verified). Note this trap applies to the whole
   "Legacy migration inventory" block in `.env.example`: those names are listed
   blank by convention, and any of them that `Settings` validates will hard-fail
   if copied into a real `.env` as-is.

6a. **`domain/provisioning.py`'s forwarder contract is incompatible with the
   Mihomo implementation (C1).** Added 2026-09-21 by `REVIEW-089`; **no
   owner**. Two independent mismatches — fixing either leaves the other:
   (a) `provisioning_state.py:193-198` fills only the legacy
   `listeners: Mapping[str, str]`, so rendering it through Mihomo raises
   `MIHOMO_CONTROLLER_SECRET_REF_INVALID` (measured — **not**
   `MIHOMO_LEGACY_LISTENERS_UNSUPPORTED`, because
   `_validate_deployment_constants()` runs first), and the ADR-026
   compensation path uses the same shape so it raises identically;
   (b) the `ForwarderProvider` Protocol and `MockForwarderProvider` are
   two-step `render → apply`, while `MihomoForwarderProvider` is three-step
   `render → finalize(resolver) → apply` and its `apply()` requires a
   `MihomoCandidateConfig`. `mypy --strict` cannot see (b): `ProjectionTemplate`
   subclasses `CandidateConfig`. **Consequence: the day S04-C allows
   `FORWARDER_PROVIDER=mihomo`, every provisioning run fails at
   `APPLY_FORWARDER` and lands `PENDING_MANUAL` with the external tenant
   already created.** Fail-closed, but provisioning stops. Changing the
   protocol touches `domain/**` and `providers/base.py`, so 铁律 5 requires an
   ADR first — routed to the user per ADR-036 §2.

6b. **Mihomo reconciliation has neither a producer nor a runner (C2).**
   Added 2026-09-21 by `REVIEW-089`; **no owner**. `reconcile_mihomo_job()`
   has **zero non-test callers** and `workers/` mentions mihomo nowhere, while
   the Xray path has all three parts (producer `services.py:223` /
   `accounting_sync.py:475`, runner `main.py:22-53` lifespan loop, inline
   `reconcile_gateway_job_in_session()`). No checkpoint from B2-B1 through
   S04-C claims any of them. S04-C's allowed files *do* include `main.py` and
   `workers/scheduler.py`, but its goals and acceptance criteria mention
   neither — **a file being in the list is not the same as the work being in
   the plan.** Which business events should enqueue a reconcile is a design
   choice that changes fingerprint frequency and lock contention, so it is
   routed to the user, not invented by an executor.

6. **Frontend components are written one-statement-per-file-line.** Entire React
   components sit on single 1000+ character lines
   (`frontend/app/(customer)/**/page.tsx`, `(admin)/admin/components.tsx`).
   `eslint`/`tsc` pass, so nothing is broken — but the code is effectively
   undiffable and unreviewable, which conflicts with `AGENTS.md`'s requirement
   that the PR timeline be a reconstructable record. Type definitions are also
   duplicated per page rather than shared, which is the concrete form of
   `docs/84-*`'s "`frontend/lib/api.ts` is not a real typed client" gap.
   **Re-confirmed 2026-09-21** (`REVIEW-089` m2): eight customer pages still
   carry 1–2 lines over 400 characters each. Needs its own TASK; out of S04's
   scope.

7. **`mypy` run inside the remote Claude Code container is all environment
   noise.** Added 2026-09-21 (`REVIEW-089` m3), for any session that follows
   `CLAUDE.md`'s "run `mypy backend/app backend/tests` before pushing". In that
   container `mypy` lives in its own pipx venv (`/root/.local/bin/mypy`) and
   cannot see the project's site-packages, so it reports **352 errors** — 189
   `import-not-found`, 101 `untyped-decorator`, and the rest cascading from
   them. CI's `lint` job installs `-e ".[dev]"` first, so a green CI is
   genuinely green. **Do not "fix" those 352.** To run mypy meaningfully
   locally, run it from an interpreter that has the project dependencies.

## 9. Spec-vs-reality drift in `ARCHITECTURE.md`

`ARCHITECTURE.md` is the original build spec and has **not** been kept in sync.
Known divergences (do not treat these as missing work without checking intent):

| Spec says | Reality |
|---|---|
| `backend/app/core/rate_limit.py` | Does not exist; rate limiting lives inside `providers/egress/webshare.py` |
| `workers/usage_sync.py`, `workers/baseline.py` | Renamed to `accounting_sync.py`, `transport_sync.py`, `drift_check.py` |
| `ops/backup/{backup,restore,install-cron}.sh` | Implemented by TASK-S09 (PR #132; the TASK file was created as S07, collided, and was renamed); `ops/status.py` remains absent |
| `egress/manual`, `payment/manual`, `notify/telegram`, `email/{resend,smtp}`, `captcha/turnstile`, `storage/{r2,s3,local}` | None exist; only mock/noop |
| `providers/base.py` Protocol signatures | Materially evolved (e.g. `GatewayProvider.render()` now requires a `CredentialResolver`; `AccountUserDTO.routing_principal` added per ADR-016) |
| `(docs)/guides/{windows,macos,ios,android}` | Route directories exist but contain only `.gitkeep` |
| `frontend/lib/api.ts` typed client | One line: a base-URL constant |
| Task sequence T1–T8 | Superseded; see section 4b |

`ARCHITECTURE.md`'s §1 design goals, §2 layering rules, the three 铁律, §6 guard
requirements, and §8 deployment acceptance list **do** still hold and are
actively enforced by tests — the drift is in the file tree and task sequence,
not the invariants.

## 10. Architecture audits (now user-triggered only)

> **2026-09-19 (ADR-034): there is no scheduled audit, and no event that
> forces one.** Periodic supervision went in ADR-033; the three remaining
> mandatory triggers — before wiring a real external system, before a
> production deploy, after a workstream completes — went in ADR-034. An audit
> happens when, and only when, the user asks for one. Nobody is expected to
> notice that one is due.
>
> **The table below is kept, and its purpose changed.** It is no longer
> evidence that a cron ran; it is **where the next audit finds its starting
> point**. The user's plan is to accumulate a large batch of implementation
> work before calling for a review, so the further apart the rows, the more
> this matters.
>
> **2026-09-20 (ADR-036) makes this table matter more, not less.** ChatGPT now
> writes the specs it reviews against, so the user-triggered stage review is
> the only place a spec-level defect gets an independent reader. Verified the
> same day: nothing wakes Claude on a schedule — zero Routines, `claude.yml`
> fires only on the user's `@claude` comment, and `pipeline-health.yml` holds
> no LLM and no Claude credential (ADR-036 §5, which also forbids adding one).

Every audit appends a row: **date, the `main` SHA audited, what was actually
run, what was found, whether the breaker was pulled.** An audit that only
reads code without running `ruff` / `mypy` / the test suite does not count —
that requirement comes from ADR-029 §6's behavioral contract, which ADR-034
kept.

**How to start a user-triggered audit** (ADR-034 §4.3): read the last row
below for the previously audited SHA, then `git log <that SHA>..main`, then
the digests on the `pipeline-health` branch covering that span — *then* start
reading code. Reconstructing the span from scratch is the expensive part, and
this table is what makes it cheap.

| Date (UTC) | `main` SHA | Ran | Findings | Breaker pulled? |
|---|---|---|---|---|
| — | — | — | No audit has appended a row yet. | — |

Two things a fresh session should know rather than re-derive:

1. **The audit is the only independent check after `main`.** Under ADR-029 the
   reviewer of a PR is the same agent that specified it, so a whole class of
   defect — the cross-PR interaction that appears in no single diff — can only
   be caught here. The live example is recorded in §8.
2. **A Critical finding forces re-evaluation of ADR-029 itself**, not just a
   breaker pull. ADR-029's "重新评估条件" says so explicitly: repeated
   Criticals mean read-only review plus after-the-fact detection is not an
   adequate substitute for the manual gate it replaced.

**Confirmed broken, 2026-09-19 — and superseded.** The claude.ai Routine that
was supposed to fire this audit (`43 */6 * * *`) **did fire** at 00:43:05
(session `cse_01CvtKzoF3Tp3mauatU85GQP`) and produced nothing: 45 minutes
later its `last_run.status` was still `PENDING`, no `claude/audit-*` branch
existed, and no PR was opened. It should not have skipped — its own prompt
treats an absent audit record as "never audited" and requires the full pass —
so this was a hang, not a quiet no-op. Cause: the Routine was created from a
session with no connector grants, so the fired session had no
`mcp__github__*` tools and no repository source.

**ADR-030 §3 replaces the mechanism rather than patching that one Routine**, and
TASK-S10 implements the replacement (`.github/workflows/pipeline-health.yml`
plus `scripts/pipeline_health.py`, writing `docs/87-pipeline-health.json`).
The lesson is architectural: *any carrier of scheduled automation whose
credentials and repository access are not version-controlled and inspectable
in the repo cannot be trusted to carry a safety mechanism.* Supervision moves
to `.github/workflows/` — a machine-generated health digest
(`TASK-S10-pipeline-health-digest.md`, no LLM, `GITHUB_TOKEN` only) that
Claude reads on a low cadence. **Correction to an earlier note here:** it previously said there was "no
evidence `CLAUDE_CODE_OAUTH_TOKEN` is configured and working". That was too
strong. Branches such as `claude/issue-97-xray-gateway-registry-wiring` exist,
and `claude-code-action` only creates those after actually running and
committing — so it has worked. The accurate statement is that `claude.yml` has
247 runs whose recent ones all concluded `skipped`, because its `if` requires
`@claude` in a comment and none was posted: **not triggered, not broken.**
The digest still avoids that credential, for the reasons in ADR-030 §3 —
availability of the supervision input must not depend on a rotatable secret
that cannot be inspected from the repository, and polling a model every two
hours to conclude "no change" is what the token budget forbids.
