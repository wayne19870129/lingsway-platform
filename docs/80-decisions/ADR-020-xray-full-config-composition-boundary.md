# ADR-020 — Xray full-config composition boundary before preservation rewrite

- Status: Accepted
- Date: 2026-09-15 UTC
- Decision owners: repository owner `wayne19870129`; implementation agents must preserve this boundary
- Scope: TASK-T16 Phase 2B preservation/legal-deletion prerequisite
- Supersedes: none
- Refines: ADR-014, ADR-015, ADR-017, and ADR-019 without weakening their ownership, transaction, or secret-handling rules

## 1. Decision summary

The next preservation/legal-deletion slice must compose one complete repo-owned
Xray candidate from fresh desired inputs before validation or installation. The
composition boundary is split deliberately:

1. An application/infra assembler owns operation-scoped data collection and
   lifecycle. It reads the static skeleton, central `Settings`, the persisted
   Reality identity, the fresh `DesiredRoutingState`, and constructs the
   operation-scoped `CredentialResolver`.
2. A pure canonical Xray composer owns deterministic structural composition. It
   overlays the canonical Reality fields, renders resolved outbounds, renders
   routing, preserves the fixed skeleton, and returns a complete JSON object.
3. A provider-neutral `GatewayCandidateRenderer` is the application port for
   rendering. The concrete `XrayCandidateRenderer` is the full-config Xray
   adapter; its `CandidateConfig` content is the complete file that the
   `XrayFileProvider` runtime adapter may test, install, reload, and verify.
   Neither generic domain code nor the provider base contract knows Xray,
   Reality, Marzban, or a filesystem path.

The selected alternative is **Option B: application/infra constructs a
provider-neutral candidate-rendering port and a concrete Xray adapter receives
validated Xray context, implemented through one shared pure canonical composer**.
This keeps data ownership and secret/session lifetime outside generic domain and
base contracts while making the provider/runtime path and standalone ops path
use exactly one composition semantics.

This ADR does not implement the composer, change production Python, wire the
registry, or start the preservation rewrite. Those are the next implementation
steps after this decision is independently reviewed and manually merged.

## 2. Current mismatch that this ADR closes

On current `main` (`866bc63d70f6785cfef5877724e81105ce1d7316`):

- `backend/app/providers/gateway/xray_file.py::XrayFileProvider.render()` emits
  only `routing` and `outbounds`.
- The returned `CandidateConfig` therefore has no `inbounds` or canonical
  Reality values.
- `LocalXrayRuntime.install()` serializes the candidate content and replaces
  the entire `xray_config.json`.
- The dormant provider path is therefore a fragment/full-file mismatch. It has
  not become a live incident because registry selection remains disabled, but
  preservation cannot safely assume that a runtime superset will repair the
  missing inbound/Reality portion.

The standalone `ops/gateway/render_xray_routes.py` already has the useful
full-file direction for the current file-rendering workflow. ADR-020 makes the
full candidate contract canonical for both paths instead of allowing a second
provider-specific meaning for `CandidateConfig`.

## 3. Ownership and input layers

### 3.1 Static template skeleton

`infrastructure/marzban/xray_config.base.json` remains an input to the concrete
Xray adapter, not a generic desired-state source. The composer may copy only
the following explicit allowlist; all other template paths are rejected as
unknown unless this ADR is amended:

| Template path | Canonical owner | Candidate behavior |
| --- | --- | --- |
| `log.loglevel` | `STATIC_TEMPLATE` | copy exactly |
| `inbounds[*].tag` | `STATIC_TEMPLATE` | copy exactly |
| `inbounds[*].listen` | `STATIC_TEMPLATE` | copy exactly |
| `inbounds[*].port` | `STATIC_TEMPLATE` | copy exactly |
| `inbounds[*].protocol` | `STATIC_TEMPLATE` | copy exactly |
| `inbounds[*].settings.decryption` | `STATIC_TEMPLATE` | copy exactly |
| `inbounds[*].streamSettings.network` | `STATIC_TEMPLATE` | copy exactly |
| `inbounds[*].streamSettings.security` | `STATIC_TEMPLATE` | copy exactly |
| `inbounds[*].streamSettings.realitySettings.show` | `STATIC_TEMPLATE` | copy exactly |
| `inbounds[*].streamSettings.realitySettings.xver` | `STATIC_TEMPLATE` | copy exactly |
| `routing.domainStrategy` | `STATIC_TEMPLATE` | copy exactly |
| `inbounds[*].streamSettings.realitySettings.dest` | `SETTINGS` | overwrite from Settings |
| `inbounds[*].streamSettings.realitySettings.serverNames` | `SETTINGS` | overwrite from Settings |
| `inbounds[*].streamSettings.realitySettings.privateKey` | `SECRET` | overwrite from persisted identity |
| `inbounds[*].streamSettings.realitySettings.shortIds` | `SECRET` | overwrite from persisted identity |
| `inbounds[*].settings.clients` | `MARZBAN_RUNTIME` | exclude; never copy or synthesize |
| `routing.rules` | `DB` + `RENDERER_CONSTANT` | replace with fresh desired rules and fixed BLOCK rules |
| `outbounds` | `DB` + `RENDERER_CONSTANT` | replace with fresh desired outbounds and BLOCK |

`STATIC_TEMPLATE` is limited here to the listed fixed wire-up/default fields;
it does not own Reality identity or deploy values. `DB` owns desired route and
egress definitions, `SECRET` owns the persisted Reality identity,
`SETTINGS` owns Reality deployment values, `RENDERER_CONSTANT` owns the BLOCK
outbound/rules, and `MARZBAN_RUNTIME` owns dynamic clients. The composer must
validate the template against this projection before composing. A future
template key, or a current key not listed above, fails closed and requires an
explicit ownership update in a reviewed ADR; it is never inherited by a broad
`deepcopy(template)` rule.

### 3.2 Central Settings

`Settings.xray_reality_dest` and
`Settings.xray_reality_server_name` are the sole desired sources for Reality
deployment parameters. Blank or whitespace-only values fail closed. Template
or runtime values are not fallbacks.

### 3.3 Persisted Reality identity

The identity remains owned by the Secret infrastructure under:

- secret ref: `gateway/xray/reality-identity`
- purpose: `XRAY_REALITY_IDENTITY`
- payload: exactly `{"privateKey": <nonblank string>, "shortIds":
  [<one or more nonblank strings>]}`

The identity is read through the purpose-bound Secret helper. It is never
regenerated because a read, decrypt, purpose, or payload validation fails. No
identity is read from the template or `xray_config.json`, and no identity is
written to logs, reprs, audit events, PR text, or error messages.

The existing create-once bootstrap remains a separate identity-provisioning
operation. During a route apply, the assembler performs a current read of the
already-owned identity. If the identity is absent or invalid, composition fails
closed; route mutation is not made to depend on an implicit regeneration.

### 3.4 Fresh desired routing

The application/domain boundary obtains a fresh `DesiredRoutingState` after the
route mutation and flush required by ADR-017. It contains the authoritative
route principals and complete outbound DTO set from the current operation
snapshot. It is not derived from `runtime.current()`.

### 3.5 Egress credentials

Egress credential refs continue to be resolved through the explicit
operation-scoped `CredentialResolver` from ADR-019. The resolver is built by
the application/infra boundary with the same provisioning `Session` used for
the fresh route/outbound read. `XrayCandidateRenderer` may use the resolver only for
the duration of `render()`; it never stores it. Plaintext credential material
may exist transiently in the composer/provider call, but DTO reprs, exceptions,
logs, and audit payloads must remain redacted.

The registry constructs neither a resolver nor a cached operation/session
object.

## 4. Provider-neutral and Xray-specific contracts

The generic provider base and domain remain provider-neutral. The approved
generic application port is:

```python
class GatewayCandidateRenderer(Protocol):
    def render(
        self,
        desired: DesiredRoutingState,
        resolver: CredentialResolver,
    ) -> CandidateConfig: ...


class GatewayProvider(Protocol):
    def validate(self, candidate: CandidateConfig) -> ValidationResult: ...
    def apply(self, candidate: CandidateConfig) -> ApplyResult: ...
    def health(self) -> HealthReport: ...
```

This is the explicit future change to `backend/app/providers/base.py`: add the
provider-neutral `GatewayCandidateRenderer` and remove rendering from
`GatewayProvider` if the current lifecycle Protocol still includes it. If a
compatibility version retains `render` on `GatewayProvider`, its signature must
remain exactly provider-neutral (`DesiredRoutingState` plus
`CredentialResolver`), never an Xray-specific type. The domain provisioning
service depends only on `GatewayCandidateRenderer`, `GatewayProvider`,
`DesiredRoutingState`, `CredentialResolver`, and `CandidateConfig`; it never
imports Xray, Reality, Marzban, or filesystem concepts. The implementation PR
must update mocks, constructors, callers, and tests in the same change.

The concrete Xray adapter lives under `backend/app/providers/gateway/` or an
application/infra adapter module and may define the following custom immutable
non-dataclass types. These types must not be exported from `base.py` or used by
generic domain contracts:

```python
class XrayStaticSkeleton:
    # Immutable validated projection of the explicit static allowlist in §3.1.
    ...


class XrayRealityConfig:
    # Immutable validated values; implementation uses private slots and
    # properties, not @dataclass or a generic serializable record.
    ...


class XrayFullConfigInput:
    # Immutable composition input containing XrayStaticSkeleton,
    # XrayRealityConfig, and the generic DesiredRoutingState.
    ...


class XrayCandidateRenderer(GatewayCandidateRenderer):
    def render(
        self,
        desired: DesiredRoutingState,
        resolver: CredentialResolver,
    ) -> CandidateConfig: ...
```

`XrayCandidateRenderer` is constructed per operation by an application/infra
factory with a validated `XrayStaticSkeleton` and persisted
`XrayRealityConfig`. Its public method still satisfies the provider-neutral
port; internally it forms `XrayFullConfigInput`, invokes the shared pure Xray
composer, resolves credentials through the explicit resolver, and returns a
complete candidate. The process/application-lifetime registry may hold the
stateless `XrayFileProvider` runtime adapter, but it must not hold this
operation-scoped Xray context, resolver, Session, or plaintext.

`XrayRealityConfig` is normative as a custom immutable non-dataclass. Its
secret-bearing properties (`private_key`, `short_ids`) are available only to
the composer through explicit accessors, while its `repr()` returns redacted
fields, for example `private_key=<redacted>` and `short_ids=<redacted>`; it must
never include actual secret values. Because it is not a dataclass,
`dataclasses.asdict()` must reject it rather than generically serializing it.
`XrayFullConfigInput` must use the same custom safe-repr/non-dataclass rule and
must return a redacted representation for nested Reality values. No recursive
`repr`, `asdict`, logging, exception, audit, or PR serialization may expose
Reality identity or resolved credential material.

The assembler's lifecycle is:

```text
same operation Session
  -> read and validate explicit XrayStaticSkeleton projection
  -> read Settings Reality deploy values
  -> current-read persisted Reality identity
  -> fresh DesiredRoutingState
  -> construct operation-scoped CredentialResolver
  -> XrayCandidateRenderer.render(DesiredRoutingState, resolver)
  -> complete CandidateConfig
```

No SQLAlchemy `Session`, ORM model, Secret row, raw Secret ref lookup, or
provider-specific DTO is placed in a generic domain/base contract. The
operation-scoped resolver is still created and owned at the application/infra
boundary exactly as ADR-019 requires; the registry never constructs or caches
it.

### 4.1 Portability rule

Generic domain code and `backend/app/providers/base.py` must not mention
Xray-specific DTOs, Reality, Marzban paths, VPS vendors, hard-coded hostnames
or domains, Webshare-specific types, or concrete filesystem deployment paths.
Replaceable infrastructure enters through Protocols, concrete providers,
adapters, Settings/configuration, and operation-scoped factories. A future
gateway engine or vendor must be implementable by adding/replacing its
concrete adapter and its composition context without changing core business
logic or generic candidate contracts. This extends ADR-009's replaceability
rule; it does not authorize a repository-wide rewrite in this docs-only slice.

## 5. Canonical composition semantics

The implementation must add one pure module, proposed as
`backend/app/providers/gateway/xray_composition.py`, with a function named
`compose_xray_full_config(input, resolver) -> Mapping[str, object]` or an
equivalent method on `XrayConfigComposer`. The module may depend on
Xray-specific DTOs and the provider-neutral resolver Protocol, but not on
generic `providers/base.py` or domain changes that introduce Xray types. It
must not perform SQLAlchemy, filesystem reads, environment reads,
subprocesses, network calls, or logging of values.

The composer must:

1. Validate the input skeleton against the explicit allowlist in §3.1 and
   construct a new output object from those listed paths; do not recursively
   inherit unknown template keys.
2. Locate and validate the fixed Reality inbound skeleton.
3. Overlay `Settings`-derived `dest`/`serverNames` and persisted identity
   `privateKey`/`shortIds` from `XrayRealityConfig`.
4. Resolve every desired egress credential ref through the supplied resolver
   and render the complete desired outbound set.
5. Render all desired user route rules and the fixed BLOCK rules.
6. Return a complete candidate object containing only the explicit static
   projection, canonical Reality fields, and canonical routing/outbounds.

The static projection is an allowlist, not a permissive copy operation. A
new template field is an error until its owner and behavior are explicitly
recorded in an ADR. `inbounds[*].settings.clients` is deliberately omitted
because its owner is `MARZBAN_RUNTIME`; its absence from the repo candidate is
not a request to regenerate or preserve dynamic clients.

The renderer owns these hard invariants:

- `geoip:private` BLOCK rule is first;
- user route rules are deterministic and reference only desired outbounds;
- `tcp` and `udp` BLOCK rules are last;
- no `DIRECT` fallback is emitted;
- BLOCK uses the Xray blackhole outbound;
- no duplicate principal or outbound tag is accepted;
- a deleted desired route/outbound is absent from the candidate.

`CandidateConfig.content` from this path is therefore a full desired file,
never a routing fragment and never a runtime merge result.

## 6. Why the alternatives are not selected

### Option A — provider owns full composition and all reads

Rejected as the ownership boundary. It would make `XrayFileProvider` read
Settings, files, DB/ORM state, and Secret infrastructure, or retain an
operation Session. That violates the provider/domain/infra split and ADR-019's
no-Session/no-ORM provider rule. A provider can own the pure rendering
algorithm, but not the data-access lifecycle.

### Option B — application/infra builds typed full input, concrete Xray adapter renders it

Selected. It gives the assembler one operation lifecycle and keeps route,
Settings, static-skeleton, and Secret reads explicit. The generic application
port receives only `DesiredRoutingState` and `CredentialResolver`; the concrete
`XrayCandidateRenderer` receives its validated Xray context at operation scope,
forms the Xray-specific input, and returns the full candidate. The shared pure
composer makes this choice deterministic rather than duplicating a second
renderer, while `GatewayProvider` remains replaceable for non-Xray gateways.

### Option C — merge `runtime.current()` into the candidate

Rejected. Runtime state is not desired state under ADR-014/015/019. A runtime
merge would preserve stale deleted routes/outbounds, can reintroduce old
Reality values, and makes legal deletion impossible to prove. It also risks
copying Marzban-managed dynamic clients into a repo-owned candidate.

### Option D — standalone ops renderer remains the only full composer

Rejected. That would leave hot provider apply with a fragment contract and
would make `GATEWAY_PROVIDER=xray_file` unable to test/install/reload the same
configuration that the ops workflow writes. Standalone rendering may remain an
entrypoint during migration, but it must call the canonical assembler/composer.

### Option E — runtime adapter owns a second composition layer

Rejected. `LocalXrayRuntime` owns backup, install, reload, health, rollback, and
post-reload observation only. It must not parse desired DB/Secret state or merge
runtime fields. A second runtime composer would recreate the exact mismatch
this ADR removes.

## 7. Provider and standalone-ops relationship

There is one canonical pure composer and two thin orchestration adapters:

- The application/infra factory constructs an operation-scoped
  `XrayCandidateRenderer` with validated `XrayStaticSkeleton` and
  `XrayRealityConfig`. Its provider-neutral `render(desired, resolver)` method
  delegates to the pure composer and wraps the resulting full JSON object in
  `CandidateConfig`.
- `XrayFileProvider` is the stateless runtime/application adapter for
  `validate`, `apply`, and `health`. It consumes only the complete
  `CandidateConfig`; it never accepts or installs a routing fragment. If a
  compatibility `render` method remains on this concrete class, it must expose
  only the provider-neutral `DesiredRoutingState`/`CredentialResolver` shape
  and delegate to an operation-scoped `XrayCandidateRenderer`, never expose
  `XrayFullConfigInput` through a generic Protocol.
- `ops/gateway/render_xray_routes.py` remains a standalone operational entry
  point while that workflow exists, but its DB/Settings/Secret collection is
  moved behind the same application/infra assembler and its final structure is
  produced by the same pure composer. It must not retain a parallel
  `render_config()` semantics.

The concrete Xray candidate adapter is the long-term full-config renderer. The
standalone script is a compatibility/operations adapter, not a second source of
truth. The two paths must be covered by parity tests over the same
Xray-specific context and generic desired snapshot. The generic gateway
provider contract remains portable to a different engine or vendor.

## 8. Marzban dynamic-client boundary

Marzban remains the owner of dynamic client lifecycle described by ADR-015.
Dynamic client records and other Marzban-managed runtime fields are not part of
the repo-owned `XrayFullConfigInput`, canonical candidate, or repo-owned
post-reload projection. The composer must not copy them from `runtime.current()`.

The runtime adapter's install contract must therefore be explicit: it installs
the repo-owned full candidate and must not synthesize dynamic clients from a
runtime read. An adapter that cannot apply the full candidate while leaving
Marzban's separately managed client lifecycle intact is unsupported and must
fail closed; `runtime.current()` merging is not an allowed compatibility fix.

The implementation must distinguish these comparison classes:

| Projection class | Owner | Comparison rule |
| --- | --- | --- |
| Routing rules and repo-owned outbounds | repository desired state | exact canonical equality |
| Static inbound protocol/listen/port skeleton | static template | exact canonical equality |
| Reality `dest`, `serverNames`, `privateKey`, `shortIds` | Settings + Secret identity | exact canonical equality |
| Marzban dynamic clients and runtime-only fields | Marzban/runtime | excluded from repo-owned equality; never copied |

If a required repo-owned field is absent or cannot be projected safely after
reload, verification fails closed. It is not silently classified as dynamic.

## 9. Preservation/legal-deletion contract for the next slice

The preservation rewrite must use this algorithm:

1. Acquire the existing named GatewayRouteBinding lock.
2. Perform the route mutation and flush, without an early business commit.
3. Read a fresh desired snapshot using the same operation Session, construct the
   operation-scoped provider-neutral renderer and concrete Xray context at the
   application/infra boundary, resolve credentials, and compose a full
   candidate. The domain sees only the neutral renderer port.
4. Parse the candidate, run all structural invariants, and invoke `xray run
   -test` before installation.
5. Back up the current file, install the complete candidate, reload, and run
   health checks.
6. Read `runtime.current()` only for post-reload observation. Project its
   repo-owned fields and compare them **exactly** with the candidate's
   repo-owned projection.
7. Commit the caller-owned business transaction only after validation, apply,
   reload, and exact projection succeed. On any failure, rollback the business
   transaction and restore/reload the backup using the existing compensation
   behavior.
8. Release the named lock after commit or rollback.

There is no pre-apply runtime superset check. A stale route or outbound that is
absent from fresh desired state must be absent from the candidate and from the
repo-owned post-reload projection. Missing desired routes/outbounds, stale
extras, missing static skeleton fields, changed Reality fields, and malformed
or missing repo-owned Reality state are all failures. Marzban dynamic clients
are evaluated by their separate owner and are not a reason to enlarge the
candidate.

The persisted Reality identity read is a read-only current read in the same
operation Session as the fresh routing/credential snapshot. The existing
create-once identity bootstrap is an independent Secret-provisioning lifecycle
and is not a pre-apply route commit. ADR-017's lock span and caller-owned
commit/rollback boundary remain unchanged.

## 10. Implementation handoff

The next implementation PR must be limited to the following concrete work:

- `backend/app/providers/base.py`: add the provider-neutral
  `GatewayCandidateRenderer` Protocol and keep `GatewayProvider` limited to
  `validate`/`apply`/`health` (or retain only a provider-neutral render
  signature); adapt mocks and call sites together. Do not add Xray DTOs here.
- `backend/app/providers/gateway/xray_file.py` or the concrete Xray adapter
  module: add the custom immutable `XrayStaticSkeleton`, `XrayRealityConfig`,
  and `XrayFullConfigInput` types plus the operation-scoped
  `XrayCandidateRenderer`; these are not generic domain contracts.
- `backend/app/providers/gateway/xray_composition.py`: add the pure canonical
  full-config composer and invariant helpers.
- `backend/app/providers/gateway/xray_file.py`: make the concrete Xray adapter's
  candidate-rendering path delegate to the full composer and make
  validation/apply consume a full `CandidateConfig`; replace runtime-superset
  preservation with the exact post-reload repo-owned projection in a later
  preservation slice.
- `backend/app/domain/provisioning.py` and the application composition
  boundary: depend on the provider-neutral renderer, while the application/
  infra factory assembles fresh desired state, validates the explicit Xray
  skeleton, reads Reality identity, and constructs the same-operation resolver
  without exposing Session/ORM to the provider.
- `ops/gateway/render_xray_routes.py`: use the shared assembler/composer and
  remove duplicate structural semantics while retaining its operational entry
  point until the provider path is live.
- `backend/tests/`: add unit and integration coverage for full candidate
  composition, provider/standalone parity, credential resolution, and the
  exact projection boundary.

Required preservation tests include:

- deleted route and outbound are absent after apply;
- stale runtime route/outbound causes exact projection failure and rollback;
- missing desired route/outbound causes validation failure;
- static inbound protocol/listen/port drift fails;
- Settings Reality `dest`/`serverNames` drift fails;
- persisted Reality privateKey/shortIds drift, missing, malformed, wrong-purpose,
  or unreadable state fails without regeneration or leakage;
- dynamic Marzban client changes are excluded from repo-owned equality and are
  never copied into the candidate;
- invalid candidate fails before reload;
- reload/health/projection failure restores the backup and preserves the
  existing transaction/disable compensation behavior.

## 11. Explicitly out of scope

This ADR does not authorize or include:

- production Python or schema changes in this documentation PR;
- registry wiring or making `GATEWAY_PROVIDER=xray_file` selectable;
- writer-guard/drift reconciliation or deployment/reload on a real server;
- Webshare or Marzban API calls;
- a new Issue, automatic merge, or changes to `AGENTS.md`/workflow policy;
- copying runtime config into desired state;
- changing Reality ownership, Secret ref/purpose, Settings field names, or
  persisted identity semantics from PR #105.

