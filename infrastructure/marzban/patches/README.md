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
  (AGPL-3.0-only; see `Gozargah/Marzban`'s own `LICENSE` file). This
  repository's own root `LICENSE` is a blanket proprietary/all-rights-
  reserved declaration with no carve-out for third-party code, so this
  patch set deliberately does **not** commit a copy of Marzban's
  AGPL-3.0-licensed source into the repository — see "Upstream source:
  fetched, not vendored" below.
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

`0001-expose-routing-principal.patch` adds, to `UserResponse`:

- `id: int = Field(exclude=True)` — a normal field, populated via
  `UserResponse`'s existing `from_attributes=True` config directly off the
  ORM object's `.id` attribute (the exact same attribute
  `app/xray/operations.py` already reads), like every other field on the
  class; `exclude=True` only affects serialization, not extraction, so no
  existing field's extraction is touched.
- `routing_principal: str = Field(default="")` plus a
  `@model_validator(mode="after") def compute_routing_principal(self)`
  that sets `self.routing_principal = f"{self.id}.{self.username}"` —
  byte-for-byte the same formula `operations.py` already uses internally.

...and, to `SubscriptionUserResponse(UserResponse)` — the model backing
the customer-facing `GET /{token}/info` endpoint (`app/routers/
subscription.py`) — a `routing_principal: str = Field(default="",
exclude=True)` override, so the DB-id-derived value never reaches a
subscription-token holder.

The `UserResponse` mechanism is a `model_validator(mode="after")` +
regular `Field`, not a `@computed_field` property. ADR-016 originally
recorded a `@computed_field` design; that was corrected during this PR
after discovering it could not be selectively excluded in a subclass
under the pinned pydantic 2.10.4 (`ValueError: you can't override a field
with a computed field`), which is exactly what `SubscriptionUserResponse`
needs to do. See ADR-016 Decision 3's dated correction for the full
history, including the two earlier non-viable proposals (a hand-rolled
lookalike response model, and a `model_validator(mode="before")` that
would have silently dropped every other field's `from_attributes`
extraction). This patch touches no other class, endpoint, or field beyond
the two above, and makes no promise about `GET /api/users` or webhook
payloads that happen to reuse `UserResponse` (see ADR-016's "scope
narrowing").

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

`apply_patch.sh --check` only proves the patch's own context lines still
apply to whatever source tree it is pointed at — it says nothing about
whether the pinned commit's *other* files still form the same contract
this repository depends on. `verify_pinned_upstream.py` is the other,
independent layer: see "Pinned-upstream contract verification" below.

## Pinned-upstream contract verification (`verify_pinned_upstream.py`)

`app/models/user.py` is not the only file this patch's correctness
depends on. `app/xray/operations.py` is where Marzban actually computes
the Xray client email this repository must match
(`f"{dbuser.id}.{dbuser.username}"`) — if a future upstream change alters
that formula without touching `app/models/user.py` at all, the patch
would still apply cleanly (passing `apply_patch.sh --check`) while
`routing_principal` silently stopped matching what Xray actually
authenticates against. Likewise, the contract tests below are only
meaningful under the exact pinned dependency versions they were verified
against (`pydantic==2.10.4`, `fastapi==0.115.2`, `starlette==0.40.0`,
`SQLAlchemy==2.0.36`).

`pinned_upstream_manifest.py` is the single source of truth for the
pinned commit, the three files this contract depends on
(`app/models/user.py`, `app/xray/operations.py`, `requirements.txt`) and
their verified sha256 hashes, the exact Xray email formula, and the
pinned dependency versions — both `verify_pinned_upstream.py` and the
pytest fixtures (`tests/_pinned_upstream.py`) import from it so these
facts are recorded in exactly one place.

`verify_pinned_upstream.py` fetches all three files fresh over the
network and fails closed (non-zero exit, every problem reported, never a
skip) if: any fetch fails, any file's hash no longer matches, the exact
Xray email formula is no longer present in `app/xray/operations.py`, or
any of the four pinned dependency versions is no longer pinned exactly
in `requirements.txt`. Run it directly:

```
python infrastructure/marzban/patches/verify_pinned_upstream.py
```

CI (`.github/workflows/ci.yml`'s `marzban-contract` job) runs this before
the pytest contract suite, so a change to any of these three pinned
files — not just `app/models/user.py` — that breaks the contract fails
the build. `test_pinned_upstream_contract.py` covers this verifier's own
fail-closed behavior (fetch failure, hash mismatch, missing formula,
dependency drift) with real assertions, deliberately breaking one check
at a time via monkeypatching rather than relying on the live network
state to happen to be broken.

This verifier and `apply_patch.sh --check` are deliberately two separate
layers: upstream **contract** verification (do the files this patch
depends on still say what we think they say) and patch **applicability**
verification (does the patch's own diff still apply to the current
target file). Passing one does not imply the other.

## Upstream source: fetched, not vendored

This patch set does **not** commit a copy of Marzban's `app/models/
user.py` into this repository. Marzban is AGPL-3.0-licensed; this
repository's own root `LICENSE` is a blanket "all rights reserved,
proprietary and confidential" declaration with no stated carve-out for
included third-party code, so committing a verbatim copy of an
AGPL-3.0-licensed file under that declaration would be a real license
conflict — not something to resolve by inventing an unauthorized legal
conclusion. Whether and how to formally vendor Marzban source (e.g. with
a proper `THIRD_PARTY_LICENSES` notice) is left to the repository owner
or counsel to decide.

Instead, `infrastructure/marzban/patches/tests/_pinned_upstream.py`
fetches the pinned commit's `app/models/user.py` directly from
`raw.githubusercontent.com/Gozargah/Marzban/7f396db3e703d71a28060bc9ce4a532ec64cb1f4/app/models/user.py`
at test time and verifies its sha256 against the hash recorded during
this PR's upstream research, before any fixture or drift-guard test uses
it. Every fetch **fails closed** (`pytest.fail()`/raised exception, never
a skip or a silent fallback) on a network error or a hash mismatch — an
unreachable network or a changed/compromised upstream file can never be
mistaken for the pinned, verified source. This makes the test suite
non-hermetic (it needs outbound network access at test time; a future PR
that wants a fully offline test run must resolve the license question
first, not route around it), and that non-hermetic-ness is a deliberate,
known tradeoff — the alternative is exactly the licensing risk this
avoids. `apply_patch.sh` itself is unaffected: it still operates
generically on whatever `<source-tree-root>` it's pointed at and does not
know whether that source came from a fetch or a real checkout.

## Contract tests (`tests/`)

`tests/test_routing_principal_contract.py` actually applies
`0001-expose-routing-principal.patch` (via `apply_patch.sh`, not a
hand-applied edit) to a fresh temp copy of the fetched-and-verified
pinned `app/models/user.py`, imports the **real, patched** `UserResponse` class
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
aren't exercised are self-checking. A dedicated test
(`test_empty_links_and_subscription_url_exercise_real_validation_path`)
monkeypatches those two stubs with working fakes and supplies empty
`links`/`subscription_url` to prove the patched model also validates
correctly through the normal, non-short-circuited path.

`test_subscription_user_response_excludes_routing_principal` proves the
patch's second hunk: the same, real, patched module's
`SubscriptionUserResponse` (the model backing the customer-facing `GET
/{token}/info` endpoint) excludes `routing_principal` from its output
while `UserResponse` still includes it — this is the regression test for
the leak this PR's own review process found and fixed (see ADR-016
Decision 3's dated correction).
`test_subscription_info_http_route_excludes_routing_principal` repeats
this check driven through a real FastAPI `TestClient` HTTP round trip
against a route shaped like the real `GET /{token}/info` endpoint, not
just a direct `model_dump()` call, so the same exclusion is also proven
through FastAPI's own `response_model` serialization path.

Also included: `test_patch_drift_guard.py`, which proves the fail-closed
contract with real `git apply` runs (not simulated): applying the patch
to a correct pinned copy succeeds; applying it to a deliberately mutated
copy (simulating upstream drift in the exact context the patch depends
on) fails closed with a non-zero exit and touches nothing. And
`test_pinned_upstream_contract.py`, covering `verify_pinned_upstream.py`
itself — see "Pinned-upstream contract verification" above.

## CI (`.github/workflows/ci.yml`'s `marzban-contract` job)

This entire test suite (patch drift guard + routing_principal contract +
pinned-upstream contract) runs in CI, in its own job — not folded into
the `backend` job, since it needs Marzban's pinned dependency versions
(`pydantic==2.10.4`/`fastapi==0.115.2`/`starlette==0.40.0`), not this
repository's own `backend/pyproject.toml` pins, and running it in the
same job/environment as `backend`'s tests would let one silently
override the other's installed versions. The job runs unconditionally on
every PR (not path-filtered), the same as every other job in this
workflow, specifically so it can never become a required check that sits
permanently pending on a PR that happens not to touch
`infrastructure/marzban/**`. See `docs/10-deploy-new-server.md`'s
"Protect `main`" section for whether/how to add it to GitHub's required
status checks.

Every package the job installs — `pip` itself included — is pinned to an
exact version (`pip==26.2.1`, `pydantic==2.10.4`, `fastapi==0.115.2`,
`starlette==0.40.0`, `httpx==0.28.1`, `pytest==9.1.1`). A "pinned
contract" gate that silently installs whatever the latest `httpx`/
`pytest` happens to be on a given day isn't actually deterministic — a
new release of either could turn this job red for reasons having nothing
to do with Marzban or this repository's own changes. If that ever
happens (or `pip install` itself fails to reach PyPI, or
`verify_pinned_upstream.py` fails to reach `raw.githubusercontent.com`),
treat it the same way as any other flaky-infrastructure failure per
`CLAUDE.md`'s CI-red guidance: rule out a transient network/PyPI issue
with at most one re-run before treating it as a real contract break:
- A hash-mismatch or missing-formula/dependency `FAIL-CLOSED` diagnostic
  from `verify_pinned_upstream.py` is a real contract break — the pinned
  Marzban commit's content or this repo's recorded expectations actually
  disagree — not infrastructure flake.
- A bare network/connection error (fetching from PyPI or
  `raw.githubusercontent.com`) with no `FAIL-CLOSED` diagnostic at all is
  infrastructure flake — re-run once, and only escalate if it repeats.

## Explicitly out of scope here

No real Marzban `AccountingProvider`, no `AccountUserDTO` change, no
orchestration/DTO plumbing, no `registry.py` wiring, no existing-data
reconciliation, no database schema/migration, no real Marzban instance,
credentials, or network calls, no production deployment. The
provisioning named-lock hold-span / `DEFAULT_LOCK_TIMEOUT_SECONDS = 30`
blocker recorded when `backend/app/infra/gateway_route_lock.py` was
added remains open and unrelated to this patch; it must be resolved
separately before real provider wiring, not here.
