# Project Continuity / Handoff

## 1. Authority and resume contract

Authority is **ADR > `AGENTS.md` > REVIEW**. `CLAUDE.md` supplies the daily
procedure and reading order; it is not above an accepted ADR or `AGENTS.md`.
For a fresh session, re-read `CLAUDE.md`, `AGENTS.md`, this continuity index,
the relevant ADRs, TASK, README, and current PR evidence. Before resuming,
re-read mutable GitHub state: current `main`, branch head, changed files, PR
state, reviews, and checks. Do not rely on an older chat or SHA snapshot.

## 2. Roles and workflow

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
materialization is restricted input, not Mihomo activation.

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

**Known process gap (open):** no S-series work has a `docs/82-tasks/TASK-*.md`
file, although `AGENTS.md`「协作角色与职责」names GitHub Issue or `TASK-*.md` as
the *sole* record carrier for requirements and acceptance criteria. Eight merged
PRs (#120–#127) carry S02→S04-B1 with their 目标/约束/验收标准 existing only in PR
bodies and in this section's prose. See `TASK-S04-mihomo-activation.md` for the
first S-series TASK file closing this gap forward; the already-merged S02/S03
scope is not retro-filed.

**ADR numbering:** ADR-010 does not exist and is not referenced anywhere. The
sequence runs 001–009, 011–024. This is a numbering hole, not a missing document
— do not go looking for it.

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
- **S04-B:** ACTIVE. B1 is merged; **B2 has not started** and has no PR, Issue, or
  TASK file yet — see `TASK-S04-mihomo-activation.md`.
- **S04-B1:** COMPLETE / merged as PR #127, merge commit
  `c494ade` (verify against current `main`). It adds only
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

1. **Compensation-failure paths in `domain/provisioning.py` mask the original
   error and skip terminal bookkeeping.** When a compensating action itself
   raises — the `APPLY_FORWARDER` restore-previous-config re-apply, the
   `APPLY_GATEWAY` `disable_user()`, or the same call in
   `fail_apply_gateway_lock_acquisition()` — the compensation exception replaces
   the real failure and `state.rollback_database()` / `_failed()` never run. The
   `CREATE_ACCOUNTING_USER` step already handles exactly this case correctly
   (failed disable → `PENDING_MANUAL`, per ADR-018); steps 5 and 7 do not.
   Reproduced; no test covers it. Owning document: **ADR-025** + `TASK-S05-compensation-failure-contract.md`.

2. **`ops/**` and `infrastructure/**` Python is never linted or type-checked.**
   `make lint` runs `ruff check backend` and `mypy backend/app backend/tests`
   only. `ops/gateway/safe_reload.py` (the nine-step Xray reload),
   `ops/reconciliation/gateway_principals.py`, and
   `ops/forwarder/collect_mihomo_usage.py` are production-critical and sit
   outside every static gate. Audited state: no `F`/`B`/`SIM` findings today, so
   this is an **unguarded surface**, not a present bug.

3. **`ops/backup/` is empty**, so `AGENTS.md`'s "`alembic upgrade` 前提：先成功跑
   一次加密备份" precondition is unsatisfiable, `Makefile`'s `backup`/`restore`
   targets point at non-existent scripts, and `deploy/lib/80_schedule.sh`
   installs a cron for a script that does not exist. `deploy/lib/70_verify.sh`
   `check_13_backup` does fail closed on this, so a real deploy cannot silently
   pass — but it also cannot pass at all until this lands.

4. **Plan-tier configuration is inconsistent in two separate ways.**
   - `.env.example` declares `PLAN_300GB_PRICE`, which `Settings` does not read
     at all — a dead variable.
   - `Settings.plan_1000gb_price` and `PLAN_1000GB_PRICE` both exist, but the
     plan **code** `PLAN_1000GB` appears in no sellable list: neither
     `admin.py:ADMIN_CAPACITY_PLAN_CODES` nor either frontend page offers it.
     Either a 1000GB tier was intended and never wired, or the price knob is a
     leftover. This is a product decision, not a mechanical fix.
   - The sellable plan-code list is hardcoded in **three** places
     (`backend/app/api/admin.py`, `frontend/app/(customer)/plans/page.tsx`,
     `frontend/app/(customer)/orders/new/page.tsx`) with no shared source.

5. **`.env.example` omitted `XRAY_CONFIG_PATH`, `XRAY_BACKUP_DIR`, and
   `XRAY_LOG_LEVEL`** — real `Settings` fields consumed by the now-wired
   `xray_file` gateway. **Fixed 2026-09-18**: added with their code defaults
   rather than blank, because an empty `XRAY_LOG_LEVEL` raises at startup
   instead of falling back (verified). Note this trap applies to the whole
   "Legacy migration inventory" block in `.env.example`: those names are listed
   blank by convention, and any of them that `Settings` validates will hard-fail
   if copied into a real `.env` as-is.

6. **Frontend components are written one-statement-per-file-line.** Entire React
   components sit on single 1000+ character lines
   (`frontend/app/(customer)/**/page.tsx`, `(admin)/admin/components.tsx`).
   `eslint`/`tsc` pass, so nothing is broken — but the code is effectively
   undiffable and unreviewable, which conflicts with `AGENTS.md`'s requirement
   that the PR timeline be a reconstructable record. Type definitions are also
   duplicated per page rather than shared, which is the concrete form of
   `docs/84-*`'s "`frontend/lib/api.ts` is not a real typed client" gap.

## 9. Spec-vs-reality drift in `ARCHITECTURE.md`

`ARCHITECTURE.md` is the original build spec and has **not** been kept in sync.
Known divergences (do not treat these as missing work without checking intent):

| Spec says | Reality |
|---|---|
| `backend/app/core/rate_limit.py` | Does not exist; rate limiting lives inside `providers/egress/webshare.py` |
| `workers/usage_sync.py`, `workers/baseline.py` | Renamed to `accounting_sync.py`, `transport_sync.py`, `drift_check.py` |
| `ops/backup/{backup,restore,install-cron}.sh`, `ops/status.py` | None exist |
| `egress/manual`, `payment/manual`, `notify/telegram`, `email/{resend,smtp}`, `captcha/turnstile`, `storage/{r2,s3,local}` | None exist; only mock/noop |
| `providers/base.py` Protocol signatures | Materially evolved (e.g. `GatewayProvider.render()` now requires a `CredentialResolver`; `AccountUserDTO.routing_principal` added per ADR-016) |
| `(docs)/guides/{windows,macos,ios,android}` | Route directories exist but contain only `.gitkeep` |
| `frontend/lib/api.ts` typed client | One line: a base-URL constant |
| Task sequence T1–T8 | Superseded; see section 4b |

`ARCHITECTURE.md`'s §1 design goals, §2 layering rules, the three 铁律, §6 guard
requirements, and §8 deployment acceptance list **do** still hold and are
actively enforced by tests — the drift is in the file tree and task sequence,
not the invariants.
