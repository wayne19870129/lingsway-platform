# REVIEW-083 — Architecture Audit and Development Roadmap v1

Audit performed for Issue #83. Audit-only: no production code, `AGENTS.md`,
or ADR was modified while producing this document. Reconciled against
`main` as of the SHA recorded in `docs/83-project-continuity.md`
(`94258d4da6ef0477104df0671202e6501f2b9b94`) plus everything merged since,
as observed directly in this checkout on 2026-09-13.

Product goal being audited against (from `ARCHITECTURE.md` §1): a fully
automated, pluggable, migratable subscription distribution platform whose
egress is independent static residential IPs (Webshare), rendered through
an Xray/Mihomo gateway layer, with database-generated configuration and
fail-closed traffic handling as non-negotiable invariants.

---

## 1. Completed capabilities

- **Layering and provider-abstraction skeleton** (ADR-009): `domain/` has
  no `httpx`/`docker`/filesystem/shell imports (`backend/tests/unit/test_domain_boundary.py`
  enforces this); every side effect goes through `providers/` behind
  `Protocol` definitions in `backend/app/providers/base.py`. This is the
  one architectural bet the whole codebase is built on, and it holds.
- **Domain layer implemented and tested against mocks**: `ordering.py`,
  `provisioning.py` (9-step saga with per-step compensation, per
  `ARCHITECTURE.md` §5), `capacity.py`, `quota.py`,
  `subscription_render.py` — all covered by `backend/tests/unit/` and
  exercised through `backend/tests/integration/test_provisioning_phase_boundary.py`.
- **Full API surface for the described product**: `backend/app/api/public.py`
  (register/login/logout/change-password, plans, orders, payment-notice),
  `backend/app/api/admin.py` (orders, customers, egress, capacity, metrics,
  confirm-payment → provisioning, accounting health, subscription usage
  sync), `backend/app/api/subscription.py` (list/details/renew/reset-token/
  render), `backend/app/api/health.py`. Endpoint inventory matches the
  product surface `ARCHITECTURE.md` describes; nothing in the audited route
  list looked like a stub returning fake data.
- **Database models** (`backend/app/models/`): identity, billing,
  subscription, egress, gateway, ops — cover customers/sessions, plans/
  orders/payments, subscriptions/usage periods/versions, egress endpoints/
  bindings/external tenants, gateway route bindings/config versions, and
  secrets/audit log/provision run/alert. Alembic history is append-only
  per `AGENTS.md` rule 7 (no evidence found of a rewritten revision).
- **Guard-test suite** (`backend/tests/guards/`): webshare write-path
  allowlist, capacity-before-payment ordering, Xray safe-reload rollback
  behavior, routing invariants (private→BLOCK first, tcp/udp→BLOCK last,
  no DIRECT fallback), and secret-redaction in log output — i.e. the three
  "iron rules" in `ARCHITECTURE.md` §1 each have a dedicated, currently
  passing regression test, not just a code comment promising the behavior.
- **CI/CD automation is real and enforced**, not aspirational:
  `.github/workflows/ci.yml` runs `policy` (rejects `PROD_*` secret
  references in workflow files), `lint`, `shellcheck`, a dedicated
  `claude-workflow-scripts` regression suite, `backend` (against a real
  MySQL 8.4 service), `frontend` (Next.js build + Docker image build),
  `marzban-contract` (hash-pinned dependency lock + pinned-upstream
  verification for the Marzban routing-principal patch), and `backend-image`
  (verifies the Xray binary/geoip/geosite assets are actually present in
  the built image). `security.yml` and `risk-classify.yml` exist alongside
  it (per `README.md`'s risk table).
- **Background Claude Code Issue→PR automation** (`claude.yml`) has real,
  validated iterations behind it: concurrency/actor-scoping (TASK-T19),
  trust-boundary script isolation and PR-creation/dispatch determinism
  (TASK-T20), pinned rolling-model/effort configuration (TASK-T21). This
  is meta/process infrastructure, not product code, but it is a completed,
  working capability in its own right and is what produced this PR.
- **T5G scheduler jobs**: `backend/app/workers/drift_check.py` and
  accounting/transport-sync failure→`DEGRADED` handling are landed and
  tested (`test_drift_check.py`, `test_scheduler_transport_failure.py`).
  `docs/83-project-continuity.md` §7 already corrects an earlier
  "open" listing for this — confirmed still accurate against current code.
- **CI audit-retry hardening (TASK-T14)**: `npm-audit` in `security.yml`
  retries only on transient 5xx/network failures, bounded, no
  `continue-on-error` — verified directly in the workflow file.
- **IP-availability precheck (TASK-T15)**: a read-only precheck exists in
  order creation (`backend/app/api/public.py`), with the authoritative
  capacity check still enforced at payment-confirmation time — a
  performance/UX improvement that does not weaken the capacity guard.
- **Marzban accounting adapter exists as production-capable code**
  (`backend/app/providers/accounting/marzban.py`): a real HTTP client
  against the pinned Marzban v0.8.4 admin API, with its own offline
  contract tests. This is further along than the provider-registry
  inventory in TASK-T16 initially suggested — see the "Marzban 现状"
  correction embedded in that TASK file and section 6 of the continuity
  doc.

## 2. Partially completed capabilities

- **Real egress provider (Webshare)** — `backend/app/providers/egress/webshare.py`
  has a working write-path guard/allowlist and rate-limit/429-backoff
  logic (tested), but `list_endpoints()` and `capacity()` discard the real
  HTTP response body and return hardcoded empty/zero DTOs, and
  `get_tenant_usage()`/`get_credentials()` raise `NotImplementedError`.
  There is also no `Settings` field to supply a Webshare API key/credential
  to this provider at all. **Cannot be selected today under any
  environment variable** — `registry.py` hard-rejects any
  `EGRESS_PROVIDER` value other than `mock`.
- **Gateway provider (Xray)** — `gateway/xray_file.py` has the safe-reload
  skeleton and one rollback path (post-reload health/preservation check
  failing) genuinely implemented and tested, but `install()`/the first
  `reload()` raising an exception (subprocess failure) currently bypasses
  `restore(backup)` entirely rather than being caught and rolled back —
  a real gap in the "nine-step safe reload" the ADR/ARCHITECTURE.md
  promise, tracked in TASK-T16 phase notes but not yet fixed. `render()`
  also omits `inbounds` from its candidate config, which would falsely
  trip its own preservation check against a real, already-populated
  config. Same as egress: unreachable via any environment variable today.
- **Forwarder provider (Mihomo)** — `forwarder/mihomo.py` has working
  install/hot-reload/rollback-on-failure, but `health()` is hardcoded to
  always report healthy and `apply()` never calls it post-reload, so
  reload "success" is determined solely by "no exception raised," not by
  an actual health probe. Also unreachable via any environment variable.
- **Transport provider (subscription sync)** — `transport/subscription.py`
  performs real outbound HTTP to fetch a subscription URL (which itself
  typically embeds a private token, i.e. this is a credential-bearing
  external call, not the "local-only, no external network" category it
  was first assumed to be — see TASK-T16's self-correction) and writes a
  local cache file. No `NotImplementedError` found, but likewise
  unreachable via any environment variable.
- **Provider-registry wiring end-to-end (TASK-T16)** — this is the single
  biggest "partial" in the repository: `build_registry()`
  (`backend/app/providers/registry.py`) raises
  `ProviderConfigurationError` for every provider variable except its
  mock/noop value. Every "real" implementation above exists as code but
  is **never imported or instantiated by any application code path** —
  only by its own unit/guard tests. Deploying this repository today, with
  any combination of environment variables, results in a platform that
  only ever talks to mocks; Webshare/Marzban/Xray/Mihomo are never
  actually contacted.
- **Split routing / GEOSITE-GEOIP-based traffic classification (TASK-T13)**
  — `CORE_RULES` exist in `subscription_render.py` but are deliberately
  not wired into rendering per ADR-012, pending Phase 0 real-client
  evidence collection into `docs/70-external-facts.md`. Intentionally
  paused on evidence, not abandoned.
- **Deployment automation** — `deploy/bootstrap.sh` and `deploy/lib/*`
  exist per the `ARCHITECTURE.md` §3/§9 structure, but every `deploy-*.yml`
  GitHub Actions workflow runs with `DRY_RUN=true` by design (`README.md`)
  until production Actions secrets are configured — i.e. the automated
  deploy path is built but has not yet been exercised end-to-end against a
  real target the way ARCHITECTURE.md §9's P4 milestone ("run once from
  zero on a disposable Vultr box") requires.
- **Claude-workflow Issue→PR closed loop (Issue #76)** — failure class A
  (`createPullRequest` denial) is fixed and validated; failure class B
  (`action_required` / protected-branch block on `GITHUB_TOKEN`-authored
  PR pushes) is not — same-SHA `workflow_dispatch` CI/Security/Risk
  success does not clear `mergeable_state=blocked`. The durable fix
  (App-token identity instead of `GITHUB_TOKEN`, TASK-T22/Issue #79) is
  fully specified but not yet applied, because it requires
  `.github/workflows/**` write scope this Issue-triggered session does not
  hold.

## 3. Missing production capabilities

- **Payment, notify, email, captcha, storage providers**: `payment/`,
  `notify/`, `email/`, `captcha/`, `storage/` each have **only** a
  mock/noop implementation — no `manual` payment provider file (despite
  `ARCHITECTURE.md` §3 listing `payment/{manual,mock}.py`), no
  `telegram` notify provider, no `resend`/`smtp` email provider, no
  `turnstile` captcha provider, no `r2`/`s3`/`local` storage provider.
  These are architecturally planned (ADR-004 commits to manual payment
  confirmation as policy, not just a placeholder) but have zero real
  implementation code today, not even a partial one. This is a materially
  larger gap than the "four partially-implemented providers" framing in
  section 2 — those four at least have real code; these five categories
  do not exist beyond the interface.
- **Real credential wiring for any provider**: no `Settings` field exists
  to supply Webshare's API key to `WebshareProvider`, and — per TASK-T16's
  own inventory table — nothing wires Marzban's already-present
  `marzban_*` settings fields into an actual `AccountingProvider`
  instance in `registry.py` either, despite the adapter code existing.
- **End-to-end verified backup/restore** — ARCHITECTURE.md §9 places P4/P5
  (bootstrap-from-zero verification, full restore-drill) as higher
  priority than any new feature, and states current recovery capability
  is "theoretically possible," not "verified possible." No evidence in
  this audit that this has since been exercised.
- **Xray reload exception-path rollback** (see section 2) — a `raise`
  from `install()`/first `reload()` currently propagates without
  triggering `restore(backup)`, meaning a genuinely broken candidate
  config, if it errors before it fails validation, is not guaranteed to
  self-heal despite the "nine-step safe reload" design intent.
- **Mihomo post-reload health verification** — `health()` exists but is
  never consulted by `apply()`; a forwarder that comes back up but is
  functionally unhealthy would currently be reported as a success.
- **Zero-manual-approval Issue→PR merge path** — Issue #76 failure class B
  and Issue #79's App-token identity swap remain unapplied; every PR
  produced by this automation currently needs a maintainer to manually
  approve the `pull_request`-triggered check runs before `mergeable_state`
  clears.
- **Full provisioning state machine / automatic retry** — explicitly out
  of scope by design (`README.md`, `ARCHITECTURE.md` §13) until customer
  volume justifies it; flagged here only so this remains a conscious,
  re-checkable decision rather than a silently-forgotten gap.

## 4. Technical risks

- **Highest risk — provider registry mock-only is a silent trust gap.**
  Nothing in configuration, deployment scripts, or runtime behavior
  currently prevents someone from believing a deployed instance is live
  against Webshare/Marzban/Xray/Mihomo when it structurally cannot be —
  `build_registry()` makes this fail loudly at startup today (good), but
  the risk is in whichever future change flips that gate without the
  underlying implementations (webshare response mapping, Xray exception-path
  rollback, Mihomo health check) being finished first. TASK-T16's own
  constraints already require accepting real providers strictly one at a
  time, read-only capabilities before write ones, and guard-test parity
  before any write-capable provider goes live — this ordering must be
  respected literally, not compressed for velocity.
- **Xray/Mihomo failure-path gaps compound the safety-reload promise.**
  The one iron rule most directly protecting existing customers
  ("BLOCK, never DIRECT fallback," 9-step safe reload) has two identified
  gaps (install/first-reload exception bypassing rollback; Mihomo health
  never checked) that would only surface in a real failure, which is
  exactly the scenario the safeguard exists for. These should be closed
  before, not after, `gateway`/`forwarder` are wired into `registry.py`.
- **Credential-handling surface is still mostly theoretical.** No
  `Settings` field exists yet for a live Webshare key; when it is added,
  `AGENTS.md`'s credential-handling rules (no plaintext in code/logs/PRs,
  first-4/`****`/last-4 redaction) need to be verified against the actual
  new field, not assumed from the existing `marzban_*` pattern.
- **Deploy automation is unexercised in DRY_RUN.** `deploy-*.yml` being
  fail-closed is the correct current posture, but it also means the
  automated deployment path itself is unverified against a real host —
  ARCHITECTURE.md's own P4/P5 priority ordering (verify recovery before
  new features) has not yet been satisfied.
- **CI-workflow automation trust boundary depends on continued discipline,
  not a platform guarantee.** `docs/83-project-continuity.md` and
  `CLAUDE.md` are explicit that the 5-round rework cap, SHA-scoping, and
  "never self-merge" rules are self-enforced by the agent, not
  platform-enforced (branch protection on `main` is currently unavailable
  per `AGENTS.md` rule 8). This is a process risk more than a code risk,
  but it is load-bearing for everything else in this list staying safe.
- **Issue #76 failure class B leaves a manual-approval step in every PR's
  path**, which is a friction/velocity risk more than a safety risk, but
  worth tracking since it's the kind of thing that invites a well-intentioned
  but rule-violating shortcut (e.g. widening a token's scope) if left
  unresolved long enough.

## 5. Recommended implementation order

This order optimizes for: closing the safety gaps that block registry
wiring first, then wiring registry providers strictly by TASK-T16's own
read-before-write / one-at-a-time sequencing, then the remaining
provider categories, then deploy verification, with process/automation
fixes threaded in wherever they're already blocking or about to block
something else.

1. **Close the two identified Xray/Mihomo failure-path gaps** (exception-path
   rollback on `install()`/first `reload()`; Mihomo `health()` actually
   consulted post-reload) with new guard tests, *before* either provider
   is wired into `registry.py`. This is the safety-net TASK-T16 itself
   says must be trustworthy before any write-capable provider goes live.
2. **TASK-T16 Phase 2C, read-only capabilities first, per its own stated
   order**: Webshare `list_endpoints()`/`capacity()`/`get_tenant_usage()`
   real response mapping (add the missing API-key `Settings` field first),
   then Marzban accounting wiring into `registry.py` (adapter already
   exists), each with its own integration test before the next.
3. **Issue #76 failure class B / Issue #79 App-token identity swap** —
   requires the repository owner (or a session with `.github/workflows`
   write scope) to apply the already-fully-specified diff in
   `docs/82-tasks/TASK-T22-claude-automation-app-token-identity.md`; queue
   this opportunistically since it's a process fix, not a product
   blocker, but don't let it silently rot given how much of this workflow
   depends on it.
4. **Xray/Mihomo write-path wiring** (gateway `apply()`, forwarder
   `apply()`) into `registry.py`, gated behind the guard-test parity
   TASK-T16 requires, only after step 1's rollback/health fixes land.
5. **Webshare write-path wiring** (`create_tenant`, `update_tenant_quota`,
   `replace_endpoint`) — last of the "four partial" providers by design,
   since it carries real billing/account side effects; must pass the same
   guard-test bar as `test_webshare_guard.py` before being selectable.
6. **Payment / notify / email / captcha / storage real implementations**
   — currently pure interface with zero implementation code. Manual
   payment provider (matches ADR-004's already-decided policy) and
   Telegram notify are the two most directly tied to existing operational
   workflow (`docs/60-runbooks/` assumes Telegram alerting) and should
   come before Resend/Turnstile/R2, which are lower-urgency but still
   needed before this product can be considered feature-complete against
   `ARCHITECTURE.md` §3.
7. **ARCHITECTURE.md §9 P4/P5 — real deploy-from-zero and restore-drill
   verification** on a disposable host, turning `deploy-*.yml` off
   `DRY_RUN=true` deliberately and incrementally, one workflow
   (`deploy-auto` → `deploy-gateway` → `deploy-migration`) at a time,
   each with its own real-host verification before the next is trusted.
8. **TASK-T13 split routing** — stays blocked on its own Phase 0 real-
   client evidence-collection requirement; not implementation-ready
   regardless of everything else above, so it should not be scheduled
   ahead of its own precondition.

## 6. Next Issue/TASK candidates

- **New TASK**: "Xray safe-reload exception-path rollback + Mihomo
  post-reload health check" — implements section 5, step 1. Should be
  filed before any Phase 2C write-path work, since TASK-T16 itself treats
  this as a precondition, not a parallel track.
- **TASK-T16 Phase 2C continuation** (existing task, next phase) — add
  the missing Webshare credential `Settings` field, implement real
  `list_endpoints()`/`capacity()`/`get_tenant_usage()`/`get_credentials()`
  response mapping, and wire Marzban into `registry.py` as the first two
  real, read-only-capable providers.
- **New TASK**: "Manual payment provider + Telegram notify provider
  implementation" — the two provider categories with zero implementation
  code that are most directly required by already-decided policy
  (ADR-004) and already-documented operational runbooks.
- **Issue #79 / TASK-T22 execution** — needs the repository owner (or a
  workflow-write-scoped session) to apply the specified diff and run its
  own live-validation plan; this document does not re-derive that plan,
  it is already written.
- **New TASK**: "Deploy-from-zero verification (ARCHITECTURE.md P4)" —
  exercise `deploy/bootstrap.sh` end-to-end on a disposable VPS with
  `DRY_RUN=false`, followed by a restore-drill (P5), before any further
  feature work is prioritized over this per §9's own stated ordering.
- **Not recommended as a near-term Issue**: TASK-T13 split routing, until
  its own Phase 0 evidence-collection precondition in
  `docs/70-external-facts.md` is satisfied — filing implementation work
  ahead of that would contradict the task's own stated constraints.

---

*This document is an audit snapshot as of 2026-09-13. Per its own
findings above, several referenced facts (provider-registry state,
Issue #76/#79 status) are already tracked as living state in
`docs/83-project-continuity.md` and may move faster than this document
is updated — treat that file, not this one, as authoritative for
current status going forward. This document's purpose is the one-time
audit and roadmap Issue #83 asked for, not an ongoing index.*
