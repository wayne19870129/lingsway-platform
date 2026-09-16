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
This script itself does not build a Marzban Docker image or fetch the
pinned commit -- `../build_patched_image.sh` (TASK-T16 Phase 2C2) is the
caller that does both: it fetches/verifies the pinned commit into a
temporary directory, invokes `apply_patch.sh --check` then `apply_patch.sh`
as a fail-closed build step (never proceeding past a non-zero exit), and
builds the resulting image with immutable identity labels. See
`../verify_patched_image.py` for the deployment-side counterpart that
verifies those labels before Marzban is started.

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

`verify_local_source_tree.py` (TASK-T16 Phase 2C2) checks the identical
contract against an already-present local checkout instead of fetching
over the network -- used by `../build_patched_image.sh`, which already
has a git-verified pinned-commit checkout on disk before it applies the
patch, so re-fetching the same three files a second time would add
nothing but a redundant network dependency. Both scripts import their
checks (`check_pinned_content()`, `check_dependency_versions()`) from
`pinned_upstream_manifest.py`, so the contract logic itself, not just the
constants, lives in exactly one place.

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

## Reproducible dependency lock (`requirements-contract.in`/`.txt`, `lock_consistency.py`)

Pinning only the five top-level packages this contract cares about
(`pydantic`, `fastapi`, `starlette`, `httpx`, `pytest`) is not actually
reproducible: `pip install pydantic==2.10.4 fastapi==0.115.2 ...` still
lets pip resolve whatever the *latest compatible* version of every
transitive dependency (`anyio`, `httpcore`, `certifi`,
`annotated-types`, `typing-extensions`, `pydantic-core`, `pluggy`,
`packaging`, `pygments`, `iniconfig`, `h11`, `idna`, ...) happens to be
on the day CI runs. A new release of any of those can silently change
what this "pinned contract" gate actually tests, with zero change to
this repository.

- **`requirements-contract.in`** records only the five top-level exact
  pins this contract actually requires — the ones ADR-016/this README
  already state Marzban was verified against.
- **`requirements-contract.txt`** is the fully resolved lock: every one
  of those five packages *and every transitive dependency they pull in*
  (17 packages total as of this writing), each pinned to an exact
  version with `--hash=sha256:...` entries. Generated with:

  ```
  pip-compile --generate-hashes --output-file=requirements-contract.txt \
    --no-header requirements-contract.in
  ```

  using **pip-tools 7.6.1** under **Python 3.12.3** with **pip 26.2.1**.
  **Never hand-edit `requirements-contract.txt`** — regenerate it with
  the exact command above whenever a pin in `requirements-contract.in`
  changes, the same way `vendor/upstream/` (removed) was never
  hand-diffed. The generation command itself talks to PyPI, but CI never
  re-runs it — CI's only authoritative input is the committed
  `requirements-contract.txt`, so a routine CI run needs no PyPI
  metadata resolution, only downloading the exact, already-decided
  artifacts by hash.
- **`lock_consistency.py`** is a fail-closed guard (stdlib only, no
  installed dependencies needed) proving the committed lock hasn't
  silently drifted from `requirements-contract.in` or lost its
  guarantees: every top-level pin from the `.in` file must appear in the
  lock at the same version (missing or version-mismatched → fail); every
  single resolved entry (top-level and transitive alike) must be an
  exact `==` pin (any other operator, or none → fail); every entry must
  carry at least one `--hash=` (missing → fail, since
  `--require-hashes` would otherwise silently accept an unverified
  download for that one package). Run it directly:

  ```
  python infrastructure/marzban/patches/lock_consistency.py
  ```

  `tests/test_lock_consistency.py` covers each of those four failure
  modes individually (plus multiple simultaneous failures, plus a
  positive baseline against the real committed lock) via synthetic
  fixtures, not just by trusting today's real lock to happen to be
  broken or not.

CI installs from the lock with `--require-hashes`:

```
python -m pip install --upgrade pip==26.2.1
python -m pip install --require-hashes -r infrastructure/marzban/patches/requirements-contract.txt
```

`--require-hashes` makes pip refuse to install *anything* not listed in
the lock with a matching hash — no unpinned/unverified fallback is
possible. This solves version/artifact-content determinism, **not**
network availability: it still needs to actually reach PyPI to download
the pinned wheels, the same way `verify_pinned_upstream.py` still needs
to reach `raw.githubusercontent.com`. If either of those network calls
fails, or if a lock consistency or hash-verification failure occurs,
treat it the same way as any other CI-red failure per `CLAUDE.md`'s
guidance: rule out a transient network/PyPI issue with at most one
re-run before treating it as real:
- A `FAIL-CLOSED` diagnostic from `lock_consistency.py` or
  `verify_pinned_upstream.py` is a real contract break — the committed
  lock drifted from `requirements-contract.in`, or the pinned Marzban
  commit's content/this repo's recorded expectations disagree — not
  infrastructure flake.
- A bare network/connection error (fetching from PyPI or
  `raw.githubusercontent.com`) with no such diagnostic at all is
  infrastructure flake — re-run once, and only escalate if it repeats.

## Building and verifying a patched image (TASK-T16 Phase 2C2)

`../build_patched_image.sh` and `../verify_patched_image.py` (siblings of
this `patches/` directory, not inside it) are the build/deployment
counterpart this README's earlier sections said was still a future PR's
responsibility:

- `../build_patched_image.sh [image-tag]` -- fetches the exact pinned
  commit into a temporary directory (`mktemp -d`, deleted on exit),
  verifies the checkout's HEAD against `PINNED_COMMIT`, runs
  `verify_local_source_tree.py`, applies this patch via
  `apply_patch.sh --check`/`apply_patch.sh`, verifies the patch result
  actually exposes `routing_principal`, applies
  `0002-pin-build-setuptools.patch` (see below), then `docker build`s the
  resulting `Dockerfile` with five immutable OCI labels attached
  (`org.lingsway.marzban.upstream-commit`, `-upstream-tag`,
  `-routing-principal-patch`, `-patch-id`, `-contract-version` -- see
  `pinned_upstream_manifest.REQUIRED_IMAGE_LABELS`). Default tag:
  `lingsway/marzban:v0.8.4-routing-principal`. No Marzban source is
  committed to Git by this process.
- `0002-pin-build-setuptools.patch` -- a build-environment
  *compatibility* patch, **not** part of the routing_principal/ADR-016
  contract and not covered by `apply_patch.sh` or its contract tests
  (`build_patched_image.sh` applies it directly via `git apply`, since
  `apply_patch.sh` is hardcoded to `0001`'s own target file). The pinned
  commit's `Dockerfile` runs `pip install --upgrade pip setuptools` with
  no version pin, which resolves whatever setuptools is newest on the
  build day; setuptools `>=81` dropped `pkg_resources`, which the pinned
  `apscheduler==3.9.1.post1` dependency still imports unconditionally
  (via `marzban-cli`'s own import chain), breaking the final
  `marzban-cli completion install --shell bash` build step with
  `ModuleNotFoundError: No module named 'pkg_resources'` -- confirmed by
  reproducing the failure in CI before this patch existed. The patch
  pins that one `RUN` step to `setuptools<81`. See
  `tests/test_build_compat_patch.py` for its fail-closed drift-guard
  coverage (same pattern as `test_patch_drift_guard.py`, against the
  real fetched pinned `Dockerfile`, not a hand-written fixture).
- `../verify_patched_image.py <image-ref>` -- `docker inspect`s a
  candidate image's labels and fails closed (non-zero exit) unless every
  one of `REQUIRED_IMAGE_LABELS` is present with its exact value. Called
  from `deploy/lib/40_stack_up.sh` before Marzban starts whenever the
  deployment's `.env` sets `ACCOUNTING_PROVIDER=marzban` -- including
  against a `MARZBAN_IMAGE` override, which goes through the same check
  with no bypass. An image tag alone (however it's named) is never
  treated as proof; only these `docker inspect`-verified labels are.

This closes the `MARZBAN_PATCHED_IMAGE_DEPLOYMENT_PENDING` deployment
gap this README and `docs/82-tasks/TASK-T16-real-provider-registry-wiring.md`
previously recorded. It does **not** resolve the separate operational
AGPL-3.0 question of what running this (modified, AGPL-3.0-licensed)
image in a real staging/production deployment requires -- see "Upstream
source: fetched, not vendored" above for the vendoring/licensing position
this patch set already takes; the *build-time* fetch-and-patch flow this
section describes stays within that position (nothing is vendored into
Git), but actually *running* the resulting image is a separate question
the repository owner or counsel must decide, tracked as
`MARZBAN_AGPL_DEPLOYMENT_COMPLIANCE_PENDING` in
`docs/83-project-continuity.md`.

## Explicitly out of scope here

No real Marzban `AccountingProvider`, no `AccountUserDTO` change, no
orchestration/DTO plumbing, no `registry.py` wiring, no existing-data
reconciliation, no database schema/migration, no real Marzban instance,
credentials, or network calls, no production deployment. The
provisioning named-lock hold-span / `DEFAULT_LOCK_TIMEOUT_SECONDS = 30`
blocker recorded when `backend/app/infra/gateway_route_lock.py` was
added remains open and unrelated to this patch; it must be resolved
separately before real provider wiring, not here.
