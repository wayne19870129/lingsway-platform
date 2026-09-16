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

## Authoritative Xray projection inputs and writers audited

The repo-owned Xray candidate is a projection of more than
`GatewayRouteBinding`. The complete input inventory for this decision is:

- `GatewayRouteBinding`: `gateway_principal`, `outbound_tag`,
  `enabled`, `released_at`, and `egress_id`.
- `EgressEndpoint`: `host`, `port`, `protocol`,
  `credential_secret_ref`, and active/status selection.
- `EgressBinding`: `subscription_id`, `egress_id`,
  `released_at`, and its optional `credential_secret_ref` override.
- The purpose-bound Secret rows referenced by those bindings: the actual
  username/password values are candidate inputs; ciphertext, refs, and
  purpose are ownership/integrity metadata.
- Reality/deployment settings: Reality `privateKey`/`shortIds` resolved from
  the purpose-bound identity, `XRAY_REALITY_DEST`,
  `XRAY_REALITY_SERVER_NAME`, and `XRAY_LOG_LEVEL`, plus the accepted
  process-stable Xray skeleton/constants.

The audit classifies every discovered authoritative mutation as follows.

### Class A — must use the gateway reconciliation contract

1. `SqlAlchemyProvisioningState.allocate_endpoint()` and
   `save_credentials()` mutate `EgressEndpoint`, `EgressBinding`, and
   the referenced Secret during provisioning. Its
   `ensure_gateway_route_binding()` path creates/updates/enables
   `GatewayRouteBinding`.
2. `accounting_sync.release_egress()` mutates `EgressBinding`,
   `EgressEndpoint`, and the active `GatewayRouteBinding` during expiry
   release.
3. Confirmed `ops/reconciliation/gateway_principals.py` updates
   `GatewayRouteBinding.gateway_principal`.
4. Any future endpoint replacement, binding reassignment, credential
   reference/value rotation, or release/disable writer that can alter the
   candidate must enter the same contract, including an operation that
   calls `put_secret()` for an Xray-referenced credential.

The admin provisioning wrapper delegates to the provisioning state; it is not
a second independent writer. `credential_resolver.py`,
`backend/app/workers/drift_check.py`, the Webshare provider, and the admin
queries are read-only for this projection.

### Class B — setup/bootstrap only

`bootstrap_reality_identity()` is an explicit create-once setup operation.
Its `get_or_create_secret()` winner semantics never replace an existing,
malformed, wrong-purpose, or unreadable identity. It may run before real
gateway enablement, but any future identity rotation or repair is not
bootstrap and must use Class A's contract.

### Class C — future provider or operational path

The current Webshare provider exposes inventory reads; no production endpoint
replacement or credential-rotation writer is implemented here. Any future
Webshare endpoint replacement, sub-user credential rotation, or upstream
provider SDK writer must be gated by this ADR before real wiring. Likewise,
future operational changes to the Reality destination/server name or Xray log
level that affect a live candidate must use the same durable gate before
production enablement.

### Class D — destructive/manual maintenance

No direct SQL, file, Secret deletion, purge, or manual endpoint-maintenance
writer is part of the current application paths. If one is introduced, it is
a destructive/manual maintenance operation and must require an explicit
operator gate, the same projection-writer serialization, post-change
verification, and secret-safe audit. It may not bypass the reconciliation
contract.

No other current application writer was found in the inspected API, service,
worker, provider, reconciliation, bootstrap, or maintenance paths. Model
definitions and read-only queries are not writers. This inventory supersedes
the earlier GatewayRouteBinding-only wording and must be re-audited before
production wiring if a new writer is added.

The existing named gateway lock is the single-writer serialization boundary
for the entire repo-owned Xray projection, not merely one table. All Class A
mutations must acquire it, and credential/Reality changes must not use an
intersecting lock with an unspecified order. If a future durable worker uses
another internal lock, it must serialize behind this boundary with one
documented global ordering.
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

## Crash-consistency problem

The earlier apply-before-commit description is not crash-safe as currently
implemented and is not sufficient for production. The current concrete
gateway provider can persist an `APPLIED` baseline while applying a candidate
before the surrounding SQL transaction commits. A process crash can therefore
leave fresh DB desired state A, runtime/file B, and baseline B. The writer
guard can see runtime/file/baseline agreement and incorrectly treat that
state as normal. `finally`, an in-memory flag, or best-effort compensation
cannot repair a process that has stopped.

The baseline is projection evidence only. It is never DB authority and is
never used after restart to infer desired state. Recovery must read a fresh
committed DB desired-state snapshot and compare/reconcile from that truth.

## Options considered

### Direction A — Durable staged apply, then post-commit finalization

A crash-safe version of apply-before-commit would need a durable two-phase
projection protocol, not the current `GatewayProvider.apply()` call alone:

1. Persist a durable `PREVIOUS_APPLIED` record for A and a
   `PENDING_APPLY` record for candidate B before the external apply.
2. Apply and verify B while the named single-writer lock remains held.
3. Commit the business DB transaction.
4. Perform an explicit, idempotent finalization that records `APPLIED` B
   only after the DB commit is durable; clear the pending record as part of
   that finalization.
5. On restart, recover from the durable pending/finalization records and a
   fresh DB desired-state read. The recovery path cannot depend on request
   cleanup, `finally`, or process memory.

The current provider saves `APPLIED` from inside `apply()`, before the
application knows whether the DB commit completed. Before implementation, the
ownership must be chosen explicitly: a provider-specific staged/finalize
contract, an application-owned durable projection-state record that makes
provider `apply()` non-finalizing, or an equivalent mechanism. Simply adding
another application call after the current `apply()` is not sufficient.

Direction A can be crash-safe if every state transition and recovery rule is
durable and the external apply/finalize operations are idempotent. It has a
large external side-effect window and still needs explicit compensation when
a later business step fails.

### Direction B — Commit-first durable reconciliation

This direction commits the DB desired state B together with a durable pending
reconciliation record before applying the runtime projection:

1. Under the single-writer lock, compose B from a fresh operation snapshot and
   create an idempotent pending record.
2. Commit DB desired state B and the pending marker.
3. Apply, validate, and verify B; then persist `APPLIED` B and clear the
   pending marker in a separate durable finalization.
4. Customer-facing provisioning success is not returned at step 2. It is
   returned only after runtime/file verification and finalization succeed.
   Between those points the operation is `PENDING`/non-success; an apply or
   verification failure becomes a durable `DEGRADED`/manual state.
5. Retries use the pending operation identity and candidate fingerprint as
   idempotency keys. Recovery always reads fresh DB desired state B, never a
   baseline to reconstruct B, and retries or quarantines the pending work
   deterministically.
6. The named lock covers pending creation, commit, runtime mutation,
   verification, and finalization/recovery. A failed runtime apply is a real
   route-risk event; it is never hidden by claiming the DB commit was
   rolled back.

Direction B has the stronger process-crash property because committed DB
desired state remains the authority and the durable pending record survives
process loss. It requires new durable application/domain state and changes
customer success timing, so it is not implemented by this ADR-only slice.

## Proposed direction and deferred implementation

For crash consistency, this ADR proposes Direction B, commit-first durable
reconciliation, over the currently described Option 1/apply-before-commit
flow. This is a crash-safety decision, not a claim that it is the smallest
implementation. Direction A remains a valid design only after its durable
staging/finalization ownership is explicitly accepted; the current provider
contract cannot be treated as Direction A.

The follow-up implementation must define the durable pending/finalization
state, idempotency key, recovery ownership, success/error states, baseline
promotion rules, lock span, and route-risk/manual policy before enabling a
real gateway. It must not add a new generic gateway framework or provider-
neutral DTO merely to express this contract.

## Process-crash recovery matrix

For every row below, restart authority is a fresh committed DB desired-state
read; the baseline is evidence only. The writer remains fail-closed until the
listed convergence/evidence is complete.

| Crash point | Fresh DB / runtime interpretation | Restart recovery and next-writer rule |
| --- | --- | --- |
| A. Before gateway apply | DB is A; no committed B exists. | Discard/close uncommitted pending work, verify or restore A if needed; no next writer until the lock/recovery check completes, then allow a serialized writer. |
| B. During gateway apply | DB is A or committed B depending on the selected direction; runtime is unknown. | Mark pending/degraded, never trust a partial apply or baseline; read DB fresh, verify/restore its projection, and block writers until convergence or manual clearance. |
| C. Runtime B applied but baseline/finalization is not durable | Runtime B is untrusted evidence; baseline may still say A. | Fail closed, read fresh DB, apply/verify the DB-authoritative candidate, then finalize its marker; no writer until this is durable. |
| D. Runtime B and baseline B exist but DB commit is incomplete | Baseline B cannot promote B to authority; fresh DB is A. | Fail closed and converge runtime/file to A, quarantine the stale B evidence, then allow the next serialized writer only after A is verified. |
| E. DB commit B is complete but baseline finalization is incomplete | Fresh DB B is authoritative; pending B survives. | Retry the same idempotent apply/verify/finalize for B, or enter durable DEGRADED/manual; no later writer bypasses the pending record. |
| F. DB rollback A is complete but runtime restore is incomplete | Fresh DB A is authoritative; runtime may be B/unknown. | Keep fail-closed pending-restore state, restore/verify A, and block all writers until A is evidenced. |
| G. Compensation apply crashes | DB authority is the fresh committed state (normally A after rollback); compensation result is unknown. | Treat compensation as incomplete, re-read DB, retry idempotently or require manual intervention, and permit no writer while the projection is uncertain. |
| H. Compensation succeeds but final marker is not persisted | Runtime A may be correct but the projection evidence is incomplete. | Fail closed, verify A from fresh DB, persist the final APPLIED evidence, then release the writer gate; no writer may rely on the unpersisted assumption. |

No recovery branch may infer desired state from `APPLIED`, runtime,
filesystem, or a candidate fingerprint alone.

## Deferred implementation boundary

This ADR remains `Proposed`. It does not implement
`current_desired_routing_state()`, a new domain protocol, gateway finalize
API, durable reconciliation job/schema, provisioning compensation, release
orchestration, or any other reconciliation contract. Those changes require a
separate implementation slice and independent exact-head review.