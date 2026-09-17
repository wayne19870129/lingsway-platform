# Project Continuity / Handoff

**Authoritative main/base:** `6162fefaa63639bf896daad8349dbe883baaa298`

## Current state

- **S02:** COMPLETE.
- **S03:** ACTIVE.
- **S03-A:** Transport multi-provider architecture is current and documented in
  [ADR-024](80-decisions/ADR-024-transport-multi-provider-selection-ownership.md).
- **S03-B:** Registry wiring is blocked pending independent acceptance of S03-A.

## Active boundaries

S03-A is documentation-only. It preserves multiple simultaneous subscription
transport records and requires deterministic record-code mapping, opaque
secret-reference resolution, per-record cache isolation, registry-owned client
lifecycle, zero-I/O construction, and per-record scheduler failure isolation.
Subscription cache materialization is not Mihomo activation; no install,
reload, apply, deployment, credential use, or real provider request is part of
this work.

Defaults remain mock/noop. No agent merges a PR; CI, Security, Risk, and
independent exact-SHA review must be evaluated on the final head before human
acceptance.
