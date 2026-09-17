# Project Continuity / Handoff

**Current authoritative main:** `2445caeacc926013b115e512d53d73213f96a5b7`
(verified 2026-09-17 UTC). PR #116 and Phase 2C3D are complete. This
document is the current continuity index; older SHA, PR, and execution-mode
notes have been removed rather than treated as current state.

## 1. Current collaboration, authority, and resume contract

Authority is **ADR > `AGENTS.md` > REVIEW**. `CLAUDE.md` supplies
day-to-day procedure and reading guidance; it is not an authority tier above
ADR or `AGENTS.md`. TASK files and PRs are the scoped requirement,
implementation, and review evidence for their work.

- The User owns acceptance, manual merge, and production approval.
- Codex performs the scoped implementation work within the task's allowed
  files.
- Claude Code is the repository's standing implementation role where assigned;
  parallel agents must obey each task's file boundary and must not write
  another active workstream's files.
- ChatGPT/ChatGPT Work is the independent exact-SHA reviewer and
  requirements/acceptance orchestrator.

For a fresh session, read in order: `CLAUDE.md`, `AGENTS.md`, this file,
the relevant ADR(s) under `docs/80-decisions/`, the relevant TASK under
`docs/82-tasks/`, the current PR/Issue evidence, then `README.md` and
`ARCHITECTURE.md` when relevant. Reconfirm current `main`, branch head,
changed files, and checks from GitHub; do not rely on prior chat.

## 2. Review, merge, and automation baseline

Independent review is scoped to the PR's exact head SHA. Reviews against a
superseded SHA are stale. CI, Security, and Risk must all be green on that
same head before a PR is ready for human consideration. No agent merges,
force-pushes `main`, or enables auto-merge; the User performs the final
manual merge.

The current background automation baseline is defined by
`.github/workflows/claude.yml` and
`docs/82-tasks/TASK-T24-claude-pr-and-verification.md`: authorized
Issue-comment triggers are actor-scoped, verification is bounded, PR/check
creation is deterministic and serialized, and the workflow does not deploy,
auto-approve, auto-close Issues, or auto-merge PRs. Consult those authoritative
files for implementation details; this index records only the current
boundary.

## 3. Safety, credential, and production boundary

Follow `AGENTS.md` for fail-closed routing, external-write allowlists,
disable-not-delete accounting behavior, safe reload rules, credential
handling, and file/module ownership. Never place plaintext credentials in
code, logs, commits, PR text, or this document. Provider construction and
documentation reconciliation must not contact real external systems. Any
production deployment, provider activation, credential use, or external write
requires the applicable safeguards and explicit User approval; this PR
performs none of those actions.

## 4. Durable technical state

Current main is `2445caeacc926013b115e512d53d73213f96a5b7`. Provider registry
selection is explicit and construction remains zero-I/O. ADR-022 defines the
durable Xray reconciliation boundary; accepted ADR-023 defines the Mihomo
full-config writer and activation boundary. Real-provider readiness is not
the same as production readiness: Webshare and Subscription transport are
implemented/readiness-complete but still not registry-wired, while Mihomo also
requires its implementation and durable activation/recovery work. The provider
matrix below is the current code-backed state.

## 5. Current S02 state

- **S02-A — Mihomo architecture:** Issue #117 is closed/completed; PR #121
  is merged/completed. The Mihomo full-config writer, single-writer,
  freshness, commit-order, rollback, and fail-closed architecture boundary is
  defined by the accepted ADR-023.
- **S02-B — Webshare readiness:** Issue #118 is closed/completed; PR #123
  is merged/completed. Authoritative endpoint, credential, and tenant
  response mappings are implemented; unresolved capacity/usage semantics
  remain explicitly gated.
- **S02-C — Subscription transport readiness:** Issue #119 is
  closed/completed; PR #122 is merged/completed. Construction remains zero-I/O;
  explicit sync, parsing, cache atomicity, lifecycle ownership, and failure
  handling are covered. Mihomo activation remains separate.
- **S02-D — continuity/provider inventory:** PR #120 is the
  only remaining S02 workstream. This PR updates this document against the
  current main above.
- **Next step: S02-I — Integration:** S02 is **not formally complete**. This next step remains inside S02 and
  must define and independently review the serial registry-wiring order and
  activation gates.

S02-A, S02-B, and S02-C were parallel readiness tracks. Their completion does
not authorize production activation. Any later registry wiring must be
provider-by-provider, serial, and independently reviewed; no track may write
another track's files.

## 6. Provider inventory at current main

The registry distinguishes a concrete implementation file from a selectable
provider. Defaults are still mock/noop unless explicitly selected below.
Registry construction remains explicit and zero-I/O.

| Category | Configured selector / concrete implementation | Registry selectable | Completeness / placeholders / architecture blocker | Side effects / credentials / current tests / next safe step |
|---|---|---|---|---|
| egress | Default `EGRESS_PROVIDER=mock`; `MockEgressProvider`, `WebshareProvider` | `mock` only; Webshare not wired | Read mappings are implemented; capacity and tenant-usage semantics remain explicitly gated, not fabricated | Webshare API reads/writes, API key and tenant credentials; tests: `backend/tests/unit/test_webshare_provider.py`, `backend/tests/guards/test_webshare_guard.py`, `backend/tests/guards/test_webshare_procurement.py`; S02-I must review explicit mode/plan configuration before wiring |
| accounting | Default `ACCOUNTING_PROVIDER=mock`; `MockAccountingProvider`, `MarzbanAccountingProvider` | `mock` and `marzban` | Adapter and registry selection are present; no known placeholder; production operation remains gated by credentials/compliance/approval | Marzban HTTP account operations; admin credentials and CA; tests: `backend/tests/unit/test_marzban_accounting_provider.py`, `backend/tests/unit/test_registry.py`; S02-I to verify integration in isolation |
| gateway | Default `GATEWAY_PROVIDER=mock`; `MockGatewayProvider`, `XrayFileProvider`, `MarzbanXrayRuntime` | `mock` and `xray_file` | ADR-022 reconciliation and production selection boundary are implemented; invalid settings fail closed; not a deployment authorization | Xray file/backup and authenticated Marzban control/reload; credentials, CA and Reality identity; tests: `backend/tests/unit/test_xray_marzban_runtime.py`, `backend/tests/unit/test_xray_provider_standalone_parity.py`, `backend/tests/unit/test_registry.py`; S02-I to review activation |
| forwarder | Default `FORWARDER_PROVIDER=mock`; `MockForwarderProvider`, `MihomoForwarderProvider` | `mock` only; Mihomo not wired | Implementation exists; ADR-023 is accepted, but registry wiring and durable pending/finalization/recovery machinery are not complete; not production-ready | Local Mihomo config/reload/health and API secret; tests: `backend/tests/guards/test_mihomo_safe_reload.py`, `backend/tests/unit/test_services_wiring.py`; S02-I after implementation wiring work |
| transport | Default `TRANSPORT_PROVIDER_MODE=mock`; `MockTransportProvider`, `SubscriptionTransportProvider` | `mock` only; subscription not wired | S02-C readiness is complete; sync/cache/parser/lifecycle paths are hardened; activation and registry wiring remain incomplete | External subscription fetch and local cache write; URL/token; tests: `backend/tests/unit/test_transport_provider.py`, `backend/tests/unit/test_registry.py`; S02-I must preserve the no-Mihomo-activation boundary |
| payment | Default `PAYMENT_PROVIDER=mock`; `MockPaymentProvider` only | `mock` only | No external implementation or placeholder selected | No current external side effect; future payment credentials/contract required; tests: `backend/tests/unit/test_provider_failures.py`, `backend/tests/unit/test_registry.py`; define a separately reviewed provider |
| notify | Default `NOTIFY_PROVIDER=noop`; `NoopNotifyProvider` only | `noop` only | Intentional noop; no external implementation | No current external side effect; future delivery credentials/contract required; tests: `backend/tests/unit/test_provider_failures.py`, `backend/tests/unit/test_registry.py`; define separately |
| email | Default `EMAIL_PROVIDER=noop`; `NoopEmailProvider` only | `noop` only | Intentional noop; no external implementation | No current external side effect; future email credentials/contract required; tests: `backend/tests/unit/test_provider_failures.py`, `backend/tests/unit/test_registry.py`; define separately |
| captcha | Default `CAPTCHA_PROVIDER=noop`; `NoopCaptchaProvider` only | `noop` only | Intentional noop; no external implementation | No current external side effect; future verification credentials/contract required; tests: `backend/tests/unit/test_provider_failures.py`, `backend/tests/unit/test_registry.py`; define separately |
| storage | Default `STORAGE_PROVIDER=mock`; `MockBlobStorage` only | `mock` only | Intentional mock; no external implementation | No current external side effect; future object-storage credentials/contract required; tests: `backend/tests/unit/test_provider_failures.py`, `backend/tests/unit/test_registry.py`; define separately |

**Important readiness boundary:** Webshare, Subscription transport, and Mihomo
have implementation/readiness evidence, but none is production-ready merely
because its module exists. Webshare and Subscription still require later
registry wiring; Mihomo additionally requires its implementation and durable
activation/recovery work. No such wiring is included in S02-D.

## 7. S02-D scope, verification, and handoff

This update is documentation-only and changes no code, TASK, ADR, workflow,
config, schema, test, secret, deployment, provider, VPS, or network state.
No real provider or runtime operation occurred. The branch is rebased onto
the current main SHA above and the PR remains OPEN for independent exact-SHA
review. CI, Security, and Risk checks must be green on the final head before
human merge.

