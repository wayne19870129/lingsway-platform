# ADR-024 — Transport multi-provider selection and ownership boundary

状态：Accepted
日期：2026-09-17
范围：`TransportProviderRecord`、Subscription transport registry wiring、scheduler inventory sync

## Context

The transport data model permits more than one enabled `TransportProviderRecord`.
The scheduler therefore processes a set of records, not one process-global
transport. The current scheduler passes the same `registry.transport` object to
every record. A `SubscriptionTransportProvider` is stateful and binds one
`provider_code`, one subscription URL, one cache path, and one in-memory
inventory. `refresh_provider_inventory()` writes the provider's endpoints and
capacity under the record supplied by its caller. Reusing one subscription
instance for several records can therefore associate provider A's inventory
with provider B's database record.

This ADR defines the boundary needed before S03-B. It does not wire a real
provider, fetch a subscription, materialize a cache, or activate Mihomo.

## Decision

### 1. Cardinality and identity

The platform supports multiple simultaneous enabled subscription transport
records. The database record is the authoritative provider identity for a sync
operation. `TransportProviderRecord.code` is the stable, case-sensitive runtime
mapping key; it must be unique among records eligible for runtime selection.
The record's database primary key remains the ownership key for persisted
endpoints, capacity, alerts, and status.

The runtime must not collapse the model to one global subscription provider.
Each selected record gets exactly one concrete provider instance, and each
instance is bound to exactly one record code.

### 2. Deterministic resolver boundary

S03-B will add a registry-owned, immutable transport resolver/factory boundary.
The concrete boundary is:

```text
DB TransportProviderRecord
    -> scheduler-owned immutable TransportProviderDescriptor
    -> registry-owned lazy resolver/factory
    -> one provider instance owned by ProviderRegistry
```

The scheduler reads each enabled record and creates a provider-neutral
descriptor containing only `record_id`, `code`, `kind`, `secret_ref`, and the
validated implementation identifier (if the selected transport mode requires
one). The descriptor is an immutable value, not a SQLAlchemy ORM object. The
generic provider contract never receives a Session, ORM record, or database
query capability.

`build_registry(settings)` constructs the registry and an empty resolver only;
it performs neither DB I/O nor network I/O. The resolver accepts a descriptor
and an operation-scoped secret-resolution capability at the scheduler sync
boundary. It lazily constructs the concrete provider on first resolution,
using the descriptor's identity/configuration and resolving `secret_ref` only
for that explicit operation. It retains every created provider in the
registry-owned resolver so the existing registry `close()` path can close all
owned clients deterministically. No plaintext URL is stored in Settings or in
the descriptor.

The resolver lookup is exact on the full descriptor identity, not code alone.
It must reject, before any sync:

- an unknown record code;
- duplicate configured definitions or duplicate runtime identities;
- a definition whose kind, implementation, secret reference, or cache identity
  is missing, unsupported, or ambiguous; and
- a record mapped to more than one concrete provider; and
- a descriptor whose identity differs from the identity recorded for an
  already-cached provider instance.

There is no mutable “current provider”, ambient context, code-based fallback,
or fallback to mock. A lookup failure is a provider-specific failed sync and
is recorded as degraded while the scheduler continues with other records.

The resolver is the selected minimal boundary. Moving selection into the
scheduler would duplicate provider construction and lifecycle ownership;
changing the generic registry into a DB-aware registry would couple it to
SQLAlchemy records and sessions. The registry-owned resolver keeps database
identity at the scheduler boundary while retaining one process owner for
provider instances and their resources.

### 3. Configuration and secret ownership

`TransportProviderRecord.secret_ref` is an opaque reference to the encrypted
secret-store entry containing the subscription URL (including any token in the
URL). The scheduler or a resolver adapter may resolve that reference only for
the operation that constructs the provider; the plaintext URL is never a
configuration selector, database field, log field, exception detail, repr, ADR,
commit message, or PR body.

S03-B must define the purpose/audience used for secret resolution and must fail
closed if the reference is absent, unknown, or resolves to an unusable value.
No plaintext URL setting is introduced. `PROVIDER_A_SUBSCRIPTION_URL` and
`PROVIDER_B_SUBSCRIPTION_URL` in `.env.example` are legacy names only; they are
not a supported multi-provider runtime mapping and must not be read as a
fallback. The global `TRANSPORT_PROVIDER_MODE` remains the explicit top-level
mode selector: `mock` is the default, while selecting real subscription mode
requires explicit configuration of every enabled record and its secret
reference.

### 4. Cache isolation

Every concrete subscription provider receives a deterministic cache identity
derived from the owning descriptor, not from a shared provider mode or a human
alias alone. The implementation must bind the tuple
`(record_id, code, kind, implementation)` to exactly one cache path under the
configured transport-cache root. The path is created and validated by the
resolver/factory; it is not discovered from the subscription URL.

Provider A may write only its own cache and provider B may read only its own
cache. A mismatch between the record identity, provider code, or cache identity
fails closed before sync. Cache materialization is restricted input for later
consumers; it is not Mihomo activation.

### 5. Resource lifecycle and zero-I/O construction

`ProviderRegistry` remains the process-lifetime owner. Each factory-created
`SubscriptionTransportProvider` owns its own `httpx.Client`; injected clients
remain owned by their injector. The registry must retain every resolved
provider instance exactly once and close every provider-owned client
deterministically in its existing idempotent `close()` path, including when
another provider's close fails. Construction, resolver assembly, and Settings
loading perform zero external network I/O. Subscription HTTP fetches and cache
writes occur only inside the explicit scheduler sync path.

### 6. Runtime descriptor drift

The scheduler rebuilds a descriptor from the current DB row on every sync
batch. A new valid descriptor is lazily given a new owned provider instance.
An unchanged descriptor reuses the exact existing instance. For an existing
`record_id`, any change to `code`, `kind`, `secret_ref`, implementation, or
derived cache identity is incompatible drift: the resolver fails closed for
that record, marks its sync degraded through the existing scheduler failure
path, and does not mutate or silently replace the cached provider. Controlled
replacement requires a process restart or a separately designed replacement
lifecycle before a later implementation; it is not inferred by S03-B.

Disabling a record removes it from the scheduler descriptor set but does not
destroy or reuse its provider during the current process lifetime. Its owned
resources are closed by the normal registry shutdown. Re-enabling a record
with the same descriptor may reuse the retained instance; re-enabling with
incompatible identity/configuration remains fail-closed drift. A changed code
is therefore never allowed to redirect an old provider instance to a new
record identity.

### 7. Scheduler contract and failure isolation

For each enabled subscription record, the scheduler must resolve the concrete
provider using that record's exact code, then call:

```text
refresh_provider_inventory(db, record, resolved_provider)
```

The resolver must guarantee that the provider's bound code and cache identity
match the record before this call. Since the refresh function writes only under
the record argument, this establishes the invariant that record A cannot be
refreshed using record B's endpoints or capacity. The scheduler catches
resolution and sync failures per record, rolls back only that record's failed
transaction, marks it degraded, and continues the batch. One provider failure
must not prevent other records from being refreshed.

### 8. Provider/base boundary

S03-B may add a provider-specific resolver/factory protocol adjacent to the
transport registry boundary. It may not make the generic domain contract
DB-aware. If S03-B needs to modify `backend/app/providers/base.py`, this ADR
explicitly authorizes only the smallest provider-neutral contract change needed
to express provider identity/ownership or lifecycle; it does not authorize
plaintext credentials, SQLAlchemy imports, a global current-provider field, or
Mihomo activation methods. Any broader base-contract change requires a new ADR
or an amendment accepted before implementation.

### 9. Mihomo boundary and defaults

Subscription fetch, parsing, and isolated cache materialization are not Mihomo
install, reload, apply, or activation. S03-A and S03-B must not perform those
operations or claim Mihomo production readiness. Global defaults remain
mock/noop. Real subscription selection is opt-in and fail-closed. Constructing
Settings, ProviderRegistry, or the resolver never performs external I/O.

## Rejected alternatives

1. **One global SubscriptionTransportProvider.** Rejected because its bound
   identity and mutable inventory cannot safely serve multiple DB records.
2. **A mutable current-provider global.** Rejected because scheduler ordering
   or concurrent work could redirect results and cache writes across records.
3. **Scheduler-created unowned providers.** Rejected because it leaks clients,
   duplicates configuration logic, and breaks deterministic registry lifecycle.
4. **Legacy environment URL names as mapping.** Rejected because they are
   plaintext-secret paths with no durable DB identity or fail-closed duplicate
   semantics.

## Consequences and acceptance gates

S03-B must introduce explicit mapping and lifecycle tests before real
subscription mode becomes selectable. The tests must cover multiple records,
unknown/duplicate mappings, per-record cache paths, secret-reference failure,
close ownership, zero-I/O construction, and one-record failure isolation. No
schema migration, production activation, real credential, or network request is
part of S03-A.
