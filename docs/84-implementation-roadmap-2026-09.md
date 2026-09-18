# Implementation Roadmap Audit — 2026-09-13

> ## ⚠️ 部分结论已过期（2026-09-18 审计更正）
>
> 本文件自身的「Maintenance note」要求：发现过期时**优先重跑审计**，并加更正
> 而不是就地改写，以保留"当时我们以为什么是真的"的轨迹。以下是就地更正，
> 不改动原文正文：
>
> | 原文结论 | 现状（2026-09-18 直接核对代码） |
> |---|---|
> | Provider registry「Stub-only wiring, real adapters unwired」；`build_registry()` 对任何非 mock/noop 一律抛错 | **已过期。** `ACCOUNTING_PROVIDER=marzban`、`GATEWAY_PROVIDER=xray_file`、`TRANSPORT_PROVIDER_MODE=subscription` 均已接通（PR #111/#114/#116/#125）。仍被挡住的只有 egress、forwarder、payment、notify、email、captcha、storage |
> | 「TASK-T16 Phase 2C 尚未开始」 | **已过期。** Phase 2C1–2C3D 已全部合并；此后工作已切换到 S 系列（见 `83-project-continuity.md` §4b） |
> | `webshare.py` 的 `list_endpoints()` 丢弃响应返回 `[]` | **已过期。** 现已真实解析分页结果 |
> | `webshare.py` 的 `capacity()` 返回全零 `CapacityDTO` | **已过期，但结论方向不变。** 现改为 fail-closed 抛 `WebshareContractError`——比返回全零更安全，但也意味着 Webshare 即使接线也会在开通第 1 步直接失败 |
> | `get_tenant_usage()` / `get_credentials()` 抛 `NotImplementedError` | **部分过期。** `get_credentials()` 已实现；`get_tenant_usage()` 仍 fail-closed（单位未经实测确认） |
>
> **仍然成立、且仍是最高优先级的结论：** §2 的 gap 1（`ops/backup/` 为空）、
> gap 2（`ops/status.py` 缺失）、gap 3（客户端配置指南只有空目录）、
> gap 4（`frontend/lib/api.ts` 不是真正的客户端）、gap 5（`core/rate_limit.py`
> 不存在，限速在 webshare provider 内）——2026-09-18 逐项复核，全部未变。
> §5 的排序建议（备份工具先于任何真实外部写入接线）同样仍然成立。

**Status:** snapshot as of Issue #81, reconciled against `main` at the SHA
recorded in `docs/83-project-continuity.md` plus direct inspection of the
current working tree during this audit. This document is a **point-in-time
audit and roadmap recommendation**, not a living handoff index — that role
belongs to `docs/83-project-continuity.md`, which this document
supplements rather than replaces. Where the two disagree, re-verify against
current `main`; the continuity document is updated more frequently and
should be treated as the fresher source for collaboration/automation
mechanics, while this document is the fresher source for **product**
implementation status as of this date.

## 0. Purpose and scope

Issue #81 asked for a repository-wide implementation audit against the
project's original product goal (subscription distribution platform:
provider-based architecture, provisioning flow, gateway integration,
subscription generation, production readiness) — explicitly distinct from
the Claude Code Issue→PR→Review→Merge engineering mechanism, which is a
means to that end, not the end itself.

This is a **docs-only PR**. No production business logic, ADR, TASK file,
or `AGENTS.md` content is modified. Where this audit's findings imply code
changes, they are listed as recommended follow-up Issues/TASKs in section
5, not implemented here, per the Issue's own constraints.

## 1. Current implementation status (by area)

| Area | Verdict | Evidence |
|---|---|---|
| Backend app skeleton (`main.py`, `core/*`) | **Complete** | `config.py`, `database.py`, `secrets.py`, `logging.py`, `security.py` all present and production-shaped; no separate `core/rate_limit.py` (spec listed one — see 2.6) |
| API entry points (`api/public.py`, `admin.py`, `subscription.py`, `health.py`) | **Complete** | All spec-listed customer/admin/health flows present: register/login/logout/change-password, plans, orders + payment-notice, admin order-confirm/customers/egress/capacity/metrics/accounting-health/sync-usage, `/health` + `/health/egress` |
| Domain layer (`domain/*`) | **Complete, functional** | `provisioning.py` (725 lines) implements the full 9-step saga with real compensation logic (`_compensate_created_accounting_user`, DB rollback, `PendingManualReason`), not a stub. `ordering.py`, `subscription_render.py` substantial; `capacity.py`/`quota.py` thin by design (simple calculations) |
| Provider registry (`providers/registry.py`) | **Stub-only wiring, real adapters unwired** | `build_registry()` raises `ProviderConfigurationError` for any non-mock/noop provider setting — confirmed directly in code, matching `docs/83-project-continuity.md` section 6. This is the single largest gap between "looks done" and "is live" in the repo |
| Real provider adapters (webshare, xray_file, mihomo, marzban, transport/subscription) | **Partial, per-provider completeness varies, and none is registry-reachable** | `webshare.py` (344 lines), `xray_file.py` (418), `mihomo.py` (155), `marzban.py` (734, largest adapter), `transport/subscription.py` (257) all exist with their own unit/guard tests, but none is selectable through `build_registry()` under any env var combination. Completeness is uneven, not uniform: `webshare.py` specifically still has real gaps beyond registry wiring — `list_endpoints()` discards the API response and returns `[]`, `capacity()` returns an all-zero `CapacityDTO`, and `get_tenant_usage()`/`get_credentials()` raise `NotImplementedError("response mapping arrives with T5 migration")`. Do not read "adapter exists" as "adapter is functionally complete" for Webshare; the other adapters were not individually re-audited for the same gap pattern in this pass |
| Database models & migrations | **Complete, actively evolving** | All spec model modules present (`identity`, `billing`, `subscription`, `egress`, `gateway`, `ops`); 22 Alembic migrations (`0001`–`0022`) covering core → accounting sync → transport → routing/egress → subscription engine → billing → ops → capacity → dynamic traffic policy → dual route versions → payment → security baseline → fixed-cycle billing → dedicated egress → transport placeholders → gateway route bindings → webshare binding credentials → egress metadata |
| Workers/background jobs | **Complete, functional (renamed from spec)** | `scheduler.py`, `drift_check.py`, `accounting_sync.py`, `transport_sync.py` — spec's `usage_sync.py`/`baseline.py` naming has diverged but equivalent coverage exists (see 2.7); tested in `test_scheduler.py`, `test_scheduler_transport_failure.py`, `test_drift_check.py` |
| Guard tests (`tests/guards/*`) | **Complete, exceeds spec** | All 5 spec-required guard tests present (`test_webshare_guard.py`, `test_safe_reload.py`, `test_capacity_guard.py`, `test_routing_invariants.py`, `test_secret_leak.py`) plus an extra `test_webshare_procurement.py` |
| Frontend pages | **Partial** | All customer/admin route scaffolding exists (`(customer)/{login,register,portal,plans,orders,subscriptions}`, `(admin)/admin/{orders,customers,egress,capacity}`). `(docs)/guides/{windows,macos,ios,android}` directories exist but contain **only `.gitkeep`** — no actual client setup guide content, despite subscription generation being a core product feature these guides are meant to explain to end users |
| Frontend API client (`frontend/lib/api.ts`) | **Stub-only** | 1 line — a bare `NEXT_PUBLIC_API_BASE_URL` constant, not a typed client. Pages call `fetch()` ad hoc per-file (`login/page.tsx`, `register/page.tsx`, `admin/components.tsx`) rather than through a shared typed layer. Functionally connected to the real backend, but thinner than "API integration" implies and harder to keep in sync with backend contract changes |
| Deployment scripts (`deploy/bootstrap.sh`, `deploy/lib/00–80`) | **Complete, verified fail-closed** | Full 00-preflight through 80-schedule sequence present, plus `rollback.sh` and `inventory.example.yml`. `.github/workflows/deploy-*.yml` defaults `DRY_RUN=true` with an explicit assertion gating any real action; every step in dry-run mode only echoes intended actions |
| `ops/` tooling | **Partial** | `ops/gateway/{render_xray_routes.py,safe_reload.py}`, `ops/forwarder/{render_mihomo_config.py,collect_mihomo_usage.py}`, `ops/probe/egress_chain_probe.py`, `ops/procurement/{cli.py,config/procurement.yaml}` all present and real. `ops/backup/` is **empty except `.gitkeep`** — no actual backup/restore script exists yet. `docs/30-backup-restore.md` currently only states "Backup, restore, and PowerShell GPG instructions are delivered in T6/T7" — it does **not** itself document a procedure, contrary to this document's earlier wording. `AGENTS.md`'s "允许自主执行" (allowed-without-approval) list does condition running `alembic upgrade` on "先成功跑一次加密备份" (a successful encrypted backup having run first) — that specific precondition is real and currently unsatisfiable since the backup script doesn't exist. `ops/status.py` (spec'd operational overview + `OVER_PROXY_LIMIT` alert) is **absent entirely** |
| Tests overall | **Complete breadth** | 17 unit test files, 4 integration test files, 6 guard test files (27 total). No `xfail`/`skip` markers found anywhere under `backend/tests/`. Integration tests **fail-closed rather than skip** without `TEST_DATABASE_URL` (`test_db_adapters.py`: `pytest.fail("TEST_DATABASE_URL is required; DB adapter tests are never skipped")`) — stronger guarantee than a soft skip |
| `docs/70-external-facts.md` | **Sparse, confirms TASK-T13 is correctly blocked** | Only ~1 recorded "已实测" (tested) fact. Consistent with TASK-T13 (split-routing) being deliberately gated on real-world evidence per ADR-012, not neglected |

## 2. Notable gaps worth calling out explicitly

These are observations from this audit that are **not** already tracked by
an open Issue or TASK file as of this document's writing (verify against
current GitHub state before filing — see section 5):

1. **`ops/backup/` is an empty stub.** `docs/30-backup-restore.md` does
   not itself document a backup/restore procedure yet — it only states
   that one is "delivered in T6/T7" — so this is a genuinely missing
   capability, not a doc/code mismatch. Separately, `AGENTS.md`'s "允许
   自主执行" list conditions running `alembic upgrade` on "先成功跑一次
   加密备份" (a successful encrypted backup having run first); with no
   backup script, that specific precondition can never be satisfied in
   practice. Note this is a confirmed gap in the backup tooling itself,
   not a proven blocker on TASK-T16 Phase 2C specifically — see section 4
   for that distinction.
2. **`ops/status.py` (operational overview + capacity/quota alerting)
   does not exist**, despite being spec'd and despite `admin/capacity`
   and `admin/metrics` API endpoints already existing to surface similar
   data — there may be product-level operational visibility this repo
   still lacks outside the admin UI itself.
3. **Client setup guides (`(docs)/guides/{windows,macos,ios,android}`)
   are empty route stubs.** Subscription generation (Clash/Base64
   rendering — `domain/subscription_render.py`) is implemented and
   tested, but the customer-facing instructions for actually using a
   generated subscription link on each platform do not exist yet. This
   is a real gap between "backend capability done" and "customer can
   self-serve," which matters for a subscription distribution product
   specifically.
4. **`frontend/lib/api.ts` is not a real typed client.** This isn't
   blocking today (pages fetch directly and work), but it means backend
   API-contract changes have no single frontend seam to check for
   breakage — `CLAUDE.md`'s own "Implementation" section already requires
   checking both sides of an API contract change; a typed client would
   make that check mechanical instead of manual per-file.
5. **`core/rate_limit.py` named in `ARCHITECTURE.md`'s spec tree was not
   found as a standalone module** under `backend/app/core/` in this
   audit pass. Rate limiting may be implemented elsewhere (e.g. inline in
   `webshare.py`'s documented 240/min and 60/min limits, which the T16
   audit trail already confirms exist at the provider-client level) — this
   needs a direct follow-up check before concluding it's missing outright,
   since the guard-test-level rate-limit behavior for Webshare specifically
   is already covered by `test_webshare_guard.py`.

Items 1–3 are the most product-relevant: they sit on the path to
"production readiness" and "customer can complete the whole subscription
lifecycle unattended," which is the actual product goal this audit was
asked to check against — not just whether individual layers exist.

## 3. What the previous engineering cycle actually built

For calibration: the majority of recent merged work (Issues #66–#79,
TASK-T17 through T22) was the Claude Code GitHub Action automation
itself — concurrency/actor-scoping fixes, tool-permission fixes, PR
creation/dispatch reliability, App-token identity design. This was
necessary (a broken automation loop blocks all further product work
regardless of code quality) but is **not** product feature work. The
Issue that triggered this audit is correct that this distinction had
become blurred and needed restating explicitly: the automation is the
delivery mechanism, not the roadmap.

The last genuine product-code milestone before this automation detour was
TASK-T16 Phase 2B (provider adapter completion and lifecycle/`close()`
correctness for Marzban accounting and transport providers) — Phase 2C
(actually wiring those adapters into `build_registry()` for live
selection) has not started. This matches `docs/83-project-continuity.md`
section 7's "Actual next technical frontier" conclusion, and this audit's
direct code inspection confirms it still holds.

## 4. Dependencies and risks

- **TASK-T16 Phase 2C is high-risk by its own TASK file's classification**
  (real external-system side effects, trust precondition for all business
  logic). Its own constraints already specify a safe ordering: read-only
  capabilities first (`list_endpoints`, `capacity`, `get_usage`), then
  write-capable ones (`create_tenant`, `replace_endpoint`) gated behind
  guard-test-equivalent coverage. Any follow-up Issue for Phase 2C should
  preserve that ordering rather than wiring all providers at once.
- **Backup tooling (gap 2.1) is a recommended risk-sequencing choice
  ahead of T16 Phase 2C, not a proven hard dependency.** TASK-T16 Phase
  2C's own file does not name an Alembic migration/recovery step as part
  of its scope, and `AGENTS.md`'s alembic-upgrade-requires-backup
  precondition is specifically about running `alembic upgrade`, which is
  not itself part of Phase 2C's stated work. The recommendation to land
  backup/restore tooling first is a risk-control judgment call — Phase
  2C introduces real external-system side effects with no verified
  recovery path if something goes wrong — not an assertion that Phase 2C
  is technically blocked from starting without it. Treat section 5's
  ordering as recommended sequencing, and revisit it if a concrete Phase
  2C path is later found to require an Alembic upgrade or a recovery
  operation.
- **TASK-T13 (split-routing) remains correctly blocked** on
  `docs/70-external-facts.md` evidence per ADR-012's own phasing; nothing
  in this audit changes that — it should stay blocked, not be
  reprioritized upward just because other work is ready to start.
- **Issue #87 / TASK-T23 supersedes the custom App-token plan in
  TASK-T22.** PR #88 restores the minimal standard Claude Code Action
  path. After merge, validate it with a new owner-authored `@claude`
  Issue comment before scaling Issue-driven product work.

## 5. Recommended next TASK/Issue sequence

In priority order, as separate follow-up Issues per this audit Issue's own
constraint (implementation changes go into new Issues, not this PR):

1. **Backup/restore tooling (`ops/backup/`)** — currently an empty stub;
   `docs/30-backup-restore.md` does not yet document the procedure, and
   `AGENTS.md`'s `alembic upgrade` precondition can't be satisfied without
   it. Recommend this lands *before* TASK-T16 Phase 2C as a risk-control
   sequencing choice (Phase 2C introduces unrecoverable-if-wrong external
   side effects) — not because Phase 2C is proven to technically require
   it; see section 4.
2. **TASK-T16 Phase 2C (real provider registry wiring)** — the actual
   next product frontier per `docs/83-project-continuity.md` and this
   audit's independent confirmation. Follow the TASK file's own
   read-before-write, guard-test-gated ordering.
3. **Client setup guides (`(docs)/guides/*`)** — low-risk, no backend
   dependency, closes the gap between "subscription generation works"
   and "customer can use it unattended." Could run in parallel with 1–2
   since it touches only `frontend/app/(docs)/guides/**`.
4. **`ops/status.py` operational overview** — lower urgency than 1–3
   since `admin/capacity` and `admin/metrics` API endpoints already
   surface overlapping data through the admin UI; worth confirming
   whether a standalone CLI/status tool is still needed or whether the
   admin UI has superseded that original spec item before filing as a
   TASK.
5. **`frontend/lib/api.ts` typed client** — quality-of-life, not
   blocking; reduces risk of future API-contract drift going unnoticed
   on the frontend side.
6. **Issue #87/TASK-T23 post-merge validation** — run the real
   owner-authored `@claude` Issue-comment test and confirm the resulting
   PR checks start without manual workflow approval.

TASK-T13 (split-routing) is deliberately **not** in this sequence — it
stays blocked on external evidence collection per its own file, and
nothing in this audit changes that.

**On filing these as Issues:** Issue #81 asks that, where this audit
implies implementation changes, those changes go into new follow-up
Issues rather than expanding this docs-only PR's scope — it does not ask
for the audit to stop at a recommendation and leave it unfiled. Items
1–3 above are validated findings with clear, independently-checked
evidence (empty `ops/backup/`, empty guide route stubs, confirmed
one-line `api.ts`) and are ready to be filed as Issues once a session
with Issue-creation access does so; this PR itself does not create them
because doing so is outside this docs-only change's own diff surface,
not because filing them isn't warranted. Item 4 (`ops/status.py`) should
be validated first — specifically, whether the existing `admin/capacity`
/`admin/metrics` endpoints already cover its intended purpose — before
filing, since filing it as-is risks duplicating already-shipped
capability.

## 6. Maintenance note

This document is a snapshot audit, not a living index — unlike
`docs/83-project-continuity.md`, it is not expected to be kept
continuously in sync with `main`. If a future session finds this
document stale, prefer re-running an equivalent audit over trusting or
silently patching this one; consider superseding it with a new dated
document (`docs/84-implementation-roadmap-<date>.md` or similar) rather
than rewriting this one in place, so the audit trail of "what did we
think was true, when" is preserved.
