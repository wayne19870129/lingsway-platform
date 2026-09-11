# Marzban `routing_principal` source patch (ADR-016 Decision 3, Candidate B)

## What this is

A minimal, reproducible, fail-closed source patch for the pinned Marzban
release this platform depends on, plus the tooling to apply and verify it.
It does **not** build, run, or connect to a real Marzban instance; it does
not implement a real Marzban `AccountingProvider`. See
`docs/82-tasks/TASK-T16-real-provider-registry-wiring.md` (Phase 2B4) and
`docs/80-decisions/ADR-016-route-identity-architecture-unblock.md`
(Decision 3) for the full background and rationale.

## Pinned upstream

- Project: [`Gozargah/Marzban`](https://github.com/Gozargah/Marzban)
  (AGPL-3.0-only; see `Gozargah/Marzban`'s own `LICENSE` file — this patch
  and the vendored fixture below remain subject to that license and are
  **not** relicensed by this repository).
- Pinned tag: `v0.8.4`
- Pinned commit: `7f396db3e703d71a28060bc9ce4a532ec64cb1f4`
- Verified (this PR, exact-source): `app/models/user.py`,
  `app/routers/user.py`, `app/xray/operations.py`, `app/db/models.py`,
  `app/dependencies.py`, and `requirements.txt` are byte-identical between
  that commit and the `v0.8.4` tag ref, confirmed by fetching both refs
  from `raw.githubusercontent.com` and diffing.
- Pinned dependency versions confirmed from that commit's
  `requirements.txt`: `pydantic==2.10.4`, `fastapi==0.115.2`,
  `starlette==0.40.0`, `SQLAlchemy==2.0.36` — matching what
  `docs/80-decisions/ADR-016-route-identity-architecture-unblock.md`
  already recorded; no discrepancy found.

## The problem

Marzban's Xray integration computes the Xray client `email` (the value
`routing.rules[].user` matches against) as
`f"{dbuser.id}.{dbuser.username}"` — confirmed verbatim in the pinned
`app/xray/operations.py` (`add_user`, `remove_user`, `update_user`). The
pinned, publicly supported Admin API response model (`app/models/user.py`
`UserResponse`, used by both `POST /api/user` and `GET
/api/user/{username}`, both declared with `response_model=UserResponse`
in `app/routers/user.py`) never exposes that integer database `id` as a
field. Without it, this repository cannot compute the Xray routing
principal a provisioned account will actually be matched by.

## The patch

`0001-expose-routing-principal.patch` adds, to `UserResponse` only:

- `id: int = Field(exclude=True)` — a normal field, populated via
  `UserResponse`'s existing `from_attributes=True` config directly off the
  ORM object's `.id` attribute (the exact same attribute
  `app/xray/operations.py` already reads), like every other field on the
  class; `exclude=True` only affects serialization, not extraction, so no
  existing field's extraction is touched.
- a `@computed_field routing_principal` property returning
  `f"{self.id}.{self.username}"` — byte-for-byte the same formula
  `operations.py` already uses internally.

This is the mechanism ADR-016 settled on after two prior, non-viable
proposals (a hand-rolled lookalike response model, and a
`model_validator(mode="before")` that would have silently dropped every
other field's `from_attributes` extraction) — see ADR-016 Decision 3 for
the full rejection history. It touches no other class, endpoint, or
field, and makes no promise about `GET /api/users` or webhook payloads
that happen to reuse the same schema (see ADR-016's "scope narrowing").

## How it's applied

`apply_patch.sh <source-tree-root>` applies the patch to a Marzban source
checkout via `git apply`, after a `git apply --check` dry run.
`apply_patch.sh --check <source-tree-root>` runs only the dry-run check —
this is the drift guard: if the pinned upstream `app/models/user.py` ever
changes in a way that no longer matches the patch's context lines (e.g. a
future `MARZBAN_IMAGE_TAG`/pinned-commit bump), both the check and the
real apply fail closed with a non-zero exit and a diagnostic — never a
silent "continue without the patch" fallback, and never a partial apply.
Neither this script nor anything in this repository builds a Marzban
Docker image or fetches the pinned commit itself yet; a future PR that
actually rebuilds/deploys a patched Marzban image is responsible for
cloning/extracting the pinned commit and invoking this script as a
fail-closed build step, and must not proceed past a non-zero exit.

## `vendor/upstream/`

`vendor/upstream/app/models/user.py` is a byte-for-byte copy of the
pinned commit's `app/models/user.py` (verified: `sha256sum` matches what
was fetched directly from
`raw.githubusercontent.com/Gozargah/Marzban/7f396db3e703d71a28060bc9ce4a532ec64cb1f4/app/models/user.py`
during this PR). It exists solely so `apply_patch.sh` and the contract
tests below have a real, pinned target to apply the patch to without a
network call at test time. **Never hand-edit this file** — if the pinned
commit changes, replace it with a fresh fetch of that exact commit, never
a manual diff-driven edit, or the drift guard stops meaning anything.

## Contract tests (`tests/`)

`tests/test_routing_principal_contract.py` actually applies
`0001-expose-routing-principal.patch` (via `apply_patch.sh`, not a
hand-applied edit) to a fresh temp copy of the vendored pinned
`app/models/user.py`, imports the **real, patched** `UserResponse` class
from that file, and mounts it behind two FastAPI routes shaped exactly
like the two real endpoints this repository depends on
(`POST /api/user`, `GET /api/user/{username}`, both
`response_model=<the patched UserResponse>`) — driven through a real
`TestClient` HTTP round trip, not a hand-called `model_validate()`.

Because `app/models/user.py` itself imports several other Marzban modules
(`app.xray`, `app.models.admin`, `app.models.proxy`,
`app.subscription.share`, `app.utils.jwt`, `config`) that pull in a large
amount of unrelated runtime/DB/scheduler bootstrap having nothing to do
with `routing_principal`, the test harness supplies minimal stand-in
stub modules for those imports only — **the patched `UserResponse` class
itself is the genuine upstream file, patched by the genuine patch file;
only its unrelated transitive dependencies are stubbed.** This is
explicitly a `TestClient` harness, not an integration test against a
real Marzban instance, and it is not described as one anywhere in this
PR. The stubs are minimal enough that the two functions they intentionally
never let real `UserResponse` code call
(`generate_v2ray_links`, `create_subscription_token`) raise loudly if
invoked, rather than silently returning a plausible-looking fake value —
so the test harness's own assumptions about which code paths are and
aren't exercised are self-checking.

Also included: `test_patch_drift_guard.py`, which proves the fail-closed
contract with real `git apply` runs (not simulated): applying the patch
to a correct pinned copy succeeds; applying it to a deliberately mutated
copy (simulating upstream drift in the exact context the patch depends
on) fails closed with a non-zero exit and touches nothing.

## Explicitly out of scope here

No real Marzban `AccountingProvider`, no `AccountUserDTO` change, no
orchestration/DTO plumbing, no `registry.py` wiring, no existing-data
reconciliation, no database schema/migration, no real Marzban instance,
credentials, or network calls, no production deployment. The
provisioning named-lock hold-span / `DEFAULT_LOCK_TIMEOUT_SECONDS = 30`
blocker recorded when `backend/app/infra/gateway_route_lock.py` was
added remains open and unrelated to this patch; it must be resolved
separately before real provider wiring, not here.
