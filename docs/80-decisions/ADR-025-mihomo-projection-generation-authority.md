# ADR-025: Mihomo Projection Generation Authority

- Status: Accepted candidate / architecture decision awaiting exact-head review and human merge
- Date: 2026-09-18
- Scope: S04-B2 Mihomo projection-generation authority and freshness boundary
- Supplements: ADR-023

## Decision summary

The current schema has no durable globally monotonic revision representing the
complete Mihomo desired projection. S04-B2 must not fabricate
`DesiredForwarderState.snapshot_revision` from unrelated domain versions,
timestamps, runtime state, cache metadata, process state, or hashes.

The accepted architecture is an append-only, DB-allocated generation stream:

`mihomo_projection_generations`

The future table is evidence/version metadata only. It does not become a
second desired-state authority and must not contain the full desired
configuration. The authoritative inputs remain their owning committed DB
records. Recovery always performs a fresh DB read, rebuilds the canonical
source manifest, recomputes its fingerprint, and compares that result with
the generation metadata.

This ADR is architecture-only. It does not add the ORM model, migration,
loader, secret resolver, runtime readback, candidate validation, recovery
service, registry wiring, deployment, or runtime activation. Those belong to
S04-B2-B after this ADR is independently reviewed and human-merged.

## 1. Existing blocker

The current repository facts are:

- `DesiredForwarderState.snapshot_revision` defaults to `1` and is not an
  authoritative durable revision.
- `TransportVersion` is scoped per `route_group` and represents only the
  transport domain.
- `EgressVersion` is scoped per `route_group` and represents only the egress
  domain.
- `Secret.revision` is scoped per secret.
- `updated_at` timestamps are not a globally monotonic durable authority.
- runtime/current config, backup, cache, candidate, and runtime fingerprints
  are evidence or materialized inputs, never desired-state authority.

Therefore a real S04-B2 loader cannot be implemented safely until the
generation contract below is accepted.

## 2. Append-only global projection generation

The planned persisted model is `mihomo_projection_generations` with at least:

- `revision`
- `manifest_version`
- `desired_fingerprint`
- `created_at`

`revision` is a positive, durable, globally monotonic integer allocated by the
database. Gaps are allowed. A revision is never reused for a later different
projection. It is never derived from time, a hash, runtime state, cache state,
or an in-memory counter.

`manifest_version` identifies the canonical source-manifest algorithm. A
future canonicalization change must be able to fail closed rather than silently
reinterpret an old generation.

`desired_fingerprint` is a deterministic, secret-safe, lower-case canonical
SHA-256 hex digest of the canonical projection source manifest. It is not
runtime evidence and is not a digest of the current file.

`created_at` is audit metadata only and never the revision authority.

## 3. Generation metadata is not desired-state authority

The generation row must not contain authoritative full desired JSON/YAML and
must never be used to reconstruct desired state. Owning committed DB records
remain authoritative for every input. A generation records only non-secret
identity, version, and fingerprint evidence.

The forbidden direction is:

`generation row -> reconstruct desired state`

The required direction is:

`fresh owning DB rows -> canonical manifest -> fingerprint -> compare with generation`

Persisting a complete `DesiredForwarderState` document as a second snapshot
authority is explicitly rejected because it duplicates desired data, creates
synchronization ambiguity, risks stale data and leakage, and violates
ADR-023 source ownership.

## 4. Canonical source manifest

The manifest is deterministic, canonical, and secret-free. It contains every
effective input that can affect the repo-owned Mihomo projection, including:

- enabled/active route groups and Mihomo route bindings;
- traffic rules;
- relevant egress endpoints and bindings;
- listener identity, address, port, and target inputs;
- transport provider/record references;
- validated transport materialization proof metadata;
- proxy and proxy-group identities;
- DNS desired values and policy values;
- deployment constants;
- controller-secret opaque reference and durable `Secret.revision`;
- every other S04-A repo-owned projection input.

Each transport materialization proof includes owner/record identity, provider
code, source revision, cache identity, content hash, and freshness deadline.
The manifest does not fingerprint raw cache contents when the validated
`content_hash` is already the authoritative proof field.

The manifest excludes plaintext controller secrets, plaintext credentials,
subscription URLs/tokens, Job timestamps, unrelated `updated_at` values,
runtime config, current file bytes, backups, health results, and runtime
fingerprints.

Canonicalization rules are explicit: rows use stable DB/business identity
ordering; mappings use canonical key ordering; enums use stable values;
integers remain integers; booleans are never accepted as integer revisions;
semantic datetimes use canonical UTC representation; Python `repr()`, ORM
object serialization, and unordered set iteration are forbidden. The
`manifest_version` is part of the identity of this representation.

## 5. Allocation and commit boundary

Generation allocation occurs only while the existing global Mihomo named lock
is held. The future B2-B procedure is:

1. Fresh locking/current reads of every authoritative input.
2. Verify referenced transport materializations and the controller-secret
   revision.
3. Build the canonical source manifest and compute its fingerprint.
4. Read the latest projection generation using a current/locking read.
5. Reuse its revision only when `manifest_version` and fingerprint exactly
   match.
6. Otherwise insert a new append-only generation row.
7. Persist the generation and identifier-only `MIHOMO_RECONCILE` intent in the
   same preparation transaction.
8. Commit that boundary before any runtime mutation.

No old generation may be updated to a different fingerprint. If the desired
projection changes from A to B and later returns to A, the return receives a
new revision (for example, 10=A, 11=B, 12=A); revision 10 is never reused.

This refines ADR-023 for the current derived projection model. Business
desired rows may already be committed before a projection generation is
prepared. That is safe only because business-row changes alone never mutate
runtime; the first Mihomo activation boundary is the committed generation plus
pending intent, followed by a fresh full fingerprint check. A crash between a
business commit and intent creation leaves DB state authoritative while
startup/scheduled reconciliation later detects the drift and creates the
generation and intent before applying it. This is a liveness gap, not
permission to use runtime as desired state.

## 6. Freshness and concurrency

The named lock serializes Mihomo writers, but it does not replace fresh DB
reads. Row locks use deterministic ordering where practical, and MySQL
REPEATABLE READ session snapshots must not be treated as current authority.

After the preparation commit and before render/apply, the service must
fresh-read all authoritative inputs and require an exact match of
`manifest_version`, generation revision, and desired fingerprint. A mismatch
is `MIHOMO_STALE_DESIRED_SNAPSHOT`: no backup, install, reload, or runtime
mutation is allowed.

Before finalization, it must perform another fresh confirmation. A desired DB
change after apply prevents clean finalization and enters the ADR-023
UNKNOWN/controlled-reconciliation path; it never silently makes a stale
candidate authoritative.

## 7. Rejected revision alternatives

The following are rejected:

- `max(TransportVersion, EgressVersion, Secret.revision)`: separate streams
  are not globally ordered and are incomplete.
- `updated_at`: timestamps are not a global monotonic authority.
- process counters: not durable across processes or restarts.
- cache mtime: cache is not desired-state authority.
- runtime fingerprint: runtime is evidence only.
- MySQL triggers incrementing a singleton for every possible source mutation:
  brittle ownership coupling across bounded contexts and difficult to prove
  complete.
- requiring every unrelated domain writer to manually bump a global counter:
  easy to bypass and couples unrelated domains to Mihomo.
- storing full snapshot JSON/YAML in the generation table: creates a second
  source of truth and leakage/staleness risk.

## 8. Planned B2-B schema

After this ADR is independently reviewed and human-merged, B2-B may add an
additive migration and ORM model equivalent to:

- `revision`: `BIGINT` primary key, database-generated monotonic identity;
  SQLite-compatible integer behavior may be used in tests;
- `manifest_version`: non-null `VARCHAR(32)`;
- `desired_fingerprint`: non-null `CHAR`/`VARCHAR(64)`, validated as lower-case
  SHA-256 hex;
- `created_at`: non-null repository-standard precise UTC timestamp.

There is no unique constraint on `desired_fingerprint`, no snapshot JSON
column, and no secret column. `DesiredForwarderState.snapshot_revision` will
equal `generation.revision`; `snapshot_identity` will be a deterministic,
secret-free derivation of manifest version, revision, and fingerprint. It will
not use a random UUID.

`Secret.revision` remains the purpose-bound controller-secret revision and is
an input to the manifest, not the global projection revision. A changed
controller-secret revision changes the fingerprint and causes a new generation;
this does not authorize hot secret rotation. `TransportVersion` and
`EgressVersion` remain domain-local inputs and are never numerically combined
to fabricate the global revision.

## 9. Recovery and implementation gate

After crash/restart, recovery reads committed desired DB state fresh, rebuilds
the manifest, inspects generations, pending intents, and blockers, and
prepares a new matching generation and pending intent when needed. It never
recovers desired state from runtime, current YAML, backup, cache, or a prior
candidate.

No B2-B implementation may begin before ADR-025 is independently reviewed and
human-merged. B2-B may then implement the ORM model, migration, canonical
manifest builder, allocator, full desired-state loader, SQL secret resolver,
exact runtime readback, candidate validation, and controlled recovery service.

No runtime activation authorization exists. `FORWARDER_PROVIDER=mihomo`
remains non-selectable, and S04-C remains not started.
