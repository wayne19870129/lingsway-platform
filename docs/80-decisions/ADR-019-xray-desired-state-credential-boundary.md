# ADR-019：Xray desired state 与 credential boundary

- Status: Accepted candidate（待独立审查与人工合并）
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
   SQLAlchemy `Session`，并从 encrypted Secret row 返回 plaintext；当前实现使用
   普通 `select(Secret).where(...)`，属于 consistent read，不能直接满足本 ADR
   为 candidate 规定的 current-read freshness。
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

Phase 2B-remain-1B 应在 provider contracts 中新增（示例省略已有的
`dataclasses.field` import）：

```python
@dataclass(frozen=True, slots=True)
class XrayOutboundDTO:
    tag: str
    host: str
    port: int
    protocol: str
    credential_secret_ref: str = field(repr=False)
```

字段语义：

- `tag` 是由 active `GatewayRouteBinding.outbound_tag` 提供的稳定 outbound
  标识。
- `host`、`port`、`protocol` 来自 active route 关联的 egress endpoint。
- `credential_secret_ref` 是经过 precedence 选择的 opaque secret handle，
  不是 secret plaintext。
- DTO 不包含 plaintext username、plaintext password，也不包含
  `CredentialDTO`。它可以在 DB desired snapshot、domain/provider contract
  和测试 fixture 中流转；这不授权把 raw `credential_secret_ref` 写入普通
  logs、exceptions、metrics 或 audit serialization。需要安全审计关联时，
  只记录不可逆 fingerprint 与非 secret entity identifier。
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

在 live cutover 完成后，`DesiredRoutingState` 的唯一语义是“当前完整 DB
desired snapshot”，不是一次 provisioning 新增的一条 delta route。当前 main
仍有 route-only 的 legacy producer；它是本 ADR 已知的实现缺口，不得在任何
中间 PR 中被误当作已经满足最终 contract。

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
5. 明确 snapshot 与 DB transaction 的顺序，并保持 ADR-017 的既有事务边界：
   `GET_LOCK`
   → `GatewayRouteBinding` mutate/flush
   → construct fresh full desired snapshot
   → resolve credentials
   → render
   → validate
   → apply
   → `db.commit()` 或 `db.rollback()`
   → `RELEASE_LOCK`。render/apply 前不得为了制造 snapshot 而提前 commit；
   snapshot 可以包含当前 transaction 自己已经 flush 但尚未 commit 的 route、
   binding 和 Secret mutation。

   “同一个 Session 再普通 SELECT 一遍”不是 freshness contract。生产 MySQL 8.4
   的 InnoDB 默认是 `REPEATABLE READ`；Phase A 较早的普通 consistent read
   可能已经建立旧 read view，使后续普通 SELECT 看不到另一个 writer 在本操作
   等待 named lock 期间已经 commit 的 route。remain-3 的 full-snapshot 查询、
   egress endpoint/binding 选择和 credential Secret lookup 必须统一使用
   MySQL current/latest-committed read semantics，即采用
   `SELECT ... FOR UPDATE` 的 locking-read 语义或 SQLAlchemy 中明确等价的
   current-read 形式，并在与 route mutation 相同的 Session/transaction 中执行。
   对 ORM identity map 中可能已由旧 read view 加载的对象，current read 必须强制
   refresh/populate-existing 或使用等价的不会复用旧对象状态的机制。

   本 ADR 选择“保持 REPEATABLE READ + 对 candidate 所有 DB reads 使用
   current/locking read”的方案，而不是全局或隐式切换 isolation level。因而
   `SqlAlchemyCredentialResolver` 不得把当前普通
   `reveal_secret(db, secret_ref)` 原样用于 candidate path；它必须通过显式
   current-read Secret lookup（例如 `FOR UPDATE` 加 ORM refresh，或严格等价
   的 infra helper）读取 encrypted Secret row，再解密并校验。current read
   必须同时可见当前 transaction 自己的 flushed/uncommitted writes；精确 JOIN、
   `with_for_update()` 作用范围、refresh 选项和索引留给 remain-3，但
   correctness semantics 在本 ADR 中已经固定。

   named lock 仍是 `GatewayRouteBinding` writer serialization mechanism；
   current/locking read 只解决 freshness，不替代 ADR-016/017 的 named-lock
   写者串行化，也不得擅自全局修改 isolation level。典型 A/B 交错中，A 在
   Phase A 建立旧 snapshot，B 在 named lock 内完成并 commit route、binding 和
   Secret 后释放锁，A 获锁后执行上述 fresh current reads，最终 candidate 必须
   同时包含 B 已提交的 route、B 选中的 `credential_secret_ref`、B 的 Secret
   plaintext，以及 A 自己 pending 的 route 和 Secret。若 current read、preflight、
   resolve 或 render 任一步失败，必须在同一 lock span 内 fail closed 并 rollback，
   然后才 release lock。

本 ADR 不决定具体 JOIN、索引或 transaction 代码；这些是 remain-3 的实现细节，
但不得削弱这里规定的 current-read、own-write visibility、lock-span 或
no-pre-apply-commit correctness contract。

### 2A. remain-1B / remain-3 sequencing

本 ADR 选择“先安全基础设施、后一次性 live cutover”的方案，以避免出现
“最终 DesiredRoutingState contract 已合并，但生产 producer 仍只返回 delta”
的中间 main：

- remain-1B 只允许落地 dormant `XrayOutboundDTO`、`CredentialResolver` /
  stable error contract、CredentialDTO/CandidateConfig safe repr、credential
  redaction 和 operation dependency wiring。它不得替换 live
  `DesiredRoutingState.outbound_tags`，不得把当前
  `SqlAlchemyProvisioningState.desired_routing_state()` 改成宣称 full snapshot，
  也不得迁移依赖旧 route-only producer 的 live caller。
- remain-3 必须是一个连贯的 cutover PR，先实现 deterministic full-snapshot
  query、route/endpoint/binding/Secret current-read freshness、credential precedence
  和全量 preflight，再在同一 PR 中把 `DesiredRoutingState` 的 live producer、
  `outbound_tags -> outbounds`、GatewayProvider caller 和全部测试/调用方一起切换。
  这些变更不得拆成会在 main 上留下不一致 contract 的可合并中间阶段。
- `GatewayProvider.render(desired, resolver)` 的 operation API 可以在 remain-1B
  作为安全边界落地，但在 remain-3 cutover 前，desired 参数仍必须保持旧
  route-only representation；Xray registry 继续不可选。任何 PR 都不得让
  `outbounds` final shape 与 route-only producer 同时成为可合并的 live source
  of truth。
- 若未来某个 PR 选择把基础设施与 full snapshot 合并实施，也必须作为一个
  原子 cutover 满足 remain-3 的全部条件；不得把本节的依赖顺序解释成允许提前
  合并不完整的 live contract。
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
- application boundary 创建 `SqlAlchemyCredentialResolver(db)`，其中
  `db` 必须是当前 provisioning operation 同时传给
  `SqlAlchemyProvisioningState`（或其 `_OrderProvisioningState` 包装层）的
  同一个 SQLAlchemy Session。它只创建一次、只服务这一 operation，并由
  application/session owner 随 Session 一起结束；`build_services()` 不创建
  resolver，也不接受 factory 或 fallback。
- `build_services(..., credential_resolver: CredentialResolver)` 将这个必需的
  operation dependency 传给 `ProvisioningService`；`ProvisioningService`
  只在 operation 生命周期内持有它，并在
  `provision_apply_gateway()` 中调用
  `self.gateway.render(desired_routing, self.credential_resolver)`。
  application-level `provision()` 与 `confirm_payment_and_provision()` 也必须
  要求并透传该 resolver。domain 不执行解密，只传递 provider contract capability。
- `GatewayProvider` 的最终签名固定为
  `render(desired: DesiredRoutingState, resolver: CredentialResolver) ->
  CandidateConfig`。`XrayFileProvider` 不在构造函数或实例字段中保存 resolver；
  它仅从 render 参数使用 operation-scoped resolver，然后把得到的 transient
  `CredentialDTO` 交给纯 renderer。
- `ProviderRegistry.gateway` 继续是 process/application-lifetime 的
  stateless provider owner；registry 不持有 resolver，`XrayFileProvider`
  也不持有 Session、ORM 或 resolver state。这样 process-lifetime registry
  与 operation-lifetime secret/session 生命周期不会冲突。
- 直接构造 `ProvisioningService` 的单元/集成测试必须传入 fake
  `CredentialResolver`；`build_services()` 的 wiring test 也必须显式传入
  fake。没有 implicit resolver、全局 singleton 或测试专用旁路。
- `SqlAlchemyCredentialResolver` 是 `backend/app/infra/` 的唯一实现，
  持有同一个 Session，并在 candidate path 使用显式 current-read Secret lookup；
  不得原样调用当前普通-read `reveal_secret()`。此前 `save_credentials()` 已在
  该 Session 上 flush；因此本 operation 刚写入的 Secret row 对 resolver 可见，
  不需要提前 commit。current read 同样必须保留当前 transaction 自己的 writes。
- 纯 Xray renderer 只接收已解析的 transient credential/route value；它不能
  看到 Session。带 Session 的脚本入口只能是 orchestration/infra 层，必须在
  调用纯 renderer 前完成查询和 resolver 组装。
- domain 层不解密、不解析 secret JSON、不保存 `CredentialDTO`；它只组织
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

- `XrayOutboundDTO.credential_secret_ref` 必须声明为
  `field(repr=False)`（或严格等价的安全实现）；raw ref 不得出现在
  `repr(XrayOutboundDTO)`。未来的 `CredentialDTO` 必须同时把
  `username` 与 `password` 从默认 repr 排除，并提供安全 custom repr；
  只把 password 标成 `repr=False` 不足以满足本 ADR。
- `DesiredRoutingState` 必须提供安全 repr contract：它不能通过嵌套
  `XrayOutboundDTO` 间接展示 raw ref，也不能展示任何 CredentialDTO
  plaintext。实现可以使用安全 custom repr，或证明所有嵌套字段均为安全 repr；
  remain-1B 测试必须锁定该行为。
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
| Gateway provider | consume desired snapshot; receive the operation-scoped resolver only as the render argument; render/validate/apply through safe runtime boundary | Session/ORM imports, DB queries, resolver fields, secret logging |
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

- DTO contains refs and connection details but never plaintext; its repr does not
  expose raw `credential_secret_ref`.
- full snapshot includes all active bindings and rejects partial/orphan/duplicate state.
- a real MySQL 8.4 concurrency test covers the A/B interleaving and proves one
  candidate sees B's committed route, B's selected ref, B's resolved Secret
  plaintext, and A's own pending route and Secret; it also proves no pre-apply
  commit is used and that no old Secret read view is reused.
- sentinel redaction tests use `sentinel_username`, `sentinel_password`, and
  `sentinel_secret_ref`: applicable `repr(XrayOutboundDTO)`,
  `repr(DesiredRoutingState)`, `repr(CredentialDTO)`,
  `repr(CandidateConfig)`, error text, structured logs, and audit payloads
  must not expose the relevant sentinel values; CredentialDTO tests cover both
  plaintext fields, while outbound/state/candidate and operational-output tests
  cover the secret ref and any plaintext that can reach them.
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
2. In remain-1B, add only the dormant DTO/security infrastructure and operation
   dependency described in section 2A; do not cut over the live desired-state producer
   or `outbound_tags`.
3. Fix the operation API exactly as `GatewayProvider.render(desired, resolver)`;
   create the resolver at the application boundary from the same Session used by
   provisioning state, pass it through required `build_services()` /
   `ProvisioningService` dependencies, and never store it in the registry/provider.
4. Implement `XrayOutboundDTO`, CredentialDTO safe repr, CandidateConfig safe repr,
   stable resolver errors and current-read Secret lookup without exposing Session to
   gateway providers/renderers. The current plain-read `reveal_secret()` helper
   is not sufficient for the candidate path until adapted or bypassed by the infra
   current-read helper.
5. In remain-3, implement deterministic full-snapshot query/preflight, route/endpoint/
   binding/Secret freshness, precedence, and the live `outbound_tags -> outbounds`
   cutover together in one coherent PR. Preserve ADR-014 ownership and ADR-016 route
   identity; no mergeable intermediate may claim full snapshot while producing a delta.
6. Treat existing `ops/gateway/render_xray_routes.py` coupling as an explicit
   refactor requirement; do not bypass this ADR by passing a Session into providers.
7. Keep Phase 2C and `GATEWAY_PROVIDER=xray_file` registry wiring paused until Phase
   2B is complete and independently reviewed.
8. Historical Issue #97 and PR #98 are closed artifacts; PR #98 was never merged.
   Do not reopen or modify them in remain-1A/1B. Do not change production
   deployment, credentials, workflows, or other historical tracking artifacts
   unless a later task explicitly authorizes it.

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