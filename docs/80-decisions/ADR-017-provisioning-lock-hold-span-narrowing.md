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
  独立于业务事务" 两节。）
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

Failure acquiring the lock itself (new scenario — see below):

```
provision_prepare() [steps 1-6, unlocked; already flushed DB state and
                      created real external side effects: egress tenant,
                      forwarder config, accounting user]
  -> GET_LOCK() raises GatewayRouteBindingLockError
  -> ProvisioningService.fail_apply_gateway_lock_acquisition():
       accounting.disable_user(); alert("GATEWAY_APPLY_FAILED"); mark run FAILED
  -> state.rollback_database()  (undoes phase A's flushed DB state)
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

## 重新评估条件

- 一旦 `GatewayProvider`/`NotifyProvider` 的真实 provider 有生产环境
  延迟数据，须重新评估 `DEFAULT_LOCK_TIMEOUT_SECONDS` 是否仍然合理。
- 一旦决定把 `NOTIFY` 移出 named-lock critical section，须先补一份
  说明其 run-status/补偿语义如何在锁外保持一致的分析，再动代码。
- 若未来需要支持多主 MySQL 写拓扑，本 ADR 与 ADR-016 都需要重新评估
  named lock 作为唯一并发合约的假设。
