# ADR-022 — Gateway desired-state reconciliation and downstream compensation

- Status: Accepted
- Date: 2026-09-16
- Decision owners: repository owner / human approval
- Scope: TASK-T16 Phase 2C3C implementation
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

This ADR is the accepted architecture decision for Phase 2C3C. Its
implementation uses the existing generic Job table and one application/infra
reconciler; it does not add a new provider interface or schema migration.

## Supersession relationship with ADR-017

If this ADR is accepted, it **partially supersedes ADR-017 only for real
gateway reconciliation transaction and finalization ordering**. It supersedes
these four ADR-017 clauses for a real gateway projection mutation:

1. ADR-017's steps 1-9 sharing one eventual commit/rollback is replaced by
   two durable commits: the authoritative desired-state-plus-pending commit,
   followed by the runtime-success/customer-activation finalization commit.
2. ADR-017's rule that gateway apply happens before the success DB commit is
   replaced by commit-first: DB desired state B and pending reconciliation
   are durable before runtime apply.
3. ADR-017's rule that gateway failure after mutation rolls back the same
   business transaction is replaced by DB B remaining authoritative with
   pending retry or DEGRADED/MANUAL state; ordinary gateway failure does not
   roll back B.
4. ADR-017's rule that successful Phase-B business commit occurs only after
   `provision_apply_gateway()` returns is replaced by the explicit runtime
   reconcile and finalization contract below.

All other ADR-017 conclusions remain effective: Phase A external calls do not
hold the projection lock for their full duration; one MySQL named lock remains
the serialization primitive; lock acquisition/release is fail-closed; durable
run/job status ordering remains required; ProviderRegistry owns providers; and
failure handling remains secret-safe. This ADR does not modify ADR-017 itself.

## Authoritative Xray projection inputs and writer classification

The repo-owned full Xray candidate is a projection of these inputs:

- `GatewayRouteBinding`: `gateway_principal`, `outbound_tag`,
  `enabled`, `released_at`, and `egress_id`.
- `EgressEndpoint` candidate fields: `host`, `port`, and `protocol`.
  `status` and `current_count` are allocation/lifecycle state, not direct
  fields of the current full Xray candidate.
- Credential reference selection: active `EgressBinding.credential_secret_ref`
  has priority; otherwise the active endpoint's
  `EgressEndpoint.credential_secret_ref` is used.
- The referenced purpose-bound Secret's actual username/password values.
  Ciphertext, reference, and purpose remain integrity/ownership metadata, not
  candidate values.
- Reality/deployment inputs: Reality `privateKey`/`shortIds` resolved from
  the purpose-bound identity, `XRAY_REALITY_DEST`,
  `XRAY_REALITY_SERVER_NAME`, `XRAY_LOG_LEVEL`, and accepted static
  skeleton/renderer constants.

Projection relevance depends on committed active-route reference. A value made
for a new provisioning attempt is prospective until the authoritative commit
activates a route that references it.

### Prospective Phase A state — not a live Class A writer

`allocate_endpoint()` and `save_credentials()` may create or update
prospective `EgressEndpoint`, `EgressBinding`, and credential Secret rows
while the new data is not referenced by a committed active
`GatewayRouteBinding`. They remain inside the uncommitted business
transaction and do not take the projection lock during Phase A. The same is
true of other preparation steps before the route activation boundary.

The admin provisioning wrapper delegates to the provisioning state rather than
creating a second writer. `credential_resolver.py`, the drift checker, the
Webshare provider's current inventory reads, and admin queries are read-only
for this projection.

### Class A — live or activating projection writers

Once an active route references an input, or the current operation is about to
commit that input as the active route's desired state, the following are Class
A and must use the projection reconciliation contract:

- Active-route mutation through
  `ensure_gateway_route_binding()`, including principal, outbound tag,
  endpoint reassignment, enable/disable, and release.
- `accounting_sync.release_egress()` when it changes an active referenced
  binding, endpoint, or route.
- Confirmed `ops/reconciliation/gateway_principals.py` principal updates.
- Endpoint host/port/protocol changes, selected credential-reference changes,
  referenced credential plaintext rotation, and Reality identity rotation.
- Any operation that activates the prospective Phase A endpoint/binding/Secret
  set and creates the durable pending reconciliation record.

`put_secret()` is context-sensitive: a subscription token or an unreferenced
prospective credential is not itself a live projection write; a value written
to a Secret referenced by an active route is Class A.

### Class B — setup/bootstrap only

`bootstrap_reality_identity()` is an explicit create-once setup operation.
Its `get_or_create_secret()` winner semantics never replace an existing
malformed, wrong-purpose, or unreadable identity. It may run before real
gateway enablement. Any identity repair or rotation is Class A.

### Class C — future provider or operational path

The current Webshare provider exposes inventory reads only. Any future Webshare
endpoint replacement, sub-user credential rotation, or upstream-provider SDK
writer must adopt this contract before real wiring. Inventory-only reads do not
write the projection; a change to an active route's endpoint or credential is
Class A. Future live changes to Reality destination/server name or Xray log
level must also use the contract before production enablement.

### Class D — destructive/manual maintenance

No direct SQL, file, Secret deletion, purge, or manual endpoint-maintenance
writer is present in the current application paths. If introduced, it requires
an explicit operator gate, the same projection-writer serialization,
post-change verification, and secret-safe audit; it may not bypass this ADR.

The existing named gateway lock is the single Xray projection-writer boundary.
It is acquired when a mutation can change the committed/live projection or when
the operation is formally activating prospective inputs. It is not acquired at
the start of Phase A and must not span CAPACITY, ALLOCATE_ENDPOINT,
CREATE_TENANT, STORE_CREDENTIALS, APPLY_FORWARDER, or
CREATE_ACCOUNTING_USER slow external calls. No intersecting lock may be added
without one documented global ordering.

## Direction B — commit-first provisioning state machine

Direction B is the selected crash-safe architecture. The normal sequence is:

1. Phase A prepares prospective resources: CAPACITY, ALLOCATE_ENDPOINT,
   CREATE_TENANT, STORE_CREDENTIALS, APPLY_FORWARDER, and
   CREATE_ACCOUNTING_USER. The new route is not yet committed live Xray
   desired state. Prospective endpoint/binding/credential rows remain in the
   same uncommitted business transaction and do not take the projection lock.
2. Before the gateway boundary, generate and store the subscription token or
   other required pure DB activation state. The customer must not receive its
   URL yet. This removes the new token-after-runtime compensation window.
3. Acquire `gateway_route_binding_write()`. While holding it, reread fresh
   committed projection truth, establish the active GatewayRouteBinding for B,
   compose the prospective post-mutation desired state, and create the durable
   gateway reconciliation `PENDING` record.
4. Commit once. This first authoritative commit makes all of the following
   durable DB truth B together: Phase A prospective endpoint/binding/Secret
   state, active GatewayRouteBinding, required token state, and the pending
   reconciliation record. DB B is not rolled back to A for an ordinary
   post-commit gateway failure.
5. Still under the projection lock, reread the freshly committed DB B and
   construct DesiredRoutingState. Then call `gateway.render()`,
   `gateway.validate()`, `gateway.apply()`, health checks, and repo
   projection verification. `runtime/file = B` and `APPLIED B` are valid
   only after these checks succeed; APPLIED remains projection evidence, never
   DB authority.
6. Perform the second durable finalization transaction: the gateway
   reconciliation Job becomes SUCCEEDED, Subscription becomes ACTIVE, Order
   becomes ACTIVATED, and
   other customer-facing success state is committed together. Only after
   this commit may the run be SUCCEEDED, a subscription URL be returned, or
   customer success be reported.
7. Release the projection lock. Only then perform best-effort NOTIFY.

After the first commit, Subscription/Order remain PROVISIONING plus durable
`GATEWAY_RECONCILIATION_PENDING` (or an equivalent explicit state). An
accounting user, egress tenant, and forwarder may already exist; that is
intentional and recoverable. Customer success is withheld until finalization.

## Runtime failure and compensation semantics

If runtime apply, health, or projection verification fails after DB B commits,
DB B remains authoritative. The operation enters `PENDING_RETRY` or durable
`DEGRADED/MANUAL` according to error classification. Recovery rereads fresh
DB B, re-renders from DB, and retries idempotently; it never reconstructs B
from baseline, infers desired state from runtime, or blindly rolls back to A.

A retryable gateway failure must not automatically disable the already-created
accounting user while DB B still references it. Explicit cancellation, operator
abort, or terminal destructive compensation is a new serialized projection
mutation. It may disable the accounting user, release the egress, and remove
or disable the desired route only through this same contract.

If finalization commit fails, fresh DB still shows B plus pending state and
runtime may already be B. Restart/recovery verifies or reapplies B and retries
finalization; it never restores A merely because the finalization transaction
was interrupted.

NOTIFY is outside the projection-lock correctness span. Notification failure
cannot change completed DB B, runtime B, APPLIED B, or Subscription ACTIVE.

## Direction B process-crash recovery matrix

Every restart begins with a fresh committed DB desired-state read. Baseline is
projection evidence only. Recovery is fail-closed until the stated evidence
and convergence are complete.

| Crash point | Authoritative interpretation | Recovery and next-writer rule |
| --- | --- | --- |
| A. Before first authoritative commit | Fresh DB is old state A; no committed B exists. | Discard uncommitted work, verify/restore A if needed, finish the lock/recovery check, then allow a serialized writer. |
| B. After DB B + pending commit, before runtime apply | Fresh DB B and pending state are authoritative; runtime is still old/unknown. | Keep pending, apply/verify B idempotently, and block later writers until finalization or durable degraded/manual handling. |
| C. During runtime apply | Fresh DB B is authoritative; runtime outcome is unknown/partial. | Fail closed, retain pending/degraded state, reread B, and converge/verify B before any next writer. |
| D. Runtime B applied, baseline/finalization incomplete | Fresh DB B is authoritative; runtime B is evidence pending verification/finalization. | Verify B and persist final evidence/finalization, or enter durable manual state; no writer bypasses pending. |
| E. APPLIED B exists, business finalization not committed | Fresh DB B plus pending is authoritative; APPLIED B is only projection evidence. | Retry finalization after verifying B; do not recover A; block later writers until pending resolves. |
| F. Business finalization committed before lock release/process crash | Fresh DB B and Subscription ACTIVE are authoritative; runtime B is expected but must be checked. | Verify B after restart, repair fail-closed if needed, then release/reacquire the gate only after evidence is durable. |
| G. Retry/recovery crashes | Fresh DB B remains authoritative; pending/degraded recovery state survives. | Resume idempotently from B and the durable operation identity, or require manual intervention; no baseline-derived desired state and no bypass writer. |
| H. Operator cancellation/terminal compensation during projection mutation | Fresh committed DB state and its durable cancellation/pending marker are authoritative. | Serialize cancellation, reread DB, complete disable/release/route mutation and verification, or remain degraded/manual; no concurrent writer enters. |

No row uses apply-before-commit as the normal contract. No recovery branch
infers desired state from APPLIED, runtime, filesystem, or fingerprint alone.

## Deferred implementation boundary

This ADR remains `Accepted`. It does not implement the state machine or any
new reconciliation table/model, enum, schema/Alembic migration,
`current_desired_routing_state()`, new domain protocol, provider finalize API,
provisioning-state/service ordering, token-step move, accounting compensation
change, scheduler worker, or release orchestration. Those changes require a
separate implementation slice after independent review and human acceptance.

## Implementation boundary — Phase 2C3C

The accepted implementation reuses the existing jobs table with
job_type=GATEWAY_RECONCILE. Its payload contains only operation_kind,
subscription_id, order_id, and provision_run_id; tokens, credentials,
candidates, bearer values, and Reality plaintext never enter the payload or
audit detail. Existing attempts/available_at/locked_at fields provide retry,
stale-running recovery, and exhaustion handling.

backend/app/infra/gateway_reconciliation.py is the single application/infra
entry point. It opens a fresh Session per recovery attempt, uses the existing
named lock, rereads committed DB desired state, constructs the existing
operation-scoped credential resolver, calls the existing
GatewayProvider.render(desired, resolver) / validate / apply contract, and
commits finalization only after runtime verification. The backend-api lifespan
starts one bounded background recovery loop that reuses the process-owned
ProviderRegistry and closes deterministically. No ContextVar, global mutable
state, provider-held Session, or plaintext cache is used.

Paid provisioning and expiry release now commit DB B plus a pending gateway
intent before runtime mutation. Post-commit runtime failure keeps DB B
authoritative and schedules retry or durable manual intervention. New Class-A
writers check unresolved gateway jobs under the same projection lock; the
manual principal command fails closed for a real Xray selection until routed
through this reconciler. The production GATEWAY_PROVIDER=xray_file hard gate
remains until the implementation and its independent review prove every
writer and recovery path safe.

Gateway retry and exhaustion state is owned by the GATEWAY_RECONCILE Job,
safe AuditLog entries, and provision_error where customer-facing business
state requires it. Gateway reconciliation does not write
Subscription.reconcile_state or reconcile_attempts; those fields remain
owned by the accounting reconciliation worker.
