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
Defaults are mock/noop. Accounting mock/marzban and gateway mock/xray selection
boundaries exist; Subscription registry wiring is implemented by S03-B, which is
already merged. Mihomo
activation, production deployment, and real-provider production authorization
remain independent gates. Mihomo has an accepted full-config and activation
boundary in ADR-023, but implementation, durable activation/recovery, and
production readiness remain separate work. Transport subscription cache
materialization is restricted input, not Mihomo activation — and per ADR-025
§3a it is only usable once anchored by a committed DB receipt; the cache file
is never authority for its own contents.

## 5. Current phase and S03 boundary

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
- **S04-B:** ACTIVE.
- **S04-B1:** COMPLETE / merged from `task/s04-b1-mihomo-durable-reconciliation`.
  Merge commit: `c494ade7d40419afd1a812c8b5ddefb154b1c14c`. It adds only
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

- **S04-B2-A:** ACTIVE on `task/s04-b2-mihomo-runtime-reconciliation`, PR #128,
  architecture/documentation-only. It delivers **ADR-025**
  (`ADR-025-mihomo-projection-generation-authority.md`) and the S-series
  implementation TASK (`TASK-S04-mihomo-activation.md`). No Python, no ORM,
  no migration.
- **S04-B2-B:** **NOT STARTED.** Two gates, both required: ADR-025's status
  line reads `Accepted` (PR #128 merged by the User), **and**
  `TASK-S04-mihomo-activation.md` is merged. `TASK-T16` is explicitly **not**
  the execution vehicle — its allowed-file list does not authorize
  `backend/app/models/`, `backend/app/infra/`, or
  `infrastructure/alembic/versions/`.
- **S04-C:** **NOT STARTED.** `FORWARDER_PROVIDER=mihomo` remains NOT
  selectable; no deployment authorization exists.

### Two durable blockers S04-B2-B must close (do not re-derive)

1. **Generation-revision blocker.** There is no durable globally monotonic
   revision for one complete Mihomo desired projection.
   `DesiredForwarderState.snapshot_revision` defaults to `1` and is
   caller-supplied; `TransportVersion` and `EgressVersion` are per-`route_group`
   domain-local; `Secret.revision` is per-secret; `updated_at` is not globally
   monotonic. The revision must not be fabricated from timestamps, hashes,
   process counters, cache mtime, runtime state, or combined domain versions.
   Accepted contract: append-only DB-allocated `mihomo_projection_generations`
   (ADR-025 §2, §8.1).

2. **Durable materialization-receipt blocker.** The subscription cache file has
   **no DB anchor**. `SubscriptionTransportProvider.sync_nodes()` writes it
   atomically (`write`→`flush`→`fsync`→`os.replace`), and
   `refresh_provider_inventory()` then commits endpoint inventory, capacity,
   and status — but nothing committed carries `content_hash`, `cache_identity`,
   `source_revision`, or `freshness_deadline`. `TransportMaterialization`
   (which holds exactly that proof tuple) exists only as an in-memory dataclass
   in `providers/forwarder/mihomo_projection.py`. Consequence: a loader that
   hashed whatever file is on disk would let a modified/torn/out-of-band cache
   authenticate itself. Accepted contract: a receipt produced **only** by a
   successful sync, committed in the same transaction as that sync's inventory,
   after the atomic file write; new file + old receipt ⇒ hash mismatch ⇒
   **fail closed**, never auto-trusted and never re-minted from the file
   (ADR-025 §3a, §8.2).

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
