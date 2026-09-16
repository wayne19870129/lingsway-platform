# ADR-022 — Gateway desired-state reconciliation and downstream compensation

- Status: Proposed
- Date: 2026-09-16
- Decision owners: repository owner / human approval
- Scope: TASK-T16 Phase 2C3B follow-up
- Depends on: ADR-014, ADR-015, ADR-016, ADR-017, ADR-019, ADR-020, ADR-021

## Context

PR #114 wires a real Marzban-controlled Xray runtime behind
`GATEWAY_PROVIDER=xray_file`. That provider has an external runtime side
effect, while the current application flow was designed around mock gateway
behavior. A successful gateway apply can therefore be followed by a failed
downstream subscription step, a failed paid-purchase business commit, or an
expiry/release writer that changes `GatewayRouteBinding` without applying the
derived Xray projection.

The authoritative invariant is:

- DB state is the desired-state authority.
- The repo-owned Xray file/runtime is a derived projection.
- The writer baseline is the last projection whose apply and post-apply
  verification were confirmed.
- A successfully completed operation must not leave DB desired state A,
  runtime projection B, and an APPLIED baseline for B when A != B.
- If consistency cannot be restored, the outcome is explicitly
  DEGRADED/manual intervention or durable reconciliation; stale B must not
  remain a normal APPLIED state.

This ADR is a proposed architecture decision only. It does not change domain
contracts, provisioning orchestration, release signatures, provider
interfaces, or production behavior in this slice.

## Authoritative GatewayRouteBinding writers audited

The repository audit found these mutation paths:

1. `backend/app/infra/provisioning_state.py::
   SqlAlchemyProvisioningState.ensure_gateway_route_binding()` creates a
   binding or updates its egress, principal, outbound tag, and enabled state.
   It is reached by `ProvisioningService` and is guarded by the ADR-016 named
   lock.
2. `backend/app/workers/accounting_sync.py::release_egress()` updates the
   active binding to disabled/released while releasing the egress and commits
   under the same named lock.
3. `ops/reconciliation/gateway_principals.py::_conditional_update()` updates
   `gateway_principal` during the explicitly confirmed manual reconciliation
   operation, under the same named lock and conditional-update contract.

No other create/update/enable/disable/release writer was found in the
application, worker, admin, migration, or maintenance paths inspected in this
slice. Model definitions and read-only admin/API queries are not writers.
This list is authoritative for the follow-up implementation unless a later
repository audit finds a new writer.

## Required contract for every writer

Every authoritative writer must handle the DB mutation and the repo-owned
Xray projection as one reconciliation contract. It may not finish after
changing only the database.

The named gateway writer lock must remain held continuously across:

1. Fresh operation-scoped reads of the currently committed desired state and
   any required previous projection/baseline.
2. The DB mutation and flush.
3. Fresh desired-state composition for the post-mutation state.
4. Gateway render, validate, and apply.
5. The DB commit when the apply is successful.
6. DB rollback and any external accounting-user compensation when the apply or
   later business step fails.
7. Re-render/apply compensation to the previous committed desired state when
   a later DB/business step invalidates the first apply.

The lock must not be released before compensation completes or a durable
DEGRADED/manual-intervention outcome has been recorded. A second writer must
not enter between rollback and runtime restoration.

A future implementation must obtain a process-lifetime provider from the
existing registry and construct operation-scoped resolver/context from the
same operation Session. `accounting_sync.py` must not instantiate a second
registry or provider. Providers must not query ORM state or infer desired
state from runtime.

## Options considered

### Option 1 — Apply before commit with explicit downstream compensation

This matches the current Phase 2B/2C ordering and is the proposed direction
for the launch sprint because it preserves ADR-017's lock span and existing
`GatewayProvider.render()`, `validate()`, and `apply()` contract.

The future implementation must:

- capture a fresh previous committed desired-state snapshot before the
  mutation;
- flush the new binding, compose and apply the post-mutation desired state;
- commit only after apply and verification succeed;
- if subscription issuance, the paid-purchase business commit, or another
  same-operation step fails after apply, roll back the DB transaction while
  holding the lock and apply the captured previous committed state;
- treat successful compensation as the new confirmed APPLIED baseline for the
  restored projection;
- if compensation fails, explicitly transition to DEGRADED/manual
  intervention and invalidate or quarantine the stale APPLIED baseline; never
  report zero writes or treat the stale new candidate as authoritative;
- preserve existing separate accounting-user semantics: disable only when the
  accounting side effect is known to exist, and keep ambiguous outcomes
  manual;
- make expiry/release follow the same apply-before-commit/rollback-or-
  compensate contract, so a release cannot commit a DB-only route deletion.

This option has a bounded failure window and uses the existing provider
contract, but compensation is operationally required and must itself be
observable and fail closed.

### Option 2 — Commit DB desired state first with durable runtime reconciliation

This would commit the desired binding/release first and enqueue a durable
reconciliation state/job. Runtime lag would be an explicit non-success state;
provisioning could not report customer success until reconciliation is
confirmed, and retries would need idempotency keys, durable attempt state, and
baseline semantics for pending/failed projections. During gateway failure, the
system would need a documented route-risk policy and operator-visible status.

This option provides durable recovery after process loss, but it requires new
domain/application state and orchestration contracts, which are outside the
launch-sprint constraint and larger than the current provider wiring.

## Proposed direction and deferred implementation

Keep Option 1 as the proposed minimal direction, subject to independent
review and human acceptance of this ADR. The follow-up implementation must
first add an explicit read-only operation-scoped desired-state snapshot
contract (semantics such as `current_desired_routing_state()`) and then update
the application writers listed above. It must cover downstream compensation,
release/expiry, baseline invalidation on compensation failure, safe
rerun/idempotency, and secret-safe audit before real gateway enablement.

No part of this ADR should be read as implementing those changes. Until this
ADR is accepted and its follow-up implementation is independently reviewed,
real production `xray_file` selection remains architecturally gated.
