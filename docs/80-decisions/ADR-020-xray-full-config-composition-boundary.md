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
3. `XrayFileProvider` is the full-config provider adapter. Its long-term
   `render()` contract returns a `CandidateConfig` whose content is the complete
   file that its runtime adapter may test, install, reload, and verify. It never
   reads a `Session`, ORM object, or `runtime.current()`.

The selected alternative is **Option B: application/infra full typed input into
the provider, implemented through one shared pure canonical composer**. This
keeps data ownership and secret/session lifetime outside the provider while
making the provider/runtime path and the standalone ops path use exactly one
composition semantics.

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

`infrastructure/marzban/xray_config.base.json` remains the static skeleton
source. It owns the fixed inbound shape and other intentionally static config
structure. For the Reality inbound, the template supplies only the structural
skeleton and defaults such as `show`/`xver`; it is never authoritative for
`dest`, `serverNames`, `privateKey`, or `shortIds`.

The canonical composer must copy the skeleton into the candidate and apply only
the explicitly approved overlays. It must not silently import runtime-only
fields into the skeleton.

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
the fresh route/outbound read. `XrayFileProvider` may use the resolver only for
the duration of `render()`; it never stores it. Plaintext credential material
may exist transiently in the composer/provider call, but DTO reprs, exceptions,
logs, and audit payloads must remain redacted.

The registry constructs neither a resolver nor a cached operation/session
object.

## 4. Typed contract approved for the implementation slice

The following provider-side DTO and protocol shape is approved for the next
production implementation PR. Names are normative unless an implementation
PR records an equivalent rename in its review description.

```python
@dataclass(frozen=True, slots=True)
class XrayRealityConfig:
    dest: str
    server_names: tuple[str, ...]
    private_key: str
    short_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class XrayFullConfigInput:
    static_skeleton: Mapping[str, object]
    reality: XrayRealityConfig
    routing: DesiredRoutingState


class GatewayProvider(Protocol):
    def render(
        self,
        desired: XrayFullConfigInput,
        resolver: CredentialResolver,
    ) -> CandidateConfig: ...

    def validate(self, candidate: CandidateConfig) -> ValidationResult: ...
    def apply(self, candidate: CandidateConfig) -> ApplyResult: ...
    def health(self) -> HealthReport: ...
```

This is an explicit future contract change to `backend/app/providers/base.py`:
the current dormant `render(DesiredRoutingState, resolver)` shape is replaced
by the typed full-config input while the explicit resolver argument remains.
The implementation PR must update the mock, the provisioning caller, and tests
in the same change. The domain still owns provisioning orchestration and
`DesiredRoutingState`; it does not read Settings, Secret rows, files, or
runtime state. The provider-side DTO is an adapter boundary and does not make
Reality a domain-owned concept.

The assembler's lifecycle is:

```text
same operation Session
  -> read static skeleton + Settings
  -> current-read persisted Reality identity
  -> fresh DesiredRoutingState
  -> construct operation-scoped CredentialResolver
  -> XrayFullConfigInput + resolver
```

No SQLAlchemy `Session`, ORM model, Secret row, or raw Secret ref lookup is
placed inside `XrayFullConfigInput`. `XrayRealityConfig` and any future
resolved-credential DTO must have safe repr behavior.

## 5. Canonical composition semantics

The implementation must add one pure module, proposed as
`backend/app/providers/gateway/xray_composition.py`, with a function named
`compose_xray_full_config(input, resolver) -> Mapping[str, object]` or an
equivalent method on `XrayConfigComposer`. The module may depend on provider
DTOs and the resolver Protocol, but not on SQLAlchemy, filesystem reads,
environment reads, subprocesses, network clients, or logging of values.

The composer must:

1. Deep-copy the static skeleton.
2. Locate and validate the fixed Reality inbound skeleton.
3. Overlay `Settings`-derived `dest`/`serverNames` and persisted identity
   `privateKey`/`shortIds` from `XrayRealityConfig`.
4. Resolve every desired egress credential ref through the supplied resolver
   and render the complete desired outbound set.
5. Render all desired user route rules and the fixed BLOCK rules.
6. Return a complete candidate object containing the skeleton's inbounds and
   the canonical routing/outbounds, plus any other explicitly static top-level
   fields from the skeleton.

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

### Option B — application/infra builds typed full input, provider renders it

Selected. It gives the assembler one operation lifecycle and keeps route,
Settings, static-skeleton, and Secret reads explicit. The provider receives a
complete typed boundary plus the operation-scoped resolver and returns the full
candidate. The shared pure composer makes this choice deterministic rather than
duplicating a second renderer.

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

- `XrayFileProvider.render()` receives `XrayFullConfigInput` plus the explicit
  resolver, delegates to the pure composer, and wraps the resulting full JSON
  object in `CandidateConfig`.
- `ops/gateway/render_xray_routes.py` remains a standalone operational entry
  point while that workflow exists, but its DB/Settings/Secret collection is
  moved behind the same application/infra assembler and its final structure is
  produced by the same pure composer. It must not retain a parallel
  `render_config()` semantics.

The provider is the long-term full-config renderer. The standalone script is a
compatibility/operations adapter, not a second source of truth. The two paths
must be covered by parity tests over the same typed input.

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
3. Read a fresh desired snapshot and assemble `XrayFullConfigInput` using the
   same operation Session; resolve credentials and compose a full candidate.
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

- `backend/app/providers/base.py`: add the approved typed DTOs and update the
  `GatewayProvider` render contract; adapt mocks and call sites together.
- `backend/app/providers/gateway/xray_composition.py`: add the pure canonical
  full-config composer and invariant helpers.
- `backend/app/providers/gateway/xray_file.py`: make `render()` delegate to the
  full composer; make validation/apply consume a full `CandidateConfig`; replace
  runtime-superset preservation with the exact post-reload repo-owned
  projection in a later preservation slice.
- `backend/app/domain/provisioning.py` and the application composition
  boundary: assemble fresh typed input and the same-operation resolver without
  exposing Session/ORM to the provider.
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


