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
   lifecycle. It supplies the process-stable non-secret Xray configuration, the
   fresh `DesiredRoutingState`, and an operation-scoped `CredentialResolver`.
   The concrete Xray resolver capability reads the persisted Reality identity
   on each render; the assembler/provider does not cache that identity.
2. A pure canonical Xray composer owns deterministic structural composition. It
   overlays the canonical Reality fields, renders resolved outbounds, renders
   routing, preserves the fixed skeleton, and returns a complete JSON object.
3. The existing `GatewayProvider.render(DesiredRoutingState,
   CredentialResolver)` remains the single generic render API. The
   process-lifetime `XrayFileProvider` holds only immutable non-secret Xray
   configuration; its concrete render path requires the Xray-only resolver
   capability, reads Reality identity for that call, and returns a complete
   `CandidateConfig`. Neither generic domain code nor the provider base
   contract knows Xray, Reality, Marzban, or a filesystem path.

The selected alternative is **Option B: the process-lifetime concrete Xray
provider holds immutable non-secret configuration, while
`GatewayProvider.render(DesiredRoutingState, CredentialResolver)` receives the
operation-scoped resolver and the Xray-only resolver capability supplies
Reality identity for that call; one shared pure canonical composer produces the
candidate**. This keeps data ownership and secret/session lifetime outside
generic domain and base contracts while making the provider/runtime path and
standalone ops path use exactly one composition semantics. No second generic
renderer Protocol is introduced.

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
Xray adapter, not a generic desired-state source. The composer may inspect only
the following explicit field allowlist; each field is emitted according to its
listed owner. All other template paths are rejected as unknown unless this ADR
is amended:

| Template path | Canonical owner | Candidate behavior |
| --- | --- | --- |
| `log.loglevel` | `SETTINGS` | read from deployment Settings; template is not authoritative |
| `inbounds[*].tag` | `XRAY_RENDERER_CONSTANT` | emit the fixed Xray tag; template is not authoritative |
| `inbounds[*].listen` | `STATIC_TEMPLATE` | copy exactly |
| `inbounds[*].port` | `STATIC_TEMPLATE` | copy exactly |
| `inbounds[*].protocol` | `STATIC_TEMPLATE` | copy exactly |
| `inbounds[*].settings.decryption` | `XRAY_RENDERER_CONSTANT` | emit the fixed Xray protocol value |
| `inbounds[*].streamSettings.network` | `XRAY_RENDERER_CONSTANT` | emit the fixed Xray transport value |
| `inbounds[*].streamSettings.security` | `XRAY_RENDERER_CONSTANT` | emit the fixed Xray security value |
| `inbounds[*].streamSettings.realitySettings.show` | `XRAY_RENDERER_CONSTANT` | emit the fixed Reality structure value |
| `inbounds[*].streamSettings.realitySettings.xver` | `XRAY_RENDERER_CONSTANT` | emit the fixed Reality structure value |
| `routing.domainStrategy` | `XRAY_RENDERER_CONSTANT` | emit the fixed routing structure value |
| `inbounds[*].streamSettings.realitySettings.dest` | `SETTINGS` | overwrite from Settings |
| `inbounds[*].streamSettings.realitySettings.serverNames` | `SETTINGS` | overwrite from Settings |
| `inbounds[*].streamSettings.realitySettings.privateKey` | `SECRET` | overwrite from persisted identity |
| `inbounds[*].streamSettings.realitySettings.shortIds` | `SECRET` | overwrite from persisted identity |
| `inbounds[*].settings.clients` | `XRAY_FILE_SKELETON` | emit the canonical repo-owned empty list `[]`; never copy runtime membership |
| `routing.rules` | `DB` + `RENDERER_CONSTANT` | replace with fresh desired rules and fixed BLOCK rules |
| `outbounds` | `DB` + `RENDERER_CONSTANT` | replace with fresh desired outbounds and BLOCK |

`STATIC_TEMPLATE` is limited to the ADR-014 skeleton fields `protocol`,
`listen`, and `port` (plus only genuinely equivalent infrastructure wiring
if later accepted explicitly). It does not own log level or any Xray
protocol/Reality structure value beyond the explicitly retained
`inbounds[*].protocol` skeleton field, Reality identity, or deployment values.
`DB` owns
desired route and egress definitions, `SECRET` owns the persisted Reality
identity, `SETTINGS` owns deployment-varying values, `XRAY_RENDERER_CONSTANT`
owns fixed Xray protocol/structure semantics and fixed BLOCK semantics,
`XRAY_FILE_SKELETON` owns the file's explicit empty `clients` skeleton, and
`MARZBAN_RUNTIME` owns dynamic client membership. The composer must validate
the template against this projection before composing. A future template key,
or a current key not listed above, fails closed and requires an explicit
ownership update in a reviewed ADR; it is never inherited by a broad
`deepcopy(template)` rule.

### 3.2 Process-stable Xray configuration

`XrayFileProvider` may be process/application-lifetime owner of immutable,
non-secret configuration only:

- the validated `XrayStaticSkeleton` projection;
- fixed `XRAY_RENDERER_CONSTANT` values; and
- an immutable `XrayDeploymentConfig` containing the validated Settings
  values needed for rendering, including `xray_log_level`, Reality `dest`,
  and Reality `serverNames`.

This configuration is read and validated when the concrete provider is built.
Changing deployment configuration requires constructing a new provider or
restarting the process; it is not a hidden mutable operation context.
Neither this configuration nor the provider contains Reality `privateKey` or
`shortIds`.

### 3.3 Central Settings

The deployment Settings contract includes:

```python
Settings.xray_log_level: str = "warning"
```

It is read from `XRAY_LOG_LEVEL`, defaults to `warning`, and is the sole
desired source for `log.loglevel`. Its normalized value is `strip()` followed
by lowercase; the result must be exactly one of `debug`, `info`, `warning`,
`error`, or `none`. Blank or invalid input fails closed. It is deliberately
not an alias for the application log-level setting: Xray logging and
application logging have separate operational ownership and may not be
coupled by this ADR. This is a decision-only contract; this docs-only PR does
not modify `config.py`.

The deployment Settings owner also supplies the Reality deployment values
below. A template value is never a fallback for a missing or invalid Settings
value.

`Settings.xray_reality_dest` and
`Settings.xray_reality_server_name` are the sole desired sources for Reality
deployment parameters. Blank or whitespace-only values fail closed. Template
or runtime values are not fallbacks.

### 3.4 Persisted Reality identity

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
operation. During a route apply, the operation-scoped resolver implementing
`XrayRenderResolver` performs the purpose-bound current read of the already-owned
identity from the same operation Session when the concrete Xray
`render()` calls `resolve_reality_identity()`. If the identity is absent or
invalid, composition fails closed; route mutation is not made to depend on an
implicit regeneration. The assembler constructs the same-operation resolver,
obtains fresh `DesiredRoutingState`, and wires process-stable configuration; it
does not pass Reality plaintext directly to the provider.

### 3.5 Fresh desired routing

The application/domain boundary obtains a fresh `DesiredRoutingState` after the
route mutation and flush required by ADR-017. It contains the authoritative
route principals and complete outbound DTO set from the current operation
snapshot. It is not derived from `runtime.current()`.

### 3.6 Egress credentials

Egress credential refs continue to be resolved through the explicit
operation-scoped `CredentialResolver` from ADR-019. The concrete Xray module
defines one Xray-only capability over that existing resolver:

```python
@runtime_checkable
class XrayRenderResolver(CredentialResolver, Protocol):
    def resolve_reality_identity(self) -> XrayRealityConfig: ...
```

The operation-scoped infra resolver implements both the generic egress
credential operation and this Xray-only capability, using the same provisioning
`Session` for the fresh route/outbound read and the purpose-bound Reality
identity read. `XrayFileProvider.render()` requires this capability and fails
closed if the supplied resolver cannot provide it; it never downcasts to a
Session or ORM object. The concrete Xray render path uses the resolver only for
the duration of `render()` and never stores it. Plaintext credential material
may exist transiently in the composer/provider call, but DTO reprs, exceptions,
logs, and audit payloads must remain redacted.

The registry constructs neither a resolver nor a cached operation/session
object.

The lifecycle prohibition is explicit: `ProviderRegistry.gateway` is a
process/application-lifetime stateless provider owner; it stores no
`Session`, resolver, Reality plaintext, or mutable operation context.
`XrayFileProvider` stores no operation-scoped mutable state. `ContextVar`,
global mutable context, thread-local hidden state, downcasting to obtain a
SQLAlchemy `Session`, using `runtime.current()` to restore Reality, and any
process-lifetime plaintext cache are forbidden.

## 4. Provider-neutral and Xray-specific contracts

The generic provider base and domain remain provider-neutral. The existing
single render contract is retained exactly:

```python
class GatewayProvider(Protocol):
    def render(
        self,
        desired: DesiredRoutingState,
        resolver: CredentialResolver,
    ) -> CandidateConfig: ...

    def validate(self, candidate: CandidateConfig) -> ValidationResult: ...
    def apply(self, candidate: CandidateConfig) -> ApplyResult: ...
    def health(self) -> HealthReport: ...
```

No second generic renderer Protocol is needed. The implementation PR must keep
this signature provider-neutral (`DesiredRoutingState` plus
`CredentialResolver`). The existing `XrayOutboundDTO` in
`backend/app/providers/base.py`, and its accepted use in
`DesiredRoutingState.outbounds: tuple[XrayOutboundDTO, ...]`, are grandfathered
by ADR-019 and remain unchanged in this slice. ADR-020 does not require
renaming or generalizing that DTO, and no new provider-neutral outbound DTO is
to be introduced.

The prohibition applies only to adding new full-config or deployment-specific
types to generic business contracts. `XrayStaticSkeleton`,
`XrayDeploymentConfig`, `XrayRealityConfig`, `XrayFullConfigInput`,
filesystem/runtime/VPS-specific types, and equivalent concrete provider types
remain in the concrete Xray adapter. The domain provisioning service may use
the ADR-019-approved outbound DTO through the existing base contract, but it
does not import the concrete Xray adapter, Reality, Marzban, or filesystem
concepts. The process-lifetime
concrete Xray provider may hold only the immutable non-secret
`XrayStaticSkeleton`, `XrayDeploymentConfig`, and fixed renderer constants
described in §3.2. Its `render()` call receives the operation-scoped resolver
through the existing generic argument and obtains the Reality identity through
the Xray-only capability in §3.6. This is the concrete data channel; it is not
a second public rendering interface.

The concrete Xray adapter lives under `backend/app/providers/gateway/` or an
application/infra adapter module and may define the following custom immutable
non-dataclass types. These types must not be exported from `base.py` or used by
generic domain contracts:

```python
class XrayStaticSkeleton:
    # Immutable validated projection of the explicit static allowlist in §3.1.
    ...


class XrayDeploymentConfig:
    # Immutable non-secret Settings projection: xray_log_level,
    # Reality dest, and Reality serverNames.
    ...


class XrayRealityConfig:
    # Immutable validated values; implementation uses private slots and
    # properties, not @dataclass or a generic serializable record.
    ...


class XrayFullConfigInput:
    # Immutable composition input containing XrayStaticSkeleton,
    # XrayDeploymentConfig, XrayRealityConfig, and generic desired state.
    ...




```

The concrete Xray `GatewayProvider.render()` path receives the generic
operation-scoped resolver, verifies that it implements `XrayRenderResolver`,
calls `resolve_reality_identity()` for that render, and forms a local
`XrayFullConfigInput` from the provider-held non-secret configuration, the
returned Reality identity, and fresh desired state. It then invokes the shared
pure Xray composer and returns a complete candidate. The identity and resolver
are local to the call and are not cached or placed on the provider; normal
return or failure releases those references. The process/application-lifetime
registry may hold the provider and its immutable non-secret configuration, but
must not hold an operation-scoped Xray context, resolver, Session, or plaintext.

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

The operation lifecycle is:

```text
caller-owned operation Session
  -> fresh DesiredRoutingState + operation resolver implementing XrayRenderResolver
  -> GatewayProvider.render(DesiredRoutingState, resolver)
     -> provider uses immutable skeleton/deployment config
     -> XrayFileProvider reads Reality identity through resolver
     -> local XrayFullConfigInput + pure composition
  -> complete CandidateConfig
```

No SQLAlchemy `Session`, ORM model, Secret row, raw Secret ref lookup, or
provider-specific DTO is placed in a generic domain/base contract. The
operation-scoped resolver is created and owned by application/infra, implements
the concrete Xray capability without exposing its Session, and is passed into
`render()` explicitly. The registry never constructs or caches it; the
provider never stores it.

### 4.1 Portability rule

Generic domain code and `backend/app/providers/base.py` must not introduce or
depend on new full-config/deployment-specific types such as
`XrayStaticSkeleton`, `XrayDeploymentConfig`, `XrayRealityConfig`,
`XrayFullConfigInput`, Reality deployment config, Marzban paths, VPS vendors,
hard-coded hostnames/domains, Webshare-specific types, or concrete filesystem
deployment paths. The ADR-019-approved `XrayOutboundDTO` is the explicit
grandfathered exception and remains unchanged; this ADR neither renames nor
generalizes it and does not add another outbound DTO. Replaceable
infrastructure enters through existing generic contracts, concrete providers,
adapters, Settings/configuration, and operation-scoped factories. Strong
modularity remains required for VPS/deployment targets, upstream/third-party
providers, egress providers, subscription/distribution/API domains, and
external endpoints/credentials. A future gateway engine or vendor must be
implementable by adding/replacing its concrete adapter without changing core
business logic or introducing a repository-wide abstraction hierarchy. This
extends ADR-009's replaceability rule; it does not authorize a repository-wide
rewrite in this docs-only slice.

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
recorded in an ADR. The repo-owned file candidate explicitly emits
`inbounds[*].settings.clients: []`, matching the accepted empty skeleton from
ADR-015. This empty list is file-skeleton state, not runtime membership: the
composer never copies or synthesizes Marzban-managed dynamic clients from
`runtime.current()`.

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

### Option B — application/infra supplies Xray context to the concrete adapter

Selected. It gives the assembler one operation lifecycle and keeps route,
Settings, static-skeleton, and Secret reads explicit. The generic application
uses the existing `GatewayProvider.render(DesiredRoutingState,
CredentialResolver)` call. The concrete Xray implementation holds only the
validated immutable non-secret configuration; it verifies that the supplied
operation resolver implements `XrayRenderResolver`, reads the Reality identity
through that method for this render, forms the local Xray-specific input, and
returns the full candidate. The shared pure composer makes this choice
deterministic without adding a second renderer interface, while
`GatewayProvider` remains replaceable for non-Xray gateways.

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

There is one canonical pure composer and one existing provider contract:

- `XrayFileProvider.render(desired, resolver)` remains the single generic
  entrypoint. The provider holds only immutable non-secret
  `XrayStaticSkeleton`/`XrayDeploymentConfig`; its concrete implementation
  obtains the operation-fresh Reality identity from the Xray-only resolver
  capability, delegates to the pure composer, and wraps the resulting full JSON
  object in `CandidateConfig`.
- `XrayFileProvider` consumes only the complete `CandidateConfig` for
  `validate`, `apply`, and `health`; it never accepts or installs a routing
  fragment. The Xray context is an internal concrete-adapter concern, not a
  new generic Protocol or DTO layer.
- `ops/gateway/render_xray_routes.py` remains a standalone operational entry
  point while that workflow exists, but its DB/Settings/Secret collection is
  moved behind the same application/infra assembler and its final structure is
  produced by the same pure composer. It must not retain a parallel
  `render_config()` semantics.

The concrete Xray adapter is the long-term full-config renderer. The standalone
script is a compatibility/operations adapter, not a second source of truth.
The two paths must be covered by parity tests over the same Xray-specific
context and generic desired snapshot. The generic gateway provider contract
remains portable to a different engine or vendor.

## 8. Marzban dynamic-client boundary

Marzban remains the owner of dynamic client lifecycle described by ADR-015.
The disk file also has a repo-owned skeleton: the canonical candidate includes
`inbounds[*].settings.clients: []`. That empty list is a file-skeleton field;
it is not the runtime membership managed by Marzban's gRPC Handler API.

The actual dynamic client records and other Marzban-managed runtime fields are
not part of the repo-owned desired state or desired equality. The composer must
not copy them from `runtime.current()` into the candidate.

The runtime adapter's install contract must therefore be explicit: it installs
the repo-owned full candidate with the empty file skeleton and must not
synthesize dynamic clients from a runtime read. An adapter that cannot apply
the full candidate while leaving Marzban's separately managed client lifecycle
intact is unsupported and must fail closed; `runtime.current()` merging is not
an allowed compatibility fix.

The implementation must distinguish these comparison classes:

| Projection class | Owner | Comparison rule |
| --- | --- | --- |
| Routing rules and repo-owned outbounds | repository desired state | exact canonical equality |
| Static inbound protocol/listen/port skeleton | static template | exact canonical equality |
| Fixed Xray tag/decryption/network/security/Reality show/xver/domainStrategy | Xray renderer constants | exact canonical equality |
| `log.loglevel` and Reality `dest`/`serverNames` | deployment Settings | exact canonical equality |
| Reality `privateKey`, `shortIds` | Secret identity | exact canonical equality |
| File skeleton `inbounds[*].settings.clients` | repo-owned Xray file skeleton | exact canonical equality to `[]` |
| Runtime-only dynamic client membership and other runtime-only fields | Marzban/runtime | excluded from repo-owned equality; never copied |

The Settings row above covers deployment-varying values; the Secret identity
row remains the sole owner of `privateKey` and `shortIds`. After reload or
drift inspection, the projection must keep the file skeleton `clients=[]`
separate from runtime-only dynamic membership. If the adapter cannot make that
distinction safely, verification fails closed rather than broadening the
desired candidate or treating the runtime membership as file drift.

If any other required repo-owned field is absent or cannot be projected safely
after reload, verification fails closed. It is not silently classified as
dynamic.

## 9. Preservation/legal-deletion contract for the next slice

The preservation rewrite must use this algorithm:

1. Acquire the existing named GatewayRouteBinding lock.
2. Perform the route mutation and flush, without an early business commit.
3. Read a fresh desired snapshot using the same operation Session, construct the
   operation-scoped resolver implementing the Xray-only capability, and call the
   existing `GatewayProvider.render()` with it to compose a full candidate. The
   domain sees only the generic provider contract; Reality identity is read
   inside the concrete Xray render call.
4. Parse the candidate, run all structural invariants, and invoke `xray run
   -test` before installation.
5. Back up the current file, install the complete candidate, reload, and run
   health checks.
6. Read `runtime.current()` only for post-reload observation. Project the
   repo-owned file skeleton, including `settings.clients=[]`, and the other
   repo-owned fields; compare them **exactly** with the candidate's projection.
   Keep Marzban runtime-only dynamic membership in a separate projection and
   exclude it from repo-owned equality.
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

The persisted Reality identity read is a read-only current read performed by
`XrayRenderResolver` in the same operation Session as the fresh
routing/credential snapshot. The existing create-once identity bootstrap is an
independent Secret-provisioning lifecycle and is not a pre-apply route commit.
ADR-017's lock span and caller-owned commit/rollback boundary remain unchanged.

## 10. Implementation handoff

The next implementation PR must be limited to the following concrete work:

- `backend/app/providers/base.py`: retain the existing
  `GatewayProvider.render(DesiredRoutingState, CredentialResolver)` contract
  together with `validate`/`apply`/`health`; keep it provider-neutral and do
  not add Xray DTOs or a second renderer Protocol here.
- `backend/app/providers/gateway/xray_file.py` or the concrete Xray adapter
  module: add the custom immutable `XrayStaticSkeleton`, non-secret
  `XrayDeploymentConfig`, secret-safe `XrayRealityConfig`, and
  `XrayFullConfigInput` types plus the concrete Xray-only
  `XrayRenderResolver` capability and composition path; these are not generic
  domain contracts. The provider may retain only the first two as immutable
  process-stable configuration.
- `backend/app/providers/gateway/xray_composition.py`: add the pure canonical
  full-config composer and invariant helpers.
- `backend/app/providers/gateway/xray_file.py`: make the concrete Xray adapter's
  candidate-rendering path delegate to the full composer and make
  validation/apply consume a full `CandidateConfig`; replace runtime-superset
  preservation with the exact post-reload repo-owned projection in a later
  preservation slice.
- `backend/app/domain/provisioning.py` and the application composition
  boundary: continue to call the generic `GatewayProvider.render()` contract;
  application/infra assembles fresh desired state, validates the explicit Xray
  skeleton and Settings, and constructs the same-operation resolver
  implementing `XrayRenderResolver`. The resolver reads Reality identity inside
  the concrete render call without exposing Session/ORM or Xray JSON to generic
  domain code.
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
