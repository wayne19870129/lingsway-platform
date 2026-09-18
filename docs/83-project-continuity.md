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
boundaries exist; Webshare and Subscription transport have readiness evidence
but remain separately gated for real registry wiring. Mihomo has an accepted
full-config and activation boundary in ADR-023, but implementation, durable
activation/recovery, and production readiness remain separate work. Transport
subscription cache materialization is restricted input, not Mihomo activation.

## 5. Current phase and S03 boundary

- **S02:** COMPLETE.
- **S03:** ACTIVE.
- **S03-A:** Accepted architecture contract in ADR-024. It preserves multiple
  simultaneous subscription records and defines deterministic descriptor-based
  mapping, opaque secret references, per-record cache isolation, registry-owned
  lazy provider lifecycle, zero-I/O construction, and scheduler failure
  isolation.
- **S03-B:** ACTIVE on the dedicated implementation branch and awaiting
  exact-head review. It implements the accepted descriptor-based resolver,
  secret revision/snapshot boundary, lazy registry ownership, isolated cache
  identity, and scheduler record isolation. It must not invent DB-aware generic
  provider contracts, plaintext URL configuration, mutable current-provider
  state, mock fallback, or Mihomo activation.

## 6. Current blockers and carry-over

Before resuming S03-B, independently verify the exact mutable GitHub state and
confirm ADR-024 remains the accepted contract. The implementation produces
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
