# ADR-017: Narrow the `GatewayRouteBinding` named-lock hold span in the paid-purchase provisioning saga

- 状态: 已接受
- 日期: 2026-09-12（同日第二次修订：独立审查指出两处 Major——(1)
  `gateway_route_binding_write()` 此前只把 `GET_LOCK()` 返回 0/NULL
  转换成 `GatewayRouteBindingLockError`，acquisition 阶段本身的
  `engine.connect()`/lock name 推导/`GET_LOCK` execute 抛出的其它
  DBAPI/SQLAlchemy 异常会以原始异常类型传播，绕过
  `services.confirm_payment_and_provision()` 唯一识别的
  `except GatewayRouteBindingLockError` 补偿分支，导致 phase-A 已
  flush 的业务 mutation 可能被后续 `fail_paid_purchase()` 的事务一并
  误提交；(2) `fail_apply_gateway_lock_acquisition()` 补偿写入的
  run FAILED 状态与 phase-A 业务 mutation 共享同一个未提交的
  `db: Session`，`state.rollback_database()` 会把二者一并回滚，导致
  "run 被标记为 FAILED 且持久化"这条验收标准在生产 adapter
  （`_SqlAlchemyProvisionRuns`）下实际不成立，只被内存 fake 证明过。
  本轮修复：`gateway_route_binding_write()` 把 acquisition 阶段
  （连接、锁名推导、`GET_LOCK` 本身）的任意非
  `GatewayRouteBindingLockError` 异常统一包装为
  `GatewayRouteBindingLockError`（`raise ... from exc` 保留
  chaining），RELEASE_LOCK 阶段的异常不受影响、不被这样包装；
  `_SqlAlchemyProvisionRuns`（`backend/app/api/admin.py`）改为使用
  与业务 `db` 完全独立的专属 `Session`，每次写入立即 commit，
  使 run/job 状态成为不依赖业务事务提交与否的独立审计记录，
  `confirm_payment_and_provision()` 对应把
  `state.rollback_database()` 调整到
  `fail_apply_gateway_lock_acquisition()` 之前执行。详见下方
  "Lock-acquisition-stage 异常规范化" 与 "Provisioning run 状态持久化
  独立于业务事务" 两节。同日第三次修订：独立审查再次发现两处
  Major——(1) `_get_lock()` 的 `execute()` 若抛出普通异常，无法确认
  MySQL 服务端是否已经在响应传输过程中真正授予了该 named lock；此前
  的修复只把异常包装成 `GatewayRouteBindingLockError` 后 `close()`
  连接，未 `invalidate()`，导致该物理连接可能仍被服务端视为锁的持有者
  却被放回连接池，造成锁泄漏。(2) `_SqlAlchemyProvisionRuns` 把
  `start`/`record_step`/`record_external_id`/`mark_status`/
  `mark_notification_failed` 全部改成独立 session 立即 commit 后，
  引入了两类新的 saga 失败点：SUCCEEDED 在
  `provision_apply_gateway()` 内部、早于 `activate_paid_purchase()`
  业务提交之前就已持久化（若该业务提交随后失败，则 Job 已
  durable SUCCEEDED 而 Order/Subscription 却变成
  PROVISION_FAILED）；以及一次普通 progress 写入（如
  `record_step(..., "SUCCEEDED")`）的独立 commit 失败会以异常形式
  逃出 `ProvisioningService` 的 step 级 `except` 块（该块已经正常
  退出），导致其自身的外部补偿（如 `accounting.disable_user()`）
  永远不会执行，而外层又把整个 saga 标记为失败——形成
  "已创建且已启用的外部资源 + 失败订单"的不一致状态。本轮修复：
  `gateway_route_binding_write()` 只在 `GET_LOCK()` 的 `execute()`
  本身抛出非返回值类异常（即取得结果存在歧义）时才
  `invalidate()` 该专属连接，`GET_LOCK()` 返回 0/NULL、以及
  进入 `GET_LOCK()` 之前的任何失败（引擎解析、开连接、锁名推导）
  均不触发 invalidate（服务端已确认或从未被请求授予锁）；
  `ProvisionRunStore` 的写入按用途拆分成两类可靠性契约——
  `record_step`/`record_external_id`/`mark_notification_failed` 是
  best-effort 的进度记录，其自身持久化失败会被
  `_SqlAlchemyProvisionRuns` 内部吞掉、永不向调用方传播；`start`/
  `mark_status` 保持原样可以抛出。`provision_apply_gateway()` 不再
  在内部写入 SUCCEEDED，改为新增
  `ProvisioningService.mark_apply_gateway_succeeded()`（best-effort），
  由 `confirm_payment_and_provision()` 仅在
  `activate_paid_purchase()` 的业务 commit 真正完成之后才调用；同时
  新增 `mark_run_failed()`，供该函数外层 `except Exception` 分支在
  业务回滚已经完成之后，为那些不曾经过
  `ProvisioningService` 自身 step 级 `_failed()` 的失败（例如
  `activate_paid_purchase` 提交本身失败）补上 run 的终态 FAILED 记录。
  详见下方 "Lock acquisition ambiguous-failure connection invalidation"
  与 "Run-persistence ownership and terminal-status ordering (third
  revision)" 两节。同日第四次修订：独立审查指出第三次修订遗漏了
  `PENDING_MANUAL` 这第三种 terminal run status——`provision_prepare()`
  的 `CREATE_TENANT` 两个异常分支此前仍然在返回
  `ProvisionOutcome(PENDING_MANUAL)` 之前，先在内部
  durable `runs.mark_status(PENDING_MANUAL, ...)`，早于
  `services.confirm_payment_and_provision()` 随后才执行的
  `order_state.mark_provision_pending()` 业务提交；更严重的是，若这次
  `mark_status()` 自身提交失败，异常会从 `provision_prepare()` 逃逸，
  被外层 Phase-A 通用异常分支捕获并整体转换成
  `PROVISION_FAILED`——把一个"外部 tenant 创建结果不确定、需要人工
  介入"的正常业务结果，仅因为 audit 写入失败就误判为失败订单。本轮
  修复：`ProvisionOutcome` 新增 `pending_manual_error` 字段，
  `CREATE_TENANT` 的两个异常分支只做 best-effort 的
  `record_external_id`/`record_step`，不再内部
  `mark_status(PENDING_MANUAL)`；新增
  `ProvisioningService.mark_run_pending_manual()`（best-effort，
  与 `mark_apply_gateway_succeeded()` 同构），由
  `confirm_payment_and_provision()` 仅在
  `order_state.mark_provision_pending()` 业务提交真正完成之后才调用。
  同时修正了本节下方 "Failure acquiring the lock itself" 一段过时的
  执行顺序描述（此前误写成先 `fail_apply_gateway_lock_acquisition()`
  再 `state.rollback_database()`，与自第二次修订起就已正确的真实实现
  顺序不符），以及 `fail_apply_gateway_lock_acquisition()` 文档字符串
  中"调用方在本方法返回后才需要 rollback"这句过时描述。详见下方
  "PENDING_MANUAL terminal persistence ordering (fourth revision)"
  一节。）
- 决策范围: TASK-T16 Phase 2B5. Narrows *when* the ADR-016 Decision 3
  named lock (`GET_LOCK()`/`RELEASE_LOCK()` via
  `backend/app/infra/gateway_route_lock.py`) is acquired during
  `confirm_payment_and_provision()`'s nine-step provisioning saga. Does
  **not** change ADR-016's core contract (one named lock is the sole
  correctness mechanism for `GatewayRouteBinding` writers,
  `GET_LOCK -> mutate -> commit/rollback -> RELEASE_LOCK` ordering, single
  MySQL server assumption) — this ADR only relocates the acquisition
  point and adds the phase split needed to do that safely.
- 前置: `ADR-014-xray-desired-state-ownership.md`,
  `ADR-015-marzban-ownership-and-route-identity.md`,
  `ADR-016-route-identity-architecture-unblock.md` (Decision 3 —
  unchanged by this ADR).

## Context

`docs/82-tasks/TASK-T16-real-provider-registry-wiring.md`'s Phase 2B3
entry recorded, when the named lock itself was introduced, that its
current hold span is a blocker that must be resolved before real
(non-mock) `AccountingProvider`/`GatewayProvider`/`EgressProvider`
wiring:

> 在真实（非 mock）provider 接入之前，必须重新评估锁的持有跨度与超时值

Today `confirm_payment_and_provision()` (`backend/app/services.py`)
acquires the lock once, wrapping the **entire** nine-step
`ProvisioningService.provision()` call:

```
CAPACITY → ALLOCATE_ENDPOINT → CREATE_TENANT → STORE_CREDENTIALS →
APPLY_FORWARDER → CREATE_ACCOUNTING_USER → APPLY_GATEWAY →
ISSUE_SUBSCRIPTION → NOTIFY
```

Only `APPLY_GATEWAY` (via `desired_routing_state()` →
`ensure_gateway_route_binding()`) actually touches
`GatewayRouteBinding`. Steps 1–6 call real external providers (egress
tenant creation, forwarder config push, accounting user creation) that,
once wired to non-mock implementations, are real network calls with
real latency and real failure modes — none of which have anything to do
with the row this lock protects. Holding a MySQL named lock across all
of that:

- Serializes **every** paid purchase in the system on the slowest of
  those unrelated external calls, not on actual `GatewayRouteBinding`
  contention.
- Makes `DEFAULT_LOCK_TIMEOUT_SECONDS = 30` (a `GET_LOCK()`
  wait-to-acquire timeout, not a hold-duration cap — see "30-second
  timeout, precisely" below) increasingly likely to be exhausted by
  queued purchases waiting behind a lock nobody actually needed for
  most of that span, once those steps are real HTTP calls instead of
  in-memory mocks.

This ADR narrows the span so the lock protects only what it must.

## Why not keep locking the whole `provision()` call

Rejected. This is the status quo, and is exactly the blocker TASK-T16
recorded: it couples unrelated provider latency (steps 1–6) to
`GatewayRouteBinding` mutual exclusion, with no technical justification
— those steps never read or write that table.

## Why not "just move the `with` block into `APPLY_GATEWAY`'s `try`"

Rejected, and explicitly rejected in the assigning instructions for this
task. `desired_routing_state()`'s `GatewayRouteBinding` mutation (inside
`ProvisioningService`, a step method) only **flushes** — it never calls
`db.commit()`/`db.rollback()`. The real commit
(`activate_paid_purchase()`) or rollback
(`state.rollback_database()`) happens later, in `services.py`, after
`provision()` (or the split successor below) returns. If the lock's
`with` block ended when the step method returned, `RELEASE_LOCK()` would
run *before* that eventual commit/rollback — precisely the ordering
`gateway_route_binding_write()`'s own docstring says must never happen:
a concurrent writer could then observe (or race against) this
transaction's not-yet-finalized `GatewayRouteBinding` row as if it were
free to proceed. Narrowing the *acquisition* point without also solving
*where the release happens relative to commit/rollback* would silently
reintroduce the exact correctness bug ADR-016 exists to prevent.

## Decision: split `ProvisioningService.provision()` into an explicit two-phase API; acquire the lock only for phase B, released only after phase B's caller-owned commit/rollback

### Phase boundary

- **Phase A — `ProvisioningService.provision_prepare(request)`.** Runs
  steps `CAPACITY` through `CREATE_ACCOUNTING_USER` (1–6). Never touches
  `GatewayRouteBinding`, never acquires the named lock. Returns one of:
  - a `ProvisioningCheckpoint(run_id, endpoint, tenant)` — an opaque,
    frozen, serialization-free continuation carrying exactly the two
    values (`EgressEndpointDTO`, `TenantDTO`) phase B's
    `desired_routing_state()` call needs, plus the run id for step
    bookkeeping; or
  - a terminal `ProvisionOutcome(status=PENDING_MANUAL)` — a step-3
    (`CREATE_TENANT`) provider failure that requires human review, exactly
    as today. Callers must not proceed to phase B on this outcome.
  - (any other exception propagates exactly as it does today — capacity
    rejection, endpoint allocation failure, credential-store failure with
    rollback, forwarder-apply failure with revert, accounting-create
    failure with disable — all unchanged, see "Preserved business
    semantics" below.)
- **Phase B — `ProvisioningService.provision_apply_gateway(checkpoint, request)`.**
  Runs steps `APPLY_GATEWAY` through `NOTIFY` (7–9). This is the *only*
  method that calls `desired_routing_state()`. **Callers must invoke this
  method only from inside `state.gateway_route_binding_lock()`,** and
  must not exit that `with` block until their own commit/rollback for
  this call's `GatewayRouteBinding` mutation has completed.
- `ProvisioningService.provision(request)` (kept, unchanged signature) is
  now a thin composition of the two — `provision_prepare()` then, unless
  it returned a terminal outcome, `provision_apply_gateway()` — with no
  lock acquisition of its own, exactly matching today's behavior for
  every existing direct caller/test of `provision()`. The domain layer
  has never acquired this lock itself; that has always been, and remains,
  the caller's responsibility (see "Lock acquire/release owner" below).

A continuation object was chosen over an alternative (e.g. a generator
`provision()` that `yield`s at the phase boundary, or a callback
threaded through `provision()`) because it keeps both phases ordinary,
independently callable, independently testable methods with an explicit,
typed, immutable contract for what crosses the boundary — no generator
lifetime to manage, no implicit "caller must remember to call `.send()`"
convention, no possibility of the boundary silently reordering with
exception handling the way a `try/finally` around a suspended generator
can.

### Transaction, commit, rollback, and lock ownership

Unchanged from today in every respect except *when* the lock is
acquired:

- **Transaction owner:** the `Session` passed into
  `SqlAlchemyProvisioningState`/`_OrderProvisioningState`, shared across
  phase A and phase B — this ADR does not split the one DB transaction
  that already spans steps 1–9 into two; steps 1–6's flushes and phase
  B's `GatewayRouteBinding` flush remain part of the *same* eventual
  commit or rollback, exactly as today. (See "Why not split the DB
  transaction" below.)
- **Success commit owner:** `services.confirm_payment_and_provision()`,
  via `activate_paid_purchase(command, order_state)` inside
  `order_state.transaction()` — called only after
  `provision_apply_gateway()` returns successfully, and only while the
  named lock (acquired below) is still held.
- **Failure rollback owner:** `services.confirm_payment_and_provision()`,
  via `state.rollback_database()` — called from inside the same `with
  state.gateway_route_binding_lock():` block that wraps
  `provision_apply_gateway()`, before that block exits, so the rollback
  always completes before `RELEASE_LOCK()` runs.
- **Lock acquire owner:** `services.confirm_payment_and_provision()`,
  immediately before calling `provision_apply_gateway()` — i.e. after
  phase A has already returned a non-terminal checkpoint, and
  structurally before `desired_routing_state()` can run (phase B is the
  only path to that call).
- **Lock release owner:** the same `with
  state.gateway_route_binding_lock():` block's exit in
  `confirm_payment_and_provision()`, which — per
  `gateway_route_binding_write()`'s own structural guarantee (its
  `finally` only runs after the caller's body, i.e. after phase B *and*
  the subsequent commit/rollback, have both finished) — cannot fire
  before that commit/rollback has completed.

### Exact sequence

Success:

```
provision_prepare() [steps 1-6, unlocked]
  -> GET_LOCK()
  -> provision_apply_gateway() [steps 7-9; step 7 flushes GatewayRouteBinding]
  -> activate_paid_purchase() -> order_state.transaction() -> db.commit()
  -> RELEASE_LOCK()
```

Failure inside phase B (gateway/tail failure):

```
provision_prepare() [steps 1-6, unlocked]
  -> GET_LOCK()
  -> provision_apply_gateway() raises
  -> state.rollback_database()  (db.rollback(), still holding the lock)
  -> RELEASE_LOCK()
  -> fail_paid_purchase()'s own independent terminal transaction
```

Failure acquiring the lock itself (new scenario — see below). **Sequence
corrected in the run-persistence-ordering revision**: `state.
rollback_database()` runs *before* `fail_apply_gateway_lock_acquisition()`
(and therefore before that method's own `_failed()` call durably persists
the run's terminal `FAILED` status) — not after, as an earlier revision
of this ADR incorrectly still showed here. This is the same ordering
invariant as every other `FAILED` write in this saga (see "Run-persistence
ownership and terminal-status ordering" below): `FAILED` must never be
observable ahead of the rollback for the same failure.

```
provision_prepare() [steps 1-6, unlocked; already flushed DB state and
                      created real external side effects: egress tenant,
                      forwarder config, accounting user]
  -> GET_LOCK() raises GatewayRouteBindingLockError
  -> state.rollback_database()  (undoes phase A's flushed DB state, still
                                  holding the lock)
  -> ProvisioningService.fail_apply_gateway_lock_acquisition():
       accounting.disable_user(); alert("GATEWAY_APPLY_FAILED"); mark run FAILED
  -> fail_paid_purchase()'s own independent terminal transaction
```

Failure before phase B is ever reached (capacity/allocate/create-tenant
PENDING_MANUAL/credentials/forwarder/accounting-create): unchanged from
today — none of these paths ever call `gateway_route_binding_lock()`,
before or after this ADR. See "Zero-lock-acquisition paths" below.

### Why the lock-acquisition-failure path is new

Today, `GET_LOCK()` is attempted *before* step 1, so a failure there
means nothing has happened yet: no DB flush, no external side effect,
nothing to compensate. After narrowing, `GET_LOCK()` is attempted after
steps 1–6 have already flushed DB state and made real external calls
(tenant creation, forwarder push, accounting user creation). A
`GatewayRouteBindingLockError` at that point must therefore be treated
as a phase-7 (`APPLY_GATEWAY`) failure in its own right, not silently
absorbed:

- Zero `GatewayRouteBinding` mutation — structurally guaranteed, since
  `desired_routing_state()` is only reachable from inside
  `provision_apply_gateway()`, which never runs if `GET_LOCK()` itself
  raised.
- The already-created accounting user must be **disabled**, matching the
  existing `APPLY_GATEWAY`-failure compensation exactly — never deleted
  (this ADR does not relax that existing invariant).
  `ProvisioningService.fail_apply_gateway_lock_acquisition()` reuses the
  identical `disable_user` / `_alert("GATEWAY_APPLY_FAILED")` /
  `_failed(..., ProvisionStep.APPLY_GATEWAY, ...)` compensation the
  in-band `APPLY_GATEWAY` exception handler already performs, so the two
  paths cannot semantically drift from each other by accident.
- The DB transaction must be rolled back (`state.rollback_database()`),
  undoing steps 1–6's flushed-but-uncommitted state — same as any other
  mid-saga failure.
- The order must reach `PROVISION_FAILED` via the existing
  `fail_paid_purchase()` terminal transaction — no new order/subscription
  status is introduced.
- No fallback principal, no continuing to `ISSUE_SUBSCRIPTION`, no
  success path taken under any circumstance.

### Zero-lock-acquisition paths

These paths never call `state.gateway_route_binding_lock()`, before or
after this change, and must continue not to:

- Capacity rejection (before `PAID` is even recorded).
- `ALLOCATE_ENDPOINT` failure.
- `CREATE_TENANT` → `PENDING_MANUAL` (both the `ExternalTenantCreationError`
  branch and the generic-exception branch, both already return before
  the lock would ever be reached).
- `STORE_CREDENTIALS` failure.
- `APPLY_FORWARDER` failure.
- `CREATE_ACCOUNTING_USER` failure.

Verified by lock-acquisition-count assertions in
`backend/tests/integration/test_provisioning_phase_boundary.py` (real
MySQL `GET_LOCK()`/`RELEASE_LOCK()` call counting, not a mock
call-count), not merely inferred from reading the code.

### Why not split the one DB transaction into two

Rejected. Splitting phase A and phase B into separately-committed
transactions would mean steps 1–6's success could be durably persisted
while phase B (the actual `GatewayRouteBinding` mutation and the
order's `PAID → ACTIVE` transition) fails — turning one atomic,
compensatable saga step into two, where the first commit is no longer
cleanly undoable by `rollback_database()`. The existing external
compensations (`disable_user`, forwarder revert, `release_endpoint`) all
assume they are compensating an *uncommitted* DB transaction that can
still be rolled back wholesale; splitting the transaction would require
redesigning every one of those compensations into its own explicit
undo-a-committed-write operation, which is exactly the kind of
transaction-topology change this ADR's assigning instructions
explicitly forbid. Phase A and phase B therefore continue to share one
`Session`/one eventual commit-or-rollback, as today — only the lock's
acquisition point moves.

### `NOTIFY` remains inside the lock

`ProvisionStep.NOTIFY` runs inside `provision_apply_gateway()`, which
this ADR requires callers to invoke entirely inside
`gateway_route_binding_lock()`. Moving `NOTIFY` outside the lock without
changing its transaction/run-status/compensation semantics was evaluated
and is **not** done in this ADR/PR: `NOTIFY`'s own failure handling
(`self.runs.mark_notification_failed(...)`, swallowed rather than
re-raised) already never affects the lock or the commit/rollback
decision either way today, so moving it would be a change made for its
own sake, not one required to unblock real `Egress`/`Accounting`/
`Gateway` provider wiring — the actual TASK-T16 blocker this ADR exists
to resolve. Consequently: **real, non-mock `EgressProvider`,
`AccountingProvider`, and `GatewayProvider` wiring is unblocked by this
ADR** (none of their calls happen inside the lock any more, except the
`GatewayProvider.render/validate/apply` calls inside `APPLY_GATEWAY`
itself, which is exactly what this lock exists to serialize).
**Real, non-mock `NotifyProvider` wiring remains blocked** until a
follow-up narrows `NOTIFY` out of the critical section (or the
notify-failure semantics are redesigned to tolerate it moving) — this is
recorded as an explicit, not-silently-dropped, open item in
`docs/82-tasks/TASK-T16-real-provider-registry-wiring.md`'s Phase 2B5
entry.

### 30-second timeout, precisely

`DEFAULT_LOCK_TIMEOUT_SECONDS = 30` (`backend/app/infra/
gateway_route_lock.py`) is the duration `GET_LOCK()` will *wait to
acquire* the lock before giving up — an **availability** parameter,
governing how long a contending purchase queues behind another one
already holding the lock. It is **not**, and has never been, a cap on
how long the lock may be *held* once acquired — nothing in
`gateway_route_binding_write()` enforces or even measures a maximum
hold duration; a holder keeps it for exactly as long as its `with` block
takes. This ADR does not change the value (no real production latency
measurement exists yet to justify a different number) but does change
what contributes to hold *duration*: before this ADR, a purchase held
the lock across the (soon-to-be-real) `Egress`/`Accounting` provider
calls in steps 1–6; after, it holds the lock only across
`GatewayProvider.render/validate/apply` and the DB commit/rollback that
follows — Webshare/Marzban HTTP latency no longer contributes to named-
lock hold duration at all. `GatewayProvider`'s own real-provider latency
(not yet wired) will still need production measurement once it exists,
to confirm 30 seconds remains a reasonable *acquisition-wait* budget
under real contention; that measurement is out of scope for this ADR and
is not claimed as done here.

## Consequences

- `backend/app/domain/provisioning.py`: `ProvisioningService` gains
  `provision_prepare()`, `provision_apply_gateway()`,
  `fail_apply_gateway_lock_acquisition()`, and the new
  `ProvisioningCheckpoint` dataclass. `provision()` becomes a two-line
  composition of the first two; its external behavior for every existing
  caller is unchanged.
- `backend/app/services.py`: `confirm_payment_and_provision()` calls
  `provision_prepare()` outside any lock, then acquires
  `state.gateway_route_binding_lock()` only around
  `provision_apply_gateway()` and the subsequent commit, with a new
  `except GatewayRouteBindingLockError` branch performing the
  lock-acquisition-failure compensation described above before falling
  through to the existing `fail_paid_purchase()` handling.
- `backend/app/infra/gateway_route_lock.py`: `gateway_route_binding_write()`
  now records, on the `Session` it is given (`session.info`), whether
  that session currently holds the lock. This is a production-bypass
  guard (see below), not a behavior change to the lock protocol itself.
- `backend/app/infra/provisioning_state.py`:
  `SqlAlchemyProvisioningState.ensure_gateway_route_binding()` checks
  that marker and raises `GatewayRouteBindingLockError` (fail-closed,
  zero mutation) if called without the session currently holding the
  lock — closing the latent bypass where `services.provision()` (or
  `ProvisioningService.provision()` called directly) could, if ever
  pointed at a real `SqlAlchemyProvisioningState` outside
  `confirm_payment_and_provision()`'s lock-wrapped call path, reach
  `desired_routing_state()` without the named lock ever being acquired.
  Today no such caller exists in production (verified by an explicit
  repository-wide search, recorded in this PR's description); this guard
  makes that a structurally-enforced invariant instead of one resting on
  "nobody has wired it up yet."
- No change to `ProvisioningState` protocol's public shape beyond what
  already existed; no change to `backend/app/providers/base.py`.

## Lock-acquisition-stage exception normalization (second revision)

`gateway_route_binding_write()` now treats the entire acquisition stage —
opening the dedicated connection, deriving the lock name, and the
`GET_LOCK()` call itself — as one fail-closed unit: any exception raised
there, whatever its original type (a dropped connection, a pool
exhaustion error, any other DBAPI/SQLAlchemy exception), is re-raised as
`GatewayRouteBindingLockError` with the original chained via `raise ...
from exc`. This is not cosmetic: `confirm_payment_and_provision()`'s
compensation branch (disable the accounting user, alert, mark the run
FAILED, roll back phase-A's DB state) is keyed on catching exactly
`GatewayRouteBindingLockError` — before this fix, any acquisition-stage
failure other than the two `GET_LOCK()` return-value cases would
propagate as some other exception type, miss that `except` clause
entirely, and fall through to the generic failure handler, which never
rolled back `state`'s phase-A mutations. Because
`GatewayRouteBindingLockError` for the `GET_LOCK()` return-value cases is
already raised inside `_get_lock()`/`gateway_route_binding_lock_name()`,
the normalization re-raises only exceptions of *other* types, so those
two already-correct cases are unaffected. Failures inside
`_release_lock()` (the lock-release stage, after the caller's body has
already run) are deliberately left unwrapped: a release failure already
has its own handling (connection invalidation) and is not an acquisition
failure, so relabeling it as one would misrepresent what happened to any
caller inspecting the exception type.

## Provisioning run status persisted independently of the business transaction

`_SqlAlchemyProvisionRuns` (`backend/app/api/admin.py`, the concrete
`ProvisionRunStore` used by the paid-purchase saga) previously shared the
same `db: Session` as `_OrderProvisioningState`/`SqlAlchemyProvisioningState`.
`start()` only `db.add()`+`db.flush()`ed the `Job` row; `record_step()`/
`mark_status()`/etc. only mutated it in place — none of them committed.
That meant the run's own row (and every status update to it, including
`FAILED`) lived in the *same* uncommitted transaction as phase-A's
business mutations (endpoint allocation, credential storage, forwarder
state). `state.rollback_database()` — required by the lock-acquisition-
failure path above, and by every other mid-saga failure already —
therefore rolled back the run's own `FAILED` marker (and, for a
freshly-started run, the row's own `INSERT`) right along with the
business mutations it was supposed to record failure *about*. This is
why the "run is marked FAILED" acceptance criterion had only ever been
proven against the in-memory `_Runs`/`FakeRuns` test doubles, never
against the real production adapter.

Fix: `_SqlAlchemyProvisionRuns` now opens its **own** dedicated `Session`
(from the same engine as the request-scoped `db`, via `db.get_bind()`) in
its constructor, and commits immediately after every write
(`start`/`record_step`/`record_external_id`/`mark_status`/
`mark_notification_failed`). The route handler
(`admin.py::admin_confirm_payment`) closes this session in a `finally`
after the saga call. This makes run/job status what it conceptually
already was meant to be — a durable audit trail — independent of whether
the business-data transaction it's reporting on ultimately commits or
rolls back, **without** touching the shared `jobs` table's schema or any
other `Job`-using code path; only this one `ProvisionRunStore` adapter's
session lifecycle changed. `services.confirm_payment_and_provision()`'s
lock-acquisition-failure branch now calls `state.rollback_database()`
*before* `provisioning.fail_apply_gateway_lock_acquisition()` — with the
run store's writes independently committed this ordering is no longer
required for correctness, but it is kept because it is the more
intuitive sequence (discard the abandoned business-data attempt, then
record that the run failed, then close out the order's own terminal
transaction) and matches what a human reading the code would expect.

This was evaluated against, and deliberately does **not** become, a
general `Job`-system refactor: no other `ProvisionRunStore`/`Job` caller
changes, the `Job` model and `jobs` table are untouched, and no other
job type's persistence semantics change.

### Incidental fix surfaced by real-MySQL verification: `last_error_code` truncation

Writing this section's own production-backed integration test (real
`_SqlAlchemyProvisionRuns` against real MySQL/MariaDB, forcing a genuine
lock-acquisition failure end-to-end) surfaced a pre-existing latent bug,
independent of both Majors above: `Job.last_error_code` is a
`String(80)` column, but `mark_status()` wrote the *full* exception
message (e.g. `gateway_route_binding_write()`'s normalized message, which
embeds the original chained exception's text) into it verbatim. Any error
string over 80 characters — which the very message this ADR's Major 1 fix
constructs routinely is — raised `sqlalchemy.exc.DataError: Data too long
for column 'last_error_code'` on real MySQL/MariaDB, aborting the
`mark_status()` commit and losing the `FAILED` status this section's fix
exists to make durable. SQLite/mocked test paths never caught this because
SQLite does not enforce `VARCHAR` length limits. Fixed by truncating
defensively at the persistence boundary in `mark_status()`
(`error[:80] if error else error`) rather than widening the schema: this
is a `last_error_code` write-boundary concern local to this one adapter,
not a schema or migration change, and not a general `Job`-system change.

### Tests added for this revision

- `backend/tests/integration/test_gateway_route_binding_lock.py::test_generic_exception_during_get_lock_is_normalized_to_lock_error`
  — injected-contract unit-style test proving a generic (non-return-value)
  exception raised while executing `GET_LOCK()` is normalized into
  `GatewayRouteBindingLockError` with `from`-chaining preserved.
- `backend/tests/integration/test_provisioning_phase_boundary.py::test_generic_acquisition_exception_rolls_back_phase_a_before_marking_failed`
  — real MySQL/MariaDB, through the actual `confirm_payment_and_provision()`
  call path, with only the `GET_LOCK()` execution itself injected to raise;
  proves phase-A's flushed `EgressEndpoint.current_count` mutation is
  rolled back, zero `GatewayRouteBinding` writes survive, the accounting
  user is disabled, and the run is marked `FAILED`.
- `backend/tests/integration/test_provisioning_phase_boundary.py::test_production_run_store_marks_failed_independently_of_phase_a_rollback`
  — the production-backed acceptance test Major 2 required: real
  `_SqlAlchemyProvisionRuns` + real `_OrderProvisioningState`/
  `_SqlAlchemyOrderState` sharing one real MySQL `Session`, a manufactured
  lock-acquisition failure, then a **fresh, independent** `Session`
  verifying zero `GatewayRouteBinding` rows, the rolled-back
  `EgressEndpoint.current_count`, `Order.status == PAID` /
  `Subscription.status == PROVISION_FAILED` (the paid-but-unusable
  terminal state), the `Job` row still present with `status == FAILED`,
  and `last_error_code` persisted — never the in-memory `_Runs` fake.

## Lock acquisition ambiguous-failure connection invalidation (third revision)

The second revision's exception normalization was necessary but not
sufficient: it correctly turned any acquisition-stage exception into
`GatewayRouteBindingLockError`, but the dedicated lock connection itself
was only ever `close()`d in the `finally`, never `invalidate()`d, when
`_get_lock()`'s own `execute()` call raised. MySQL named locks are
connection-scoped and are **not** released by an ordinary close or
rollback — only by an explicit `RELEASE_LOCK()` or by the holding
connection actually terminating. If `GET_LOCK()`'s response is lost in
transit (a dropped connection, a truncated read) *after* the server has
already granted the lock, the code cannot tell the difference from a
transport failure that happened before granting it — but treating both
cases identically (plain `close()`, back to the pool) risks silently
leaving a live pooled connection that MySQL still considers the lock's
actual holder, which would block every future acquisition attempt with
no visible cause.

Fix: `gateway_route_binding_write()` now `invalidate()`s the dedicated
connection in exactly the one case where this ambiguity exists — a
generic (non-`GatewayRouteBindingLockError`) exception from the
`_get_lock()` call itself — discarding the underlying physical connection
rather than returning it to the pool. The two unambiguous outcomes do
**not** invalidate: `GET_LOCK()` returning `0`/`NULL` (the server
confirmed no lock was granted), and any failure before `GET_LOCK()` is
ever called (engine resolution, opening the connection, deriving the lock
name) — the lock was never requested on that connection, so there is
nothing to leak. `engine.connect()` and `.execution_options(...)` were
also split into two separate steps (previously chained in one
expression), fixing a latent connection leak: if `execution_options()`
raised after `connect()` had already succeeded, the just-opened raw
connection was never assigned to a variable and so never closed.

Public `GatewayRouteBindingLockError` messages raised by this function no
longer interpolate the original exception's text (previously
`f"...: {exc}"`); they are now stable, non-sensitive descriptions, with
the real cause available only via `raise ... from exc` chaining — a
caller that needs the DBAPI-level detail (for logging, debugging) reads
`__cause__`, but the exception's own public string never repeats
potentially sensitive connection/driver detail.

Proven by `test_ambiguous_failure_after_real_get_lock_success_does_not_leak_the_lock`
(`test_gateway_route_binding_lock.py`): a real dedicated connection
genuinely acquires the real named lock on real MySQL, and *only then*
does the call raise (simulating the confirmation being lost in transit).
A second, fully independent connection is then able to acquire the same
lock immediately — proof the first connection was actually invalidated
(and thus terminated, releasing the server-side lock), not merely closed
and pooled while MySQL still considered it the holder.

## Run-persistence ownership and terminal-status ordering (third revision)

The second revision's independent-`Session`-per-write design for
`_SqlAlchemyProvisionRuns` fixed the original problem (a business
rollback erasing the run's own `FAILED` marker) but, by making *every*
write (including routine per-step progress markers) an independent,
synchronously-committed operation, introduced two new saga failure
points the second revision had not accounted for:

- **Case A**: `provision_apply_gateway()` persisted the run's terminal
  `SUCCEEDED` status *before* returning to its caller — which, in the
  paid-purchase saga, is *before* `activate_paid_purchase()`'s own
  business-data commit. If that commit then failed, the `Job` would be
  durably `SUCCEEDED` while the order/subscription ended up
  `PROVISION_FAILED` — a terminal-status race the run's own persistence
  design must never allow.
- **Case B**: a transient failure persisting a routine, non-terminal
  progress marker (e.g. `record_step(..., "SUCCEEDED")` for
  `CREATE_ACCOUNTING_USER` or `APPLY_GATEWAY`, called *after* that step's
  own external side effect — `accounting.create_user()`,
  `gateway.apply()` — already succeeded and that step's own `except`
  block had already exited normally) would propagate as an exception out
  of `ProvisioningService`, skipping past that step's own compensation
  entirely and reaching the saga's outer failure handling instead — which
  marks the *order* failed but has no way to know it must also disable
  the accounting user (or otherwise compensate) for a step that, as far
  as its own code was concerned, had already succeeded.

Ownership, stated explicitly per the review's request:

- **Run identity (`start()`)**: owned by `ProvisioningService.
  provision_prepare()`, called before any external side effect, and
  remains fully raising — nothing exists yet to compensate if this fails.
- **Progress bookkeeping (`record_step`, `record_external_id`,
  `mark_notification_failed`)**: owned by whichever step just ran, but
  now contractually **best-effort** — `ProvisionRunStore`'s own docstring
  states implementations must never let a failure here propagate.
  `_SqlAlchemyProvisionRuns` implements this by catching and swallowing
  (with a `rollback()` on its dedicated session to stay usable for the
  next write) any exception from these three methods specifically. This
  is what fixes Case B: a step's own external-side-effect compensation
  can no longer be skipped by an unrelated audit-write hiccup.
- **`FAILED` terminal persistence**: every failure branch inside
  `provision_prepare()`/`provision_apply_gateway()` now calls
  `self.state.rollback_database()` immediately before its own
  `self._failed(...)` call (previously only `STORE_CREDENTIALS` did this
  internally; every other branch relied on an *outer* caller in
  `services.py` to roll back only *after* the exception had already
  propagated past `_failed()`'s `mark_status(FAILED, ...)` write). This
  guarantees `FAILED` is never observable ahead of its own rollback,
  without needing to thread `run_id`/error state through exception
  unwinding to an outer layer: each failure site already has both
  `self.state` and `run_id` in scope. For failures that happen entirely
  outside `ProvisioningService`'s own step handlers — the one case that
  remains is `activate_paid_purchase()`'s own business commit failing
  *after* `provision_apply_gateway()` already returned successfully —
  `services.confirm_payment_and_provision()`'s outer `except Exception`
  branch calls the new `ProvisioningService.mark_run_failed()` once its
  own rollback (performed by `order_state.transaction()`'s own except
  clause, since `state` and `order_state` share one `db: Session`) has
  already completed. `mark_status()` itself remains fully raising, per
  `ProvisionRunStore`'s contract.
- **`SUCCEEDED` terminal persistence**: `provision_apply_gateway()` no
  longer persists this itself. The new
  `ProvisioningService.mark_apply_gateway_succeeded()` does, and it is
  **best-effort** (catches and discards any failure) — by the time a
  caller reaches it, the business transaction it reports on has already
  durably committed, so a Job-write hiccup at that point must not turn an
  already-real success into a reported failure. `services.
  confirm_payment_and_provision()` calls it only immediately after `with
  order_state.transaction(): activate_paid_purchase(...)` returns without
  raising. The non-phase-split `ProvisioningService.provision()`
  composition (kept for callers without a separate business-commit
  boundary of their own) calls it immediately after
  `provision_apply_gateway()` returns, since there is no later commit to
  wait for in that path.
- **Ordering invariant**: `FAILED` is never persisted before the
  rollback for the same failure completes; `SUCCEEDED` is never persisted
  before the corresponding business commit completes. The `Job`'s
  terminal status never runs ahead of the real business terminal state it
  reports on.

This remains, deliberately, not a general `Job`-system rewrite: the
`Job` model, the `jobs` table, and every other `Job`-using code path are
untouched; only `_SqlAlchemyProvisionRuns`'s five methods and
`ProvisioningService`'s failure/success call sites changed.

### Tests added for this revision

- `backend/tests/integration/test_gateway_route_binding_lock.py::test_generic_exception_during_get_lock_invalidates_the_ambiguous_connection`
  and `::test_get_lock_return_value_failures_do_not_invalidate_the_connection`
  — injected-contract control pair proving invalidation happens only for
  the ambiguous (generic-exception) case, never for the two unambiguous
  `GET_LOCK()` return values.
- `backend/tests/integration/test_gateway_route_binding_lock.py::test_ambiguous_failure_after_real_get_lock_success_does_not_leak_the_lock`
  — real MySQL/MariaDB: a real connection genuinely acquires the real
  lock, then fails as if confirmation were lost in transit; a second,
  independent connection then acquires the same lock immediately,
  proving no leak.
- `backend/tests/integration/test_provisioning_phase_boundary.py::test_production_activate_commit_failure_never_leaves_job_falsely_succeeded`
  — production-backed: forces `activate_paid_purchase()`'s own commit to
  fail after `provision_apply_gateway()` already succeeded; proves the
  `Job` ends up durably `FAILED` (via the new `mark_run_failed()`), never
  `SUCCEEDED`, alongside the correctly-rolled-back `GatewayRouteBinding`/
  `EgressEndpoint` state and the subscription's `PROVISION_FAILED`
  terminal status.
- `backend/tests/integration/test_provisioning_phase_boundary.py::test_production_accounting_progress_write_failure_never_leaves_user_wrongly_enabled_or_disabled`
  — production-backed: forces the progress-write commit immediately after
  `accounting.create_user()` succeeds to fail; proves the saga completes
  normally (the accounting user is never wrongly disabled, the order/
  subscription reach their real `ACTIVATED`/`ACTIVE` state, and the `Job`
  ends up `SUCCEEDED`) rather than an audit-write hiccup masquerading as a
  saga failure.
- `backend/tests/integration/test_provisioning_phase_boundary.py::test_production_gateway_progress_write_failure_never_bypasses_business_compensation`
  — the same pattern for the `APPLY_GATEWAY` step's progress write,
  proving the gateway's committed `GatewayRouteBinding` state and the
  business transaction's terminal outcome are unaffected by the
  run-store's own transaction design.
- `backend/tests/integration/test_provisioning_phase_boundary.py::test_production_run_store_marks_succeeded_only_after_business_commit`
  — production-backed, synchronized via a pausing gateway: an independent
  session observes the `Job` is *not yet* `SUCCEEDED` while
  `activate_paid_purchase()`'s commit is still pending, and only
  `SUCCEEDED` once the whole call has returned.

All five tests above use the real `_SqlAlchemyProvisionRuns` +
`_OrderProvisioningState`/`_SqlAlchemyOrderState` production adapters
sharing one real MySQL/MariaDB `Session`, per the review's explicit
requirement that the in-memory `_Runs`/`FakeRuns` test double cannot be
used as evidence for these production transaction invariants.

## PENDING_MANUAL terminal persistence ordering (fourth revision)

The third revision's ownership contract explicitly named `FAILED` and
`SUCCEEDED` as the two terminal statuses whose persistence ordering
matters, but `ProvisionRunStore.mark_status()`'s own signature has always
accepted a third: `PENDING_MANUAL` — set when `CREATE_TENANT` cannot
confirm whether external tenant creation actually succeeded, and the
order needs manual review rather than being treated as failed. This
status was missed by the third revision's fix: `provision_prepare()`'s
two `CREATE_TENANT` exception branches still called
`self.runs.mark_status(run_id, ProvisionStatus.PENDING_MANUAL, str(exc))`
directly, before returning the terminal `ProvisionOutcome` to their
caller — durably persisting the run's `PENDING_MANUAL` status *before*
`services.confirm_payment_and_provision()`'s own
`order_state.mark_provision_pending()` business-data commit ever ran.
Two concrete problems followed from this, exactly mirroring Major 2
Case A/B from the third revision but for a third status neither of those
fixes covered:

- If `order_state.mark_provision_pending()`'s business commit
  subsequently failed for any reason, the `Job` would already be durably
  `PENDING_MANUAL` for a business outcome that never actually got
  recorded — the same terminal-status race the `SUCCEEDED` fix
  eliminated, just for a different status.
- Worse: if `mark_status(PENDING_MANUAL, ...)`'s own commit failed
  (a transient run-store DB issue, nothing to do with the actual
  provisioning outcome), that exception propagated directly out of
  `provision_prepare()` — a method whose *only* other failure mode is a
  genuine step failure — and was caught by
  `confirm_payment_and_provision()`'s Phase-A generic
  `except Exception as exc:` handler, which unconditionally treats
  anything reaching it as a saga failure: `state.rollback_database()`
  followed by `fail_paid_purchase()` marking the order/subscription
  `PROVISION_FAILED`. A legitimate "external tenant creation result is
  unconfirmed, needs human review" outcome would have been silently
  reclassified as an outright failure, purely because of an unrelated
  audit-write hiccup — precisely the failure mode the third revision's
  "run persistence failure must never change real saga/external-side-
  effect semantics" principle was meant to rule out everywhere, not just
  for `FAILED`/`SUCCEEDED`.

Fix, matching the `SUCCEEDED` pattern exactly:

- `ProvisionOutcome` gained a new field, `pending_manual_error: str |
  None = None`, set only when `status` is `PENDING_MANUAL` — the minimal,
  typed way to carry `CREATE_TENANT`'s original error text (preserving
  `ExternalTenantCreationError`'s message, and its `external_id` via the
  existing best-effort `record_external_id()` call, unchanged) across the
  phase boundary without durably writing it first.
- `provision_prepare()`'s two `CREATE_TENANT` exception branches no
  longer call `mark_status()` at all. They still perform their existing
  best-effort progress bookkeeping (`record_external_id()` when an
  `external_id` is present, `record_step(..., "PENDING_MANUAL")`) and
  return `ProvisionOutcome(run_id, ProvisionStatus.PENDING_MANUAL,
  pending_manual_error=str(exc))`.
- New `ProvisioningService.mark_run_pending_manual(run_id, error)` —
  best-effort, identical shape to `mark_apply_gateway_succeeded()`: it
  catches and discards any failure persisting the status, because by the
  time a caller reaches it, the real business truth has already been
  durably recorded, so an audit-write hiccup must never look like that
  legitimate outcome needs to be undone or reinterpreted as failed.
- `services.confirm_payment_and_provision()` calls it only immediately
  after `with order_state.transaction(): order_state.
  mark_provision_pending(...)` returns without raising — never before.
  If that business commit itself fails, the exception propagates before
  `mark_run_pending_manual()` is ever reached, so the `Job` can never end
  up prematurely `PENDING_MANUAL` for a business state that was never
  actually recorded.
- The non-phase-split `ProvisioningService.provision()` composition
  (which has no separate business-commit boundary for the pending-manual
  case, same as for `SUCCEEDED`) calls `mark_run_pending_manual()`
  immediately once `provision_prepare()` returns a `ProvisionOutcome`.

This round also corrected two pieces of now-stale documentation the
review flagged: the "Failure acquiring the lock itself" sequence diagram
earlier in this ADR (previously still showing
`fail_apply_gateway_lock_acquisition()` running *before*
`state.rollback_database()`, which stopped being true as of the second
revision's fix but was never corrected in the diagram itself), and
`fail_apply_gateway_lock_acquisition()`'s own docstring (previously
stating the caller rolls back *after* calling it, when the caller has
rolled back *before* calling it since the second revision). Neither
correction changes any behavior — both were prose catching up to code
that was already correct.

### Tests added for this revision

All using the real `_SqlAlchemyProvisionRuns` + `_OrderProvisioningState`/
`_SqlAlchemyOrderState` production adapters sharing one real MySQL/MariaDB
`Session` — never the in-memory `_Runs`/`FakeRuns` test double, per the
review's explicit requirement for these transaction-ordering acceptance
criteria:

- `test_production_pending_manual_persists_only_after_business_commit`
  — the real `ExternalTenantCreationError` path through
  `confirm_payment_and_provision()`: the business `mark_provision_pending()`
  commit succeeds, the named lock is never acquired, and the `Job` ends
  up `PENDING` with the original error text preserved.
- `test_production_pending_manual_business_commit_failure_never_leaves_job_prematurely_pending`
  — forces `order_state.mark_provision_pending()`'s own commit to fail:
  the `Job` must never have been written to `PENDING` at all (it stays at
  whatever `start()` initialized it to).
- `test_production_pending_manual_run_persistence_failure_never_flips_business_state_to_failed`
  — the business commit succeeds first, then the run-store's own
  `mark_status(PENDING_MANUAL, ...)` commit is forced to fail: the
  business state (order/subscription) must remain in its legitimate
  pending-manual state, never `PROVISION_FAILED`.
- `test_production_pending_manual_preserves_external_id_when_present`
  — an `ExternalTenantCreationError` carrying an `external_id`: proves
  the `external_id` is still recorded (best-effort, as
  `record_external_id()` always has been) even with `mark_status()` no
  longer called from inside `provision_prepare()`.

## 重新评估条件

- 一旦 `GatewayProvider`/`NotifyProvider` 的真实 provider 有生产环境
  延迟数据，须重新评估 `DEFAULT_LOCK_TIMEOUT_SECONDS` 是否仍然合理。
- 一旦决定把 `NOTIFY` 移出 named-lock critical section，须先补一份
  说明其 run-status/补偿语义如何在锁外保持一致的分析，再动代码。
- 若未来需要支持多主 MySQL 写拓扑，本 ADR 与 ADR-016 都需要重新评估
  named lock 作为唯一并发合约的假设。
