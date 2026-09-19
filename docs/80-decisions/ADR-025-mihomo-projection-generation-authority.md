# ADR-025: Mihomo Projection Generation Authority

- Status: **Accepted**
- Date: 2026-09-18
- Scope: S04-B2 Mihomo projection-generation authority, transport
  materialization receipt authority, and freshness boundary
- Supplements: ADR-023 (does not replace or relax its input taxonomy)
- Implementation TASK: `docs/82-tasks/TASK-S04-mihomo-activation.md`

### Status transition

`Proposed` is the correct status while PR #128 is open. The status line is
deliberately **not** written as "awaiting review/merge", because that phrasing
becomes false the moment the PR merges and nothing would prompt anyone to
correct it.

The transition is explicit and owned:

| When | Status | Who records it |
|---|---|---|
| PR #128 open | `Proposed` | this file, as written |
| PR #128 merged by the User | `Accepted` | the first S04-B2-B PR, which must flip this line and update `docs/83-project-continuity.md` in its own first commit, before any implementation commit |

S04-B2-B must not begin implementation while this line still reads
`Proposed`. That is the merge gate, stated once here rather than repeated as
a status string that can silently go stale.

> **Transition recorded 2026-09-19: `Proposed` → `Accepted`.**
> PR #128 is merged, so the condition in row 2 holds. The row assigned the
> flip to "the first S04-B2-B PR … in its own first commit, before any
> implementation commit"; it was done one step earlier still, in a
> spec-only preflight PR that contains **no implementation at all**
> (`claude/s04-b2-b-preflight`). That is stricter than the contract, not
> looser: the gate is now already open when B2-B starts, so its first
> commit cannot be the one that opens its own gate.
>
> **Both B2-B gates are now satisfied**: this line reads `Accepted`, and
> `TASK-S04-mihomo-activation.md` is merged. S04-B2-B is clear to begin.

## Decision summary

The current schema has no durable globally monotonic revision representing the
complete Mihomo desired projection. S04-B2 must not fabricate
`DesiredForwarderState.snapshot_revision` from unrelated domain versions,
timestamps, runtime state, cache metadata, process state, or hashes.

The accepted architecture is an append-only, DB-allocated generation stream:

`mihomo_projection_generations`

The future table is evidence/version metadata only. It does not become a
second desired-state authority and must not contain the full desired
configuration. **Committed DB desired rows remain the sole authority for
*intent*** — but not every manifest input is such a row, and this ADR does not
flatten ADR-023's taxonomy into "everything comes from owning DB records"
(see §3). Recovery always performs a fresh read of every input **at its own
authority**, rebuilds the canonical source manifest, recomputes its
fingerprint, and compares that result with the generation metadata.

This ADR additionally establishes the **durable transport materialization
receipt** (§3a): the subscription cache file on disk is materialized bytes and
must never be self-authenticating. Without a committed receipt to check it
against, a modified, torn, or out-of-band cache file would authenticate
itself simply by being present at reconciliation time.

This ADR is architecture-only. It does not add the ORM model, migration,
loader, secret resolver, runtime readback, candidate validation, recovery
service, registry wiring, deployment, or runtime activation. Those belong to
S04-B2-B, gated on this ADR reaching `Accepted` **and** on
`docs/82-tasks/TASK-S04-mihomo-activation.md` being merged.

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

The planned persisted model is `mihomo_projection_generations` with exactly
these four semantic fields:

- `revision`
- `manifest_version`
- `desired_fingerprint`
- `created_at`

"At least" is deliberately **not** used here (see §8 for the bounded rule on
what further columns are admissible). This is a safety-sensitive table and an
open-ended field list is an invitation to grow it into a second desired-state
source.

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

## 3. Authority taxonomy (ADR-023 §1.2 preserved, not flattened)

An earlier draft of this ADR said the authoritative inputs are uniformly
"their owning committed DB records". That is **wrong and is hereby corrected**:
the same manifest (§4) explicitly includes deployment constants and transport
materialization evidence, neither of which is an ordinary desired DB row. Read
literally, the old wording left B2-B two bad options — either it could not
satisfy the ADR, or it would migrate deployment/cache state into the database
as a second desired source, which ADR-023 forbids.

ADR-023 §1.2's taxonomy stands unchanged. This ADR adds only the read boundary
for each class:

| Class | Authority | Where B2-B reads it | May it define *intent*? |
|---|---|---|---|
| **Committed DB desired intent** | sole intent authority | fresh locking/current read of the owning rows | **Yes — only this class** |
| **Deployment-owned validated constants** | deployment, not DB | validated `Settings` / repo-owned deployment constants, included **verbatim** in the manifest | No — but a change here changes the fingerprint and therefore forces a new generation |
| **DB-anchored restricted transport materialization** | committed receipt (§3a) anchors the bytes | receipt row read from DB + the cache file verified against it | No — materialized bytes only |
| **Runtime / current file / backup / health / fingerprint** | observation only | never an input to the manifest | **Never** |

Two consequences B2-B must not re-derive:

- **Deployment constants are never migrated into the database** to satisfy
  "everything is a DB row". They stay deployment-owned; the manifest carries
  their validated values verbatim so they are covered by the fingerprint.
- **The cache file is not the authority for its own content.** The DB receipt
  is. See §3a.

### Generation metadata is not desired-state authority

The generation row must not contain authoritative full desired JSON/YAML and
must never be used to reconstruct desired state. A generation records only
non-secret identity, version, and fingerprint evidence.

The forbidden direction is:

`generation row -> reconstruct desired state`

The required direction is:

`fresh reads at each class's own authority -> canonical manifest -> fingerprint -> compare with generation`

Persisting a complete `DesiredForwarderState` document as a second snapshot
authority is explicitly rejected because it duplicates desired data, creates
synchronization ambiguity, risks stale data and leakage, and violates
ADR-023 source ownership.

## 3a. Durable transport materialization receipt

### The gap this closes

Verified against the current tree, not inferred from documentation:

- `SubscriptionTransportProvider.sync_nodes()`
  (`backend/app/providers/transport/subscription.py`) fetches the document,
  validates it, and calls `write_provider_cache()`, which **already** writes
  atomically: temp file in the same directory, `write` → `flush` →
  `os.fsync` → `os.replace`, mode `0600`.
- `refresh_provider_inventory()` (`backend/app/workers/transport_sync.py`)
  then persists normalized endpoint inventory, capacity, and provider status,
  and commits.
- **Nothing committed binds those cache bytes to the database.** There is no
  `content_hash`, `cache_identity`, `source_revision`, or
  `freshness_deadline` column on `transport_providers`, on
  `transport_endpoints`, or anywhere else in `backend/app/models/` or
  `infrastructure/alembic/versions/`.
- `TransportMaterialization` — which carries exactly the proof tuple
  (`owner_record_id`, `provider_code`, `source_revision`, `cache_identity`,
  `content_hash`, `freshness_deadline`) — exists **only** as an in-memory
  dataclass in `backend/app/providers/forwarder/mihomo_projection.py`, used by
  S04-A and its tests. It is not persisted.

So if B2-B hashed whatever cache file happened to be on disk at reconciliation
time, that file would authenticate itself. A modified, torn, truncated, or
out-of-band-replaced file would produce a self-consistent `content_hash` and
pass every check. That is exactly the "cache becomes desired authority"
failure ADR-023 §1.2 forbids.

### Producer / commit / crash contract

**The producer of proof is a successful transport sync — nothing else.**
Neither the Mihomo loader nor any recovery path may mint a receipt.

Required order, which B2-B must implement exactly:

1. Fetch and validate the provider document (unchanged).
2. Compute `content_hash` over the **exact bytes about to be written**, before
   they reach the filesystem — never re-read from disk afterwards.
3. Write the cache atomically (`write` → `flush` → `fsync` → `os.replace`).
   This step is already implemented and must not be weakened.
4. **In the same DB transaction** that commits the sync's normalized
   inventory, upsert the non-secret materialization receipt for that owning
   record.
5. Commit.

The receipt is written **after** the file lands and **never before**. A
receipt must never describe bytes that are not yet durable.

### Crash ordering is fail-closed by construction

| Crash point | Durable result | Required behavior |
|---|---|---|
| Before step 3 | old file, old receipt | consistent; reconciliation proceeds on the old generation |
| Between steps 3 and 5 | **new file, old receipt** | **hash mismatch → fail closed.** Never automatic trust, never "the file looks newer so adopt it", never re-mint a receipt from the file. Resolution is a fresh successful sync, which re-runs 1–5. |
| After step 5 | new file, new receipt | consistent |

The middle row is the whole point: **a new file with a stale receipt is
indistinguishable from tampering**, and this ADR requires it be treated as
such. The mismatch is not repaired automatically and does not authorize a
runtime mutation.

### Consumer boundary

The Mihomo loader's only permitted operation is **verification of the current
cache file against the already-committed receipt**:

- read the receipt row (fresh, current/locking read);
- verify `cache_identity` matches the file it is about to read;
- hash the file and require exact equality with the receipt's `content_hash`;
- require the receipt to be within its `freshness_deadline`;
- require `source_revision` to match the secret/source revision the manifest
  is being built against.

The loader **must not** compute a hash of the current file and treat that
value as authority, and must not fall back to a previous cache, a previous
candidate, or the runtime config. Any mismatch, missing receipt, expired
deadline, or unresolvable owner is `MIHOMO_STALE_DESIRED_SNAPSHOT`-class:
no backup, install, reload, or runtime mutation.

The manifest carries the **receipt's** proof fields, not a re-hash of raw
cache bytes — consistent with §4's existing rule that the validated
`content_hash` is already the authoritative proof field.

### Schema consequence, recorded now

This does require a second additive schema change, and it is specified here
rather than left for the implementing agent to invent — see §8's
`mihomo_transport_materializations` table. B2-B's allowed-file list in
`docs/82-tasks/TASK-S04-mihomo-activation.md` covers both migrations.

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

Each transport materialization proof is read from the **committed receipt**
(§3a), not recomputed from the file: owner/record identity, provider code,
source revision, cache identity, content hash, and freshness deadline. The
manifest does not fingerprint raw cache contents, because the receipt's
`content_hash` is already the authoritative proof field and the file has
been verified against it before the manifest is built.

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

1. Fresh locking/current reads of every input, **each at its own authority**
   per §3's taxonomy (DB rows for intent; validated deployment constants;
   committed receipts for materialization).
2. For every referenced transport record: read its committed receipt, verify
   the on-disk cache file against that receipt's `cache_identity` /
   `content_hash` / `freshness_deadline` (§3a consumer boundary), and verify
   the controller-secret revision. A missing or mismatched receipt stops here
   — it is never repaired in this path.
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

After this ADR reaches `Accepted` and `TASK-S04-mihomo-activation.md` is
merged, B2-B may add **two** additive migrations and ORM models.

### 8.1 `mihomo_projection_generations`

- `revision`: `BIGINT` primary key, database-generated monotonic identity;
  SQLite-compatible integer behavior may be used in tests;
- `manifest_version`: non-null `VARCHAR(32)`;
- `desired_fingerprint`: non-null `CHAR`/`VARCHAR(64)`, validated as lower-case
  SHA-256 hex;
- `created_at`: non-null repository-standard precise UTC timestamp.

### 8.2 `mihomo_transport_materializations` (the §3a receipt)

One committed receipt per owning transport record, upserted by a successful
sync in the same transaction as that sync's inventory commit:

- `owner_record_id`: non-null FK to `transport_providers.id`, **unique**
  (one current receipt per owning record; history is not required and is not
  a desired-state source);
- `provider_code`: non-null `VARCHAR(40)`;
- `source_revision`: non-null positive `BIGINT` — the exact secret/source
  revision that produced these bytes;
- `cache_identity`: non-null `VARCHAR(160)`, the opaque cache key; **never a
  subscription URL or token**;
- `content_hash`: non-null `CHAR`/`VARCHAR(64)`, lower-case SHA-256 hex of the
  exact written bytes;
- `freshness_deadline`: non-null repository-standard precise UTC timestamp;
- `created_at` / `updated_at`: non-null repository-standard precise UTC
  timestamps, audit only.

This table holds **no** cache contents, no credential, no subscription URL or
token, and no rendered configuration. It is proof *about* bytes, never the
bytes.

### 8.3 Bounded rule for any further column (replaces "at least")

A later additive column on either table is admissible **only** if it is all of:

1. **non-secret** — never a credential, token, subscription URL, or plaintext;
2. **audit, identity, foreign-key, or evidence** in nature; and
3. **insufficient for reconstruction** — adding it must not move either table
   closer to being able to rebuild a `DesiredForwarderState` without reading
   the owning sources.

Explicitly inadmissible on both tables: any snapshot/document column
(JSON/YAML/blob), any rendered candidate, any cache contents, any secret
column, and any column whose purpose is to let a reader skip the fresh
authoritative read. A column that fails any of the three tests requires a new
ADR, not a judgement call by the implementing agent.

There is no unique constraint on `desired_fingerprint`.
`DesiredForwarderState.snapshot_revision` will
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

Recovery may **never** mint a transport materialization receipt from a cache
file it finds on disk (§3a). If a receipt is missing or mismatched, recovery
fails closed and waits for a fresh successful sync to produce one.

No B2-B implementation may begin until **both** gates are satisfied:

1. this ADR's status line reads `Accepted` (i.e. PR #128 merged); **and**
2. `docs/82-tasks/TASK-S04-mihomo-activation.md` is merged, since B2-B will
   touch `backend/app/models/`, `backend/app/infra/`, and
   `infrastructure/alembic/versions/` — paths no existing TASK authorizes.

B2-B may then implement the two ORM models, two additive migrations, the
materialization-receipt producer, the canonical manifest builder, the
allocator, the full desired-state loader, the SQL controller-secret resolver,
exact runtime readback, candidate validation, and the controlled recovery
service — all within that TASK's allowed-file list.

No runtime activation authorization exists. `FORWARDER_PROVIDER=mihomo`
remains non-selectable, and S04-C remains not started.
