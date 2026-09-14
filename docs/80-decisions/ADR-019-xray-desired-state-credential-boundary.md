# ADR-019：Xray desired state 与 credential boundary

- 状态：Accepted candidate（待独立审查与人工合并）
- 日期：2026-09-14
- 范围：TASK-T16 Phase 2B-remain-1A（架构与契约，仅文档）
- 执行者：本 exact task 由 Codex 唯一写入；Claude Code 不参与本任务。此任务级指派不改写仓库级协作规则。

## Context

PR #100 已合并到 `main`，本 ADR 以提交
`d45197da388262b840497d6ce611293eec95ed9b` 为事实基线。Phase 2B-remain-0
已完成 `AccountingProvider.health_check()`，但 Xray desired-state 仍不能表达
真实 outbound 的完整连接信息。直接进入 remain-1 实现会同时临时决定 DTO、
secret resolver、Session 生命周期和明文泄露边界，容易产生不可逆的接口返工。

本 ADR 只关闭这些架构问题；它不实现 DTO、resolver、renderer、schema 或业务代码。
只有本 ADR 经独立审查并人工合并后，才可开始 Phase 2B-remain-1B。

## Existing verified facts

以下事实均由当前 `main` 的代码、模型、测试和既有 ADR 重新核对：

1. `backend/app/providers/base.py::DesiredRoutingState` 当前只有
   `user_routes: Mapping[str, str]` 与 `outbound_tags: tuple[str, ...]`。
   `XrayFileProvider.render()` 因而只能生成 tag/protocol 摘要，不能表达
   host、port、连接协议或 secret 引用。
2. `CredentialDTO` 当前保存 `username` 与 `password`，且 password 使用
   `repr=False`。它是明文凭据的 transient DTO，不是安全存储。
3. `EgressEndpoint` 已有 `host`、`port`、`protocol`、
   `credential_secret_ref`；`EgressBinding` 已有可空的
   `credential_secret_ref` override；`GatewayRouteBinding` 已有
   `gateway_principal`、`outbound_tag`、`egress_id`、active/released
   状态字段。当前 ADR-014 与 `ops/gateway/render_xray_routes.py` 都把这些
   数据作为 Xray route 的数据库来源。
4. 当前 active credential precedence 是：若 active binding 的 override 为
   非空值则使用它，否则使用 endpoint-level ref。released binding 被 active
   查询排除。当前脚本使用 Python truthiness 实现这一点；本 ADR 收紧
   malformed 非空 ref 的行为，见“Credential precedence”。
5. `backend/app/core/secrets.py::reveal_secret(db, secret_ref)` 直接需要
   SQLAlchemy `Session`，并从 encrypted Secret row 返回 plaintext。
6. 当前 `ops/gateway/render_xray_routes.py` 是带 DB Session 的入口/查询层：
   它查询 active route，选择 secret ref，调用 `reveal_secret()`，解析 JSON，
   最终把 username/password 写进 Xray `outbounds[].settings.servers[].users`。
   因此 plaintext 第一次出现于 resolver/infra 边界之后、进入 renderer 输入时。
7. 当前 `CandidateConfig` 是 dataclass，`content: Mapping[str, object]`
   没有 `repr=False`；默认 repr 可能递归展示其中未来会出现的 plaintext。
   这是已确认的 downstream security gap，不在本 ADR 中修代码。
8. ADR-014 已接受：Xray inbound protocol/listen/port 与静态部署模板边界、
   Reality ownership、DB 全量渲染和 BLOCK 防护不能被本 ADR 推翻。
   ADR-016 已接受：`GatewayRouteBinding.gateway_principal` 必须来自
   accounting provider 返回的 `routing_principal`，不能从 username/tenant
   猜测。ADR-015 Part B 已接受 accounting/transport 的 H1 分类。

## Decision

### 1. Desired-state DTO boundary

Phase 2B-remain-1B 应在 provider contracts 中新增：

```python
@dataclass(frozen=True, slots=True)
class XrayOutboundDTO:
    tag: str
    host: str
    port: int
    protocol: str
    credential_secret_ref: str
```

字段语义：

- `tag` 是由 active `GatewayRouteBinding.outbound_tag` 提供的稳定 outbound
  标识。
- `host`、`port`、`protocol` 来自 active route 关联的 egress endpoint。
- `credential_secret_ref` 是经过 precedence 选择的 opaque secret handle，
  不是 secret plaintext。
- DTO 不包含 plaintext username、plaintext password，也不包含
  `CredentialDTO`。它可以安全地在 DB desired snapshot、domain/provider
  contract、审计摘要和测试 fixture 中流转。
- `DesiredRoutingState` 应以 `outbounds: tuple[XrayOutboundDTO, ...]`
  替换 `outbound_tags`。本仓库的 Xray registry 仍未激活，因此不保留长期
  compatibility 字段；remain-1B 应一次性迁移内部调用方与测试。
- `user_routes: Mapping[str, str]` 保留为
  `gateway_principal -> outbound tag`，但它必须与同一 snapshot 的 outbounds
  一致：每个非 BLOCK route target 都必须存在且唯一。
- BLOCK outbound 不是 DB-backed customer outbound。它是 renderer 固定生成的
  application-owned invariant（`tag="BLOCK"`, `protocol="blackhole"`），
  不放入 `XrayOutboundDTO`，也不从 endpoint/secret 表读取。renderer 必须始终
  生成它，并保留既有 private-IP/unmatched-flow BLOCK 规则。
- 当前 `outbound_tags` 的旧 tuple 字段被替换，而不是让它继续成为第二个
  source of truth。若未来需要外部 compatibility，必须另行记录迁移 ADR。

### 2. Full-snapshot invariant

`DesiredRoutingState` 的唯一语义是“当前完整 DB desired snapshot”，不是
一次 provisioning 新增的一条 delta route。

后续 remain-3 查询层必须：

1. 在同一个一致性边界读取所有 active、enabled、未 released 的
   `GatewayRouteBinding`，并 join 到对应 `EgressEndpoint` 与 active
   `EgressBinding`；不能只读取当前 subscription。
2. 生成 deterministic order 的全部 `user_routes` 与 `outbounds`，并拒绝
   duplicate principal/tag、缺失 endpoint、orphan route 或不一致引用。
3. 在 renderer 之前完成全量 preflight；任何 active route 的结构或 credential
   ref 无法解析时，整体 fail closed，不产出“少了其它客户”的部分配置。
4. 不读取当前运行时 Xray 文件来补全缺失 desired state，也不通过增量拼接来
   保留旧客户。新增订阅的 provisioning 只负责提交其 DB 状态；随后用于
   rendering 的输入必须重新构造完整 snapshot，从而不会因只传入新客户而删除
   其它 active 客户。
5. 明确 snapshot 与 DB transaction 的顺序：所有影响 active route 的 DB
   mutation 必须在可被查询的提交/一致性边界之后，才允许执行该 snapshot 的
   render/apply；若处于 pending/未提交状态，不得假装是完整 desired state。

本 ADR 不决定具体 JOIN、索引或 transaction 实现；这些是 remain-3 的 acceptance
requirement，不是实现 PR 临场可变更的契约。

### 3. Credential resolver boundary

选择一个 typed resolver Protocol，放在
`backend/app/providers/base.py` 的 provider contract 层：

```python
class CredentialResolver(Protocol):
    def resolve(self, secret_ref: str) -> CredentialDTO: ...
```

边界和所有权：

- `SqlAlchemyCredentialResolver`（名称可在实现时调整）属于
  `backend/app/infra/`。它是唯一允许直接持有 SQLAlchemy `Session`、ORM
  `Secret` model 并调用 `reveal_secret()` 的适配器。
- Session 由当前 application/provisioning operation 持有。resolver 的实例
  必须不超过该 Session 的生命周期；不能把 request/session-bound resolver
  放进 process-lifetime registry。
- `XrayFileProvider` 通过构造注入或 operation-scoped binding 获得
  `CredentialResolver`，只依赖 Protocol；它不能 import
  `sqlalchemy.orm.Session` 或 ORM models。
- 纯 Xray renderer 只接收已解析的 transient credential/route value；它不能
  看到 Session。带 Session 的脚本入口只能是 orchestration/infra 层，必须在
  调用纯 renderer 前完成查询和 resolver 组装。
- domain 层不解密、不解析 secret JSON、不持有 `CredentialDTO`；它只组织
  route identity、state contract 和 provisioning outcome。
- resolver 不允许缓存 plaintext。每次 render/apply operation 按需 resolve，
  operation 结束即释放所有 resolver/credential 引用；如未来必须缓存，必须
  另行做 secret-lifecycle ADR。
- `resolve()` 对 missing secret、decrypt failure、malformed payload、blank
  ref 和 unsupported credential schema 统一抛出稳定的
  `CredentialResolutionError`（具体实现可位于 provider contract 层），
  不得返回 empty credential、`None` 或调用 endpoint fallback。

### 4. Plaintext lifecycle

允许的数据流是：

```text
EgressBinding/EgressEndpoint.credential_secret_ref
    -> encrypted Secret row
    -> operation-scoped CredentialResolver
    -> transient CredentialDTO(username, password)
    -> pure Xray renderer / provider render
    -> transient CandidateConfig.content
    -> restricted Xray runtime config file
```

规则：

- plaintext 第一次只在 resolver 返回 `CredentialDTO` 时出现。
- `DesiredRoutingState`、`XrayOutboundDTO`、DB rows、audit ordinary fields、
  logs、metrics、exceptions、Issue/PR/docs 均不得包含 plaintext。
- renderer 内部局部变量和 CandidateConfig content 可以短暂持有 plaintext，
  因为当前 Xray `users` payload 需要它；它们不得被复制到全局缓存、普通
  structured logging 或 error context。
- runtime config file 是受权限保护的 runtime secret artifact，不是普通日志
  或审计记录。部署/运行时权限、备份和清理规则属于实现与运维验收。
- operation 结束后不得保留 resolver、CredentialDTO 或 candidate 的引用；
  Python memory zeroization 不作不切实际的保证，但必须缩短生命周期并禁止
  主动序列化。
- 若未来 Xray 支持不含 plaintext 的 credential reference，才可通过新 ADR
  重新评估 CandidateConfig 是否还需要明文 password。

### 5. CandidateConfig redaction contract

remain-1B 必须修改 CandidateConfig contract，使 `content` 至少使用
`field(repr=False)`，并提供显式安全 repr，例如：

```text
CandidateConfig(version='...', content=<redacted>)
```

同时：

- `version` 可以显示；它是版本/fingerprint 标识，不是 credential。
- 不允许默认 dataclass repr、`asdict()`、exception chaining、audit payload
  或 structured logger 自动序列化 `content`。
- validation/apply 异常只能携带稳定错误 code、version、字段路径或
  non-secret fingerprint；不得 dump CandidateConfig 或嵌入 username/password。
- 测试必须构造含 sentinel password 的 candidate，验证 `repr()`、异常文本、
  日志事件和 audit details 均不包含该 password。
- Xray runtime file 的内容只允许在受控的 install/backup/reload 路径中处理；
  不能把 candidate content 作为普通诊断输出。

### 6. Secret payload format and fail-closed semantics

现有 `save_credentials()` 写入的 plaintext payload 规范化为严格 JSON object：

```json
{"username": "...", "password": "..."}
```

resolver 必须：

- 要求 JSON 顶层是 object，拒绝 string、array、number、null 和空 object。
- 要求 `username`、`password` 都是 non-empty JSON strings；不自动补默认值、
  不把非字符串转成字符串、不把 malformed 当作 empty credential。
- 当前 contract 不允许 unknown keys；多余字段即 malformed，避免把未经审查的
  secret metadata 带入运行时。
- 对 invalid JSON、wrong shape、unknown keys、missing/blank/non-string fields、
  decrypt failure 和 missing ref 统一 fail closed，转换为
  `CredentialResolutionError`。错误消息只包含稳定 code，例如
  `CREDENTIAL_MALFORMED` 或 `CREDENTIAL_NOT_FOUND`，绝不包含 raw payload、
  username、password 或 raw secret ref。
- `credential_secret_ref` 是 opaque handle。普通日志/exception 不记录它；
  如安全审计确需关联，只记录不可逆 fingerprint 和非 secret 的 endpoint/
  subscription identifier，且不得记录解密后的 value。

### 7. Credential precedence

active Xray route 的选择顺序固定为：

```text
active EgressBinding.credential_secret_ref
    if it is explicitly present and non-empty
otherwise
active EgressEndpoint.credential_secret_ref
```

具体规则：

- binding override 为 `NULL` 或空字符串表示“没有 override”，允许回退
  endpoint-level ref。这与当前 DB/ADR-014 的 fallback 语义一致。
- whitespace-only、格式非法、指向不存在 Secret 或解密/JSON 校验失败的
  non-empty override 都是 malformed；必须 fail closed，禁止静默回退到 endpoint
  secret。否则会把客户绑定错误地连接到另一个 credential。
- endpoint-level ref 为空、malformed、missing 或 payload invalid 时也必须
  fail closed。
- 只考虑 `released_at IS NULL` 且 `enabled = TRUE` 的 active binding；
  released binding 不得参与 precedence 或 desired snapshot。
- override 选择和 resolver 校验必须在同一次 snapshot/render operation 中完成，
  并在任何写入/重载前完成全量 preflight。

### 8. Ownership / layer boundaries

| Layer | Owns | Must not own |
|---|---|---|
| Domain | provisioning orchestration, route identity contract, full-snapshot request boundary, fail-closed outcome mapping | SQLAlchemy, Secret decryption, raw credential JSON |
| Provider base DTO/contracts | `DesiredRoutingState`, `XrayOutboundDTO`, `CredentialDTO`, `CredentialResolver`, stable cross-layer error contract | ORM queries, Session lifecycle |
| Infra adapter | DB joins, Session-scoped resolver, encrypted Secret access, ref precedence materialization | Xray reload policy, business route decisions |
| Gateway provider | consume desired snapshot, invoke injected resolver, render/validate/apply through safe runtime boundary | Session/ORM imports, DB queries, secret logging |
| Pure Xray renderer | deterministic config transformation from resolved transient inputs; fixed BLOCK invariant | Session, DB, `reveal_secret()`, live fallback, logging plaintext |
| Runtime/deployment layer | restricted file install/backup/reload and artifact permissions | changing DB desired state or silently repairing missing data |

The existing `ops/gateway/render_xray_routes.py` currently combines entrypoint,
DB query, `reveal_secret()`, payload validation and pure rendering. This is a
verified implementation discrepancy, not a reason to change code in remain-1A.
remain-1B/remain-3 must split or wrap those responsibilities so the pure renderer
satisfies this boundary.

## Rejected alternatives

1. **Put username/password directly into DesiredRoutingState** — rejected because
   it makes a general provider DTO a long-lived plaintext container and expands the
   logging/audit blast radius.
2. **Put CredentialDTO inside XrayOutboundDTO** — rejected for the same lifecycle
   reason; desired state must remain safe before resolution.
3. **Pass SQLAlchemy Session into GatewayProvider or renderer** — rejected because it
   couples provider contracts to ORM and makes runtime providers responsible for
   transaction/session lifecycle.
4. **Let renderer call reveal_secret() directly** — rejected because it hides the
   resolver boundary, makes redaction inconsistent, and duplicates malformed-secret
   handling.
5. **Treat desired state as one new route plus current file contents** — rejected by
   AGENTS.md full-database rendering rule; runtime file is not the source of truth.
6. **Silently fall back on any binding override error** — rejected as credential
   confusion and cross-customer exposure risk.
7. **Keep outbound_tags beside outbounds indefinitely** — rejected because two
   representations drift; BLOCK remains renderer-owned instead.
8. **Introduce a broad service-health abstraction in this ADR** — rejected as
   unrelated to the desired-state/credential boundary; existing provider contracts
   remain the scoped boundary.
9. **Use a live Xray/Marzban endpoint as desired-state input** — rejected because
   DB is the source of truth and live state is not a safe substitute for a full
   snapshot.

## Migration and schema impact

This ADR requires no migration and no DB schema change. Existing columns contain the
required refs and route identity data. The implementation work will change Python
contracts and query/render orchestration:

- remain-1B: add DTO/error/resolver contracts, safe CandidateConfig repr, and
  operation-scoped injection; no live provider enablement.
- remain-3: implement deterministic full-snapshot DB query and precedence preflight.
- later renderer work: consume resolved transient credentials and install only after
  validation, preserving existing nine-step safe reload/rollback requirements.

If existing data violates the new strict contract, migration/repair must be a separate,
explicit task. No fallback or silent coercion is allowed.

## Testing requirements

The future implementation must add offline deterministic tests for:

- DTO contains refs and connection details but never plaintext.
- full snapshot includes all active bindings and rejects partial/orphan/duplicate state.
- resolver success, missing ref, decrypt failure, invalid JSON, wrong top-level type,
  unknown keys, missing fields, blank fields and non-string fields.
- resolver/provider boundary proves no Session or ORM import in gateway provider/renderer.
- binding override precedence, explicit empty override fallback, malformed non-empty
  override no-fallback, endpoint fallback and released binding exclusion.
- CandidateConfig repr, exception, audit and structured logging redaction.
- renderer always emits fixed BLOCK and rejects unsupported/missing credential data.
- no real network, production credential, Xray process, reload or deployment is used.

## Downstream implementation requirements

1. Do not begin remain-1B until this ADR is independently reviewed and manually merged.
2. Implement DTO changes in provider contracts first, then adapt all callers/tests in
   one coherent PR; do not leave two competing outbound representations.
3. Keep `CredentialResolver` operation-scoped and inject it without exposing Session.
4. Translate current `SecretStoreError`/JSON parser failures at the infra boundary into
   redacted stable `CredentialResolutionError` codes.
5. Implement full-snapshot query/preflight before any Xray render/apply path can be
   selected. Preserve ADR-014 ownership and ADR-016 route identity.
6. Treat existing `ops/gateway/render_xray_routes.py` coupling as an explicit
   refactor requirement; do not bypass this ADR by passing a Session into providers.
7. Keep Phase 2C and `GATEWAY_PROVIDER=xray_file` registry wiring paused until Phase
   2B is complete and independently reviewed.
8. Do not change production deployment, credentials, workflows, #97 or #98 in the
   remain-1A/1B work unless a later task explicitly authorizes it.

## Re-evaluation conditions

Re-open this ADR if:

- Xray changes its credential schema or can consume secret references without plaintext.
- Secret storage moves to an external manager or introduces versioned/rotated handles.
- Egress binding semantics change from one active binding per subscription/egress.
- A requirement emerges for multiple credential kinds, shared credentials, or per-route
  credential rotation without a DB snapshot.
- `CandidateConfig` no longer needs to carry runtime secret material.
- A future accepted ADR changes inbound/Reality ownership or the full-snapshot invariant.

Until then, this ADR is the authoritative contract for remain-1B and downstream
Xray desired-state/credential implementation.