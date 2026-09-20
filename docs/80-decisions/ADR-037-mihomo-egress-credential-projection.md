# ADR-037: Mihomo 出站链路契约 —— egress→transport binding 与 egress credential identity

- Status: **Accepted**
- 状态：已接受
- 日期：2026-09-20
- 范围：S04 最终产品链路的定位；`EgressTransportAssignment` 与
  `EgressBinding.credential_secret_ref` 的 provider-neutral 表示；
  它们进入 `DesiredForwarderState` / canonical manifest 的方式；
  plaintext resolve 边界；Mihomo document 的链式表达
- 承接 / 部分 supersede：**ADR-019**（Xray desired state 与 credential boundary）——
  credential 部分逐条对齐它；**但 §1 的 `credential_secret_ref` 非可空结论
  在 Mihomo 模式下被本 ADR §1a.4 supersede**，逐条对照见 §1a.3
- 补充：ADR-023（全量渲染与三类输入）、ADR-024（transport 多 provider 所有权）、
  ADR-025（generation authority + canonical manifest）、ADR-035（deployment 常量）。
  **不取代其中任何一条**
- 触发：`AGENTS.md` 铁律 5 —— 本决定会修改 `backend/app/providers/base.py`
  的 provider-neutral contract，必须先有 ADR
- 实施 TASK：`docs/82-tasks/TASK-S04-mihomo-activation.md`。**唯一安全顺序**
  （§5a 的不变式决定，不得重排）：
  **`B2-B2a` 契约 substrate → `B2-B2` loader/preparation（#163，需 rebase）
  → `B2-B2c` render/finalization → `B2-B2d` Xray 交接 → `B2-B3` → `B2-B4`
  → writer（尚无 owner）→ `S04-C` 放行 + 激活闸门**

> **本 ADR 不实现任何代码。** 本 ADR 合并进 `main` 之后，**第一个可以开工的是
> `B2-B2a`**（纯契约）—— 不是 B2-B2c，也不是 #163。理由见 §5a：任何能生产
> generation 的 checkpoint，其 manifest 必须已覆盖本 ADR 的全部 effective inputs。

## 0. 本 ADR 推翻了什么（先说清楚，不埋着）

**我在 PR #164 里对这两个问题的裁决是 Q1 = NO、Q2 = NO。本 ADR 把两个都改成 YES。**

两条的作废理由**不一样**，分开记：

| | 旧裁决 | 作废理由 |
|---|---|---|
| **Q2**（credential） | NO：「凭据当前流向 Xray，Mihomo 侧无人读它」 | **实测没错，用它回答这个问题错了。** 「今天的代码把它送去哪」是实现现状，「Mihomo projection 是否必须覆盖它」是架构问题。我让前者决定了后者，方向反了 |
| **Q1**（assignment） | NO：「这张表没有 writer/reader/ADR，语义从未定义，故不是 effective input」 | **前半句是事实，结论是回避。** 「语义从未定义」是**本 ADR 要解决的问题**，不是把它判成 unsupported 的理由。ADR-023 §1.1 允许 unsupported 分类，但那是给**确实无法表达**的字段用的；这条链路是可以表达的，只是需要有人定下来——那正是 ADR 的职责 |

**Q1 的改判由 User 于 2026-09-20 决定**：S04 要实现的是下面 §1 的链路 B。
本 ADR 把该决定连同它的全部可实施细节固定下来。

## 1. 最终产品链路（**裁定：链路 B**）

**必须消除的两种互相冲突的理解：**

```
A（否决）  客户端 → Xray → 住宅 egress → 目标
B（采纳）  客户端 → Xray → Mihomo listener → transport node → 住宅 egress → 目标
```

**最终链路 B，逐段落到具体载体：**

| 段 | 载体 | 依据 |
|---|---|---|
| 客户端接入 VPS | **Xray**，per-user 路由 | `GatewayRouteBinding.gateway_principal` → `outbound_tag`；`provisioning_state.py:398-403` |
| Xray → Mihomo | Xray outbound 指向 `127.0.0.1:{EgressEndpoint.mihomo_listen_port}` | `mihomo_listen_port` 为 `unique=True`，**每个出口一个**（`models/egress.py:194`） |
| Mihomo listener | 每个住宅出口一个 `mixed` listener | `ARCHITECTURE.md` §8：**「Mihomo listener 数 == 数据库出口数」** |
| **Mihomo → transport node** | ACTIVE `EgressTransportAssignment` 选定的**订阅节点**（materialized proxy） | 本 ADR §2 |
| **transport node → 住宅 egress** | Mihomo 以 transport node 为 dialer，拨通住宅 socks5 | 本 ADR §2.4 |
| 最终出口 | 住宅 ISP IP | `ARCHITECTURE.md` §8：**「每个 listener 出口 IP == 数据库记录 IP」** |

**为什么否决 A（两条硬证据，不是偏好）：**

1. **A 会让 transport materialization 成为死配置。** 仓库已经把订阅 cache 的内容
   materialize 成 Mihomo proxies（`mihomo_projection.py:411-421`
   `effective_proxies = desired.proxies + materialized_proxies`）。链路 A 下
   **没有任何 listener 或 rule 会选中它们** —— 整套 receipt / freshness /
   cache-identity 机制（ADR-025 §3a 的全部内容）就白做了。
2. **A 会让 `EgressTransportAssignment` 永远没有意义。** 一张 egress→transport
   node 的绑定表，在「Mihomo 直连住宅」的链路里无处可用。

**B 与 §8 的两条验收都相容**：listener 数仍等于出口数；最终出口 IP 仍是住宅 IP
（transport node 只是中继，不是出口）。

> **一个必须记住的约束**：`mihomo_listen_port` 是**每出口唯一**的。
> 因此「哪个客户走哪个住宅出口」由 **Xray 选 listener 端口**决定，
> **不由 Mihomo 决定**。Mihomo 不做 per-subscription 路由 ——
> 这就是 PR #163 里 `subscription-{id}` proxy-group 必须撤掉的根本原因。

## 1a. Xray → Mihomo 的交接契约（**链路 B 必须改 Xray，这一节把它定死**）

> **这一节是 2026-09-20 审查补上的。** 初稿选了链路 B，却同时在 §8 写着
> 「不改 Xray 既有行为」—— **两句不能同时成立**，而且 §1 的链路在当前代码下
> **根本不可达**。

### 1a.1 当前实现（实测，不是推测）

`backend/app/infra/provisioning_state.py::full_desired_routing_snapshot()`
（`:296`）构造的是：

```python
# provisioning_state.py:398-403
outbound = XrayOutboundDTO(
    tag=route.outbound_tag,
    host=endpoint.host,        # ← 住宅 egress 的 host
    port=endpoint.port,        # ← 住宅 egress 的 port
    protocol=endpoint.protocol,
    credential_secret_ref=selected_ref,
)
```

**Xray 直接拨住宅 egress，永远不会去 `127.0.0.1:{mihomo_listen_port}`。**
因此即使 B2-B2c 与当前定义的 S04-C 全部实现完，**链路 B 仍然不可达**。

### 1a.2 裁决：Mihomo 模式下 Xray outbound 指向 loopback

> **`FORWARDER_PROVIDER=mihomo` 生效时，每个 active route 的 Xray outbound
> 必须指向 `127.0.0.1:{该 route 对应 EgressEndpoint.mihomo_listen_port}`，
> protocol 为 `socks`，且 `credential_secret_ref` 为 `None`。**
>
> **住宅 egress 的 `host`/`port`/凭据不再出现在 Xray 这一跳** ——
> 它们属于 **Mihomo → 住宅**那一跳，由 §3 的 `egress_proxies` 承载。

**非 Mihomo 模式（`FORWARDER_PROVIDER != "mihomo"`）保持现状不变**：
Xray 直拨住宅并解析凭据。两种模式的边界必须由测试证明，见 §1a.5。

### 1a.3 凭据归属：本 ADR supersede 了 ADR-019 的哪一部分

**必须逐条说清楚，不能笼统说「沿用 ADR-019」。**

| ADR-019 的结论 | 在 Mihomo 模式下 |
|---|---|
| §1 `XrayOutboundDTO` 的 `tag` / `host` / `port` / `protocol` 语义 | **沿用**，只是 `host`/`port` 现在指向 loopback Mihomo listener |
| §1 `credential_secret_ref: str`（**非可空**） | **被 supersede** —— 见 §1a.4 |
| §4 plaintext lifecycle（明文只在 resolver→renderer→runtime 文件） | **沿用**，一个字不改 |
| §7 credential precedence（binding override → endpoint 回落） | **沿用**，但它现在服务的是 **Mihomo→住宅**那一跳（本 ADR §3.2），**不再**是 Xray→住宅 |
| §2 full-snapshot invariant / current-read / lock span | **沿用**，一个字不改 |
| §5 CandidateConfig redaction、§6 secret payload 格式 | **沿用** |

> **为什么 Xray→Mihomo 这一跳不需要凭据**：Mihomo 的 `mixed` listener 绑在
> `127.0.0.1`，**外部不可达**；能连到它的进程已经在这台机器上了。
> 给这一跳再造一份 secret 只增加轮换面与误配置面，**换不到任何安全性**。

### 1a.4 `XrayOutboundDTO.credential_secret_ref` 变为 `str | None`

```python
credential_secret_ref: str | None = field(repr=False, default=None)
```

**`None` 的合法性由一条硬规则约束，不是想省就省：**

| 条件 | 要求 |
|---|---|
| `host` 是 loopback（`127.0.0.1`）且 port 等于某个 `mihomo_listen_port` | `credential_secret_ref` **必须**为 `None` |
| 其它任何 outbound | `credential_secret_ref` **必须**非空；为 `None` ⇒ `XRAY_OUTBOUND_CREDENTIAL_REQUIRED` fail closed |

**composer 与 resolver 的相应改动：**

- `xray_composition.py:385-389` 当前对每个 outbound 强制要求可解析的
  username/password ⇒ 必须改为：`credential_secret_ref is None` 时渲染
  **不带 `users` 的 socks outbound**；非 `None` 时行为完全不变。
- `xray_file.py:129-134` 当前对**每个** outbound 都 `resolver.resolve()` ⇒
  必须跳过 `None`，**且不得因此放宽对非 `None` 的解析失败处理**。

### 1a.5 这条改动的四轴范围闭包（**已实测，不要重做**）

沿四条轴各走一遍，对着 `main` 实测 `XrayOutboundDTO` 的**全部**出现处：

| 轴 | 结果 |
|---|---|
| **函数 / 协议** | `providers/base.py`（DTO 定义）、`provisioning_state.py`（唯一生产构造点）、`xray_composition.py`（消费）、`xray_file.py`（消费）、`ops/gateway/render_xray_routes.py`（ops 入口，**也构造 DTO**） |
| **数据库列** | 不新增、不修改任何列。`mihomo_listen_port` 已存在且 `unique=True` |
| **API 契约** | 无 API 表面变化 |
| **渲染字段** | Xray outbound 的 `settings.servers[].users` 在 loopback 跳上消失；其余不变 |

**实测到的 `XrayOutboundDTO` 构造/引用文件共 13 个**：

```
backend/app/providers/base.py
backend/app/infra/provisioning_state.py
backend/app/providers/gateway/xray_composition.py       # 经 DesiredRoutingState 消费
backend/app/providers/gateway/xray_file.py              # 经 DesiredRoutingState 消费
ops/gateway/render_xray_routes.py
backend/tests/unit/test_desired_routing_snapshot.py
backend/tests/unit/test_xray_composition.py
backend/tests/unit/test_xray_render.py
backend/tests/unit/test_xray_provider_standalone_parity.py
backend/tests/unit/test_credential_infra.py
backend/tests/unit/test_domain.py
backend/tests/unit/test_services_wiring.py
backend/tests/guards/test_xray_writer_guard.py
backend/tests/guards/test_safe_reload.py
backend/tests/integration/test_provisioning_phase_boundary.py
```

> **默认值 `= None` 是刻意的**：它让上面十几个只关心 tag/host/port 的构造点
> **不需要逐个改**。只有真正涉及 loopback 语义的地方才需要动。

**另有一条已知的连带约束**：
`backend/tests/unit/test_gateway_reconciliation_contract.py:39` 断言源码里存在
字面量 `"full_desired_routing_snapshot(db)"` —— **不要重命名这个函数**。

### 1a.6 这属于哪个 checkpoint

**不属于 B2-B2，也不属于 B2-B2c。** 它是一个独立的 checkpoint（TASK 里命名为
**B2-B2d**），因为它动的是 **gateway（Xray）侧**，与 forwarder 侧的
`egress_proxies` 是两组互不重叠的文件。

**依赖**：`B2-B2c`（egress_proxies 存在）→ `B2-B2d`（Xray 交接）→ `S04-C`（激活）。

**B2-B2d 不放行 registry**，`FORWARDER_PROVIDER=mihomo` 在它结束时**仍被拒绝**；
它只保证「一旦放行，链路 B 是通的」。


## 2. `EgressTransportAssignment` 的语义与表示（Q1 = YES）

### 2.1 ACTIVE assignment 表示什么

> **一条 `state == ACTIVE` 的 `EgressTransportAssignment` 表示：
> 该 `egress_id` 的出站流量，必须经由 `transport_node_id` 指向的那个
> transport 节点中继出去。**

三列的职责：

| 列 | 职责 |
|---|---|
| `egress_id` | 被约束的住宅出口（→ `EgressEndpoint`） |
| `transport_provider_id` | 该节点所属的订阅 provider（→ `TransportProviderRecord`），**用于完整性校验，且必须与 node 的 `provider_id` 一致** |
| `transport_node_id` | 选定的中继节点（→ `TransportEndpointRecord`） |

`state in {DRAINING, RELEASED}` 是**历史状态**：不进 snapshot、不进 manifest、
不参与任何校验。

**每个 in-scope egress 至多一条 ACTIVE assignment**；出现两条即
`MIHOMO_TRANSPORT_ASSIGNMENT_AMBIGUOUS` fail closed。

**关于「没有 ACTIVE assignment 的 egress」——分阶段，且终点是 fail closed：**

| 阶段 | 语义 |
|---|---|
| **B2-B2 / B2-B2c / B2-B3 / B2-B4（未激活）** | **允许**。该 egress 的 proxy 不带 `dialer-proxy`。这是**未激活阶段的过渡状态**，便于分段实现与测试 |
| **S04-C 生产激活及其之后** | **禁止**。见 §2.1a 的激活闸门 |

> **⚠️ 这条限定是 2026-09-20 审查补上的，原文只写了「允许，不是错误」。**
> 那样写会让过渡态一路带进生产：激活后部分出口绕过 transport 直连住宅，
> **实际退回被否决的链路 A**，而且没有任何机制会报错。

### 2.1a 生产激活闸门（fail closed）

> **在 `FORWARDER_PROVIDER=mihomo` 被 registry 放行之前，必须机械验证：
> 每个 in-scope egress（`status IN (AVAILABLE, ASSIGNED, DEGRADED)`）
> 有且仅有一条 `state == ACTIVE` 的 `EgressTransportAssignment`。**
>
> 缺失或多于一条 ⇒ **拒绝激活**，不得放行 registry。

**这条闸门属于 S04-C，不属于 B2-B。**

### 2.1b ACTIVE assignment 的 writer —— **尚无 owner，这是一条已命名的未决项**

**必须说清楚，不能含糊过去：** 本 ADR 定义的是 **reader 语义**。
**当前仓库里没有任何代码写这张表**（实测：除 model 定义与迁移外零引用），
本 ADR **也不定义 writer**。

**不定义的原因是它是一个独立的决策，不是因为不重要**：
「哪个 egress 配哪个 transport node」涉及容量、健康度、地域、重平衡策略——
这些都没有被任何 ADR 或 TASK 决定过。**我不在这里发明一套分配算法。**

**但这不是「以后再看」**，因为 §2.1a 的闸门把它变成了硬约束：

> **在 writer 存在并真正产出 ACTIVE assignment 之前，Mihomo 生产激活被 §2.1a
> 机械阻断。** 不存在「先激活、assignment 以后补」的路径。

**依赖关系**：`writer checkpoint` → `S04-C 激活`。
**禁止**：人工改库、隐藏 seed、或「表以后自然会有数据」。
writer 的分配策略需要它自己的规格（TASK 或 ADR，由 User 决定何时做）。

### 2.2 assignment 如何对应到一个 materialized proxy

这是本节最容易被实现者猜错的一步，**写死**：

```
EgressTransportAssignment.transport_node_id
  → TransportEndpointRecord(id, provider_id, name, host, port)
  → 在该 provider 的 receipt-verified cache 内容里，
    按 (name, host, port) 三项全等 定位唯一一个 materialized proxy
```

**为什么 `(name, host, port)` 三项而不是只用 name**：
`transport_sync.py:42-75` 把 `TransportEndpointRecord.name/host/port` 直接写自
`provider.list_endpoints()`，与 cache 内容同源，所以三项应当一致；**要求三项全等
是为了在不一致时立刻发现**，而不是静默接上一个错节点。

**fail-closed 规则：**

| 情况 | 错误码 |
|---|---|
| `transport_node_id` 指向的 `TransportEndpointRecord` 不存在 | `MIHOMO_TRANSPORT_ASSIGNMENT_NODE_MISSING` |
| `assignment.transport_provider_id != endpoint.provider_id` | `MIHOMO_TRANSPORT_ASSIGNMENT_PROVIDER_MISMATCH` |
| `assignment.egress_id` 不在本次 in-scope 出口集合内 | `MIHOMO_TRANSPORT_ASSIGNMENT_EGRESS_UNKNOWN` |
| 在 cache 内容里按三元组找不到唯一匹配（0 个或 >1 个） | `MIHOMO_TRANSPORT_PROXY_UNRESOLVED` |
| 同一 egress 有 >1 条 ACTIVE | `MIHOMO_TRANSPORT_ASSIGNMENT_AMBIGUOUS` |
| 该 provider 的 receipt 缺失/不匹配 | 复用 B2-B1 既有的 receipt fail-closed 路径 |

**任何一条不满足 ⇒ 整份候选失败**，不得产出「少了某个出口」或「某个出口悄悄直连」
的部分配置（铁律 1）。

### 2.3 进 `DesiredForwarderState` 的形状

见 §4 的统一 DTO。assignment 贡献的是 `ForwarderEgressProxyDTO.transport_proxy_name`
—— **一个 provider-neutral 的字符串名字**，不是 ORM 对象、不是 DB id。

> **为什么带的是 proxy name 而不是 `transport_node_id`**：
> manifest 必须是 **Mihomo document 的身份**，不是 DB 主键的身份。
> node A→B 会换掉 name（和 host/port），fingerprint 因此必变；
> 而单纯的行 id 变动（例如重建同一节点的记录）**不该**触发重渲染。
> 同时 §5 的 manifest 里**另外**保留 `transport_node_identity` 三元组，
> 使「同名不同 host」这种情况也能被 fingerprint 捕获。

### 2.4 Mihomo document 怎么表达这条链

**唯一编码：在 egress proxy 条目上使用 `dialer-proxy`。**

```yaml
proxies:
  # 1) 订阅节点（materialized，来自 receipt 校验过的 cache）
  - name: <transport proxy name>
    ...                       # 形状由订阅源决定，本仓库不拥有

  # 2) 住宅出口（repo-owned）
  - name:         <EgressEndpoint.code>
    type:         socks5
    server:       <EgressEndpoint.host>
    port:         <EgressEndpoint.port>
    username:     <resolved plaintext>
    password:     <resolved plaintext>
    dialer-proxy: <transport proxy name>   # ← 没有 ACTIVE assignment 时整个键省略

listeners:
  - name:   listener-<code>
    type:   mixed
    listen: 127.0.0.1
    port:   <EgressEndpoint.mihomo_listen_port>
    proxy:  <code>
```

**禁止的替代编码**（不得由实现者自选）：
proxy-group 技巧、`proxy-providers` 链、把 transport 节点塞进 listener 的 `proxy`、
或任何以命名约定隐含链路的写法。**只有 `dialer-proxy` 一种。**

> **实现期必须做一次运行时确认**：`dialer-proxy` 必须在
> `infrastructure/compose/compose.transport.yml` 钉住的
> `metacubex/mihomo:v1.19.27` 上被 candidate validation 接受。
> **本 ADR 未在该镜像上实测过这一条** —— 若 B2-B2c 的 candidate 校验发现该版本
> 不支持，**停止并报告 User**（ADR-036 §2），不得自行改用其它编码。

## 3. `EgressBinding.credential_secret_ref` 的语义（Q2 = YES）

### 3.1 为什么是 YES

`ARCHITECTURE.md` §8 要求**流量经 Mihomo listener 出去后公网 IP == 该出口的数据库
记录 IP** —— 即 Mihomo 必须亲自拨通住宅出口。而住宅出口是**需认证的 socks5**
（`EgressEndpoint.protocol` 被 `xray_composition.py:383` 限定在 `{socks, socks5}`）。
当前 Mihomo proxy 条目只有 `{name, type, server, port}`，**没有凭据字段，
永远过不了那行验收**。凡 Mihomo 拨号所必需的输入，按 ADR-025 §4 即 effective input。

### 3.2 优先级：**直接复用 ADR-019 §7，不新立规则**

```
active EgressBinding.credential_secret_ref  （非空时）
  否则
EgressEndpoint.credential_secret_ref
```

- 只考虑 `released_at IS NULL` 的 active binding；
- `NULL` / 空字符串 = 没有 override，**允许**回落 endpoint-level；
- **whitespace-only、指向不存在 Secret、或 revision 非法的非空 override 是
  malformed，必须 fail closed，禁止静默回落 endpoint ref**
  （否则会把客户接到另一份凭据上）；
- endpoint-level ref 为空或 malformed 同样 fail closed。

**与 Xray 共用同一份 precedence 是刻意的**：同一出口在两条路径上必须选出同一个
credential，否则会出现「Xray 认为用 A、Mihomo 实际用 B」的分裂。

### 3.3 `Secret.revision` 属于 manifest identity —— 是，且写死细节

| 项 | 值 |
|---|---|
| **exact ref** | §3.2 precedence 选出的那一个 opaque ref（binding override 或 endpoint 回落） |
| **exact purpose** | **不做 purpose 绑定** —— 复用 `SqlAlchemyCredentialResolver`（`credential_resolver.py:72`）当前的 purpose-unbound 语义，与 Xray 读同一行。**这是既有属性，本 ADR 不改它**；要改需单独评估（见 §8） |
| **fresh-read** | `_current_secret_statement()` 的 current/locking read（`FOR UPDATE` + `populate_existing`），**与 snapshot 在同一个 Mihomo projection 命名锁 span 内** |
| **进 manifest 的** | `credential_secret_ref`（opaque）+ `credential_revision`（正整数；`bool` 不算） |
| **plaintext resolution 时点** | **render 边界**，见 §6 |

## 4. provider-neutral contract（**唯一结构，不得替换**）

在 `backend/app/providers/base.py` 新增，与既有 `ForwarderListenerDTO` 并列：

```python
@dataclass(frozen=True, slots=True)
class ForwarderEgressProxyDTO:
    name: str
    protocol: str
    host: str
    port: int
    credential_secret_ref: str = field(repr=False)
    credential_revision: int = 0
    transport_proxy_name: str | None = None
    transport_node_identity: tuple[str, str, int] | None = None
```

并在 `DesiredForwarderState` 新增：

```python
egress_proxies: tuple[ForwarderEgressProxyDTO, ...] = ()
```

**字段语义逐条固定：**

| 字段 | 语义 |
|---|---|
| `name` | `EgressEndpoint.code`。**必须与某个 `ForwarderListenerDTO.proxy` 完全一致** |
| `protocol` / `host` / `port` | 同一 `EgressEndpoint` 的三列 |
| `credential_secret_ref` | §3.2 precedence 选出的 opaque handle，**不是明文**。`repr=False` 与 ADR-019 的 `XrayOutboundDTO` 同形 |
| `credential_revision` | 该 ref 的 fresh `Secret.revision`，正整数 |
| `transport_proxy_name` | ACTIVE assignment 解析出的 materialized proxy 名；**无 ACTIVE assignment 时为 `None`** |
| `transport_node_identity` | `(name, host, port)` 三元组，来自 `TransportEndpointRecord`；`transport_proxy_name` 为 `None` 时同为 `None` |

**不可变性与排序：**

- `frozen=True, slots=True`，与同文件既有 DTO 一致；
- `DesiredForwarderState.__post_init__` 的 `freeze()` 只接受
  `str/int/float/bool/None` 与嵌套 Mapping/序列（`providers/base.py:258-264`）——
  上述字段**全部合规**，`tuple[str,str,int]` 亦然；
- **`egress_proxies` 按 `name` 升序稳定排序**，由 loader 保证；
  渲染与 manifest 都不得再次重排。

**与既有字段的关系（四条，全部必须成立）：**

| 关系 | 规则 |
|---|---|
| `DesiredForwarderState.proxies` | **语义收窄**为「transport materialization 产出的 proxies」。repo-owned 住宅出口**一律**走 `egress_proxies`。两类来源必须在**类型上**可区分 |
| `listener_specs` | 每个 `ForwarderListenerDTO.proxy` **必须**能在 `egress_proxies` 里找到同名项；找不到 ⇒ `MIHOMO_LISTENER_PROXY_UNBOUND` |
| `transport_references` / `transport_materializations` | 不变，仍是 ADR-025 §3a 的 receipt 证据。**`transport_proxy_name` 必须指向其中某个 receipt 所覆盖的 cache 内容里的 proxy**，否则 `MIHOMO_TRANSPORT_PROXY_UNRESOLVED` |
| 重名 | `_validate_sections()` 已有 `MIHOMO_DUPLICATE_PROXY_IDENTITY`，且它运行在**合并后的** `effective_desired` 上（`mihomo_projection.py:435` + `:220`），**已经覆盖跨来源重名**。本 ADR 不新增机制，只要求 `egress_proxies` 并入同一条校验路径 |

## 5. canonical manifest 表示

`build_projection_source_manifest()` 新增一个键：

```text
"egress_proxies": [
    {
      "name", "protocol", "host", "port",
      "credential_secret_ref", "credential_revision",
      "transport_proxy_name", "transport_node_identity"
    },
    ...
]   # 按 name 升序
```

**只有 opaque ref、整数 revision、节点名与三元组进 manifest。明文永不进。**
这与 ADR-025 §4 对 controller secret 的既有处理
（“controller-secret opaque reference and durable `Secret.revision`”）完全同形。

### 5.1 为什么 A→B 必须产生新 generation

| 变化 | manifest | fingerprint | generation |
|---|---|---|---|
| ACTIVE assignment 的 `transport_node_id` A→B | `transport_proxy_name` 与 `transport_node_identity` 变 | 变 | **必须新开一代** |
| 同一节点被订阅源改了 host/port | `transport_node_identity` 变 | 变 | **必须新开一代** |
| `credential_secret_ref` A→B | 变 | 变 | **必须新开一代** |
| 同 ref 的 `Secret.revision` N→N+1 | 变 | 变 | **必须新开一代** |
| 只改 Secret 明文、**不**递增 `revision` | 不变 | 不变 | **不新开** —— 见下面的告警 |

> **⚠️ 一个真实的失效面，写在这里免得以后靠踩坑发现：**
> **明文改了而 `Secret.revision` 没改，projection 不会重算，Mihomo 会继续用旧凭据**
> 直到别的原因触发新 generation。**凭据轮换路径必须递增 `Secret.revision`。**
> 这是既有契约的推论，本 ADR 只是点名。

## 5a. 落地顺序的不变式（**2026-09-20 审查补充，必须遵守**）

本 ADR 把 assignment 与 credential 定义为 **effective canonical-manifest
inputs**。这立刻产生一条对**实施顺序**的硬约束：

> **任何「已合并且能生产 generation」的 checkpoint，其 canonical manifest
> 必须已经覆盖本 ADR 定义的全部 effective inputs。**

**为什么这不是纸面问题**：`allocate_generation()` 在 `manifest_version` 与
`desired_fingerprint` 都相同时**复用**上一代（`mihomo_generation.py:104-109`）。
若先合入一个「已接线但 manifest 缺 `egress_proxies`」的生产者，则
**assignment 或 credential 改变 → fingerprint 不变 → 复用旧 generation**，
且这些 row 会**持久留在库里**。这违反 ADR-025 §4，也违反 `docs/85`
「每个 checkpoint 结束时已接线部分必须是 fail-closed 终态」。

### 5a.1 `MANIFEST_VERSION` 必须升版

manifest 新增 `egress_proxies` 键时，**`MANIFEST_VERSION` 必须由 `"1"` 升到
`"2"`**。否则同一个 `"1"` 会同时指代两种 manifest 语义，既有 row 会被按新契约
解读。升版后旧 row 明确属于「ADR-037 之前的形状」，**永远不会被新契约复用**。

### 5a.2 顺序由 TASK 固定，本 ADR 只给不变式

具体 checkpoint 划分写在
`docs/82-tasks/TASK-S04-mihomo-activation.md` 的「执行切分与唯一安全依赖顺序」。
本 ADR 只要求：**契约与 manifest 形状必须先于任何生产 generation 的接线落地。**

## 6. plaintext 边界（与 ADR-019 §4 同一条流水线）

```text
EgressBinding/EgressEndpoint.credential_secret_ref
  → 【loader】只取 ref + Secret.revision，不解密
  → DesiredForwarderState.egress_proxies        ← 仍然只有 opaque ref
  → canonical manifest / generation row          ← 仍然只有 opaque ref + revision
  → 【forwarder render / finalization 边界】operation-scoped CredentialResolver
  → transient CredentialDTO(username, password)
  → MihomoProjectionWriter 的 render()
  → transient CandidateConfig.content
  → 受权限保护的 Mihomo runtime config 文件
```

**规则：**

- **明文第一次出现，只能在 render 边界的 resolver 返回 `CredentialDTO` 时。**
- **loader 不解密**，与 ADR-035 §6 给 controller secret 定的边界一致。
- **resolver 复用已存在的 `SqlAlchemyCredentialResolver`，不新建。**
- resolve 必须与 snapshot 在**同一个 Mihomo projection 命名锁 span 内**；
  若 resolve 时读到的 `Secret.revision` **与 snapshot 的 `credential_revision`
  不一致** ⇒ 凭据在 render 期间被轮换 ⇒ **候选立即作废、fail closed、
  按 ADR-023 §2.2 重新 fresh-read 重试**，绝不用新明文配旧 fingerprint。
- operation 结束后不保留 resolver / `CredentialDTO` / candidate 的引用。

**明文绝不允许出现的位置（逐条列出）：**
loader、`DesiredForwarderState`、任何 desired DB row、canonical manifest、
generation row、transport receipt、日志 / 异常 / metrics /
structured logging、runtime-independent snapshot、PR 与文档正文。

## 7. 被否决的替代方案

| 方案 | 否决理由 |
|---|---|
| 链路 A（Mihomo 直连住宅，不经 transport） | 见 §1：会让 transport materialization 全套机制与 `EgressTransportAssignment` 同时失去意义 |
| 明文 username/password 进 `DesiredForwarderState` | 违反 ADR-023（snapshot 可流转于 DB/契约/测试）与 ADR-025 §4（manifest secret-free）；明文会进 fingerprint 输入 |
| 凭据当作 deployment constant | ADR-035 §2 的判据是逐**部署**不同，不是逐**出口**不同 |
| 往无类型 `proxies` mapping 里加键 | repo-owned 与 materialized 两类 proxy 会在类型上不可区分；`repr=False` 也无法在 mapping 上表达 |
| 只放 ref、不放 revision | 凭据轮换（ref 不变、明文变）将不产生新 generation |
| manifest 里放 `transport_node_id`（DB 主键） | manifest 是 document 的身份，不是主键的身份；重建同一节点的行会造成无谓重渲染 |
| 只按 proxy `name` 对应节点，不校验 host/port | 同名不同端的节点会被静默接上，且 fingerprint 无法察觉 |
| 用 proxy-group / 命名约定表达链路 | 隐式语义，无法校验；PR #163 的 `subscription-{id}` group 正是这条路的失败样本 |
| 让 Mihomo 复用 Xray 已解析的明文 | 跨 provider 传递明文，且两条路径 snapshot 边界不同，会产生第二个 desired-state 事实来源 |

## 8. 不属于本 ADR / 不授权

- **不改 `SqlAlchemyCredentialResolver` 的 purpose-unbound 现状**（ADR-019 的既有属性）。
  要给 egress credential 加 purpose 绑定，需单独评估——它会同时影响 Xray 路径。
- **不授权**生产激活、不授权 `build_registry()` 放行 `FORWARDER_PROVIDER=mihomo`、
  不授权部署、不授权创建或轮换任何真实 Secret。

> **⚠️ 本节初稿写着「不改 Xray 既有行为」，那句话是错的，已删除（2026-09-20 审查指出）。**
> §1 选定链路 B **必然**要求改 Xray 的 outbound 目标——两句不能同时成立。
> 正确的契约见 §1a。**我的失误很具体**：我读了
> `provisioning_state.py:398-403` 并把它当作 credential 归属的证据，
> 却没注意**同一段代码同时约束了链路走向**。用一处代码回答了一个问题，
> 忘了它对另一个问题同样有约束力。
- **不重新讨论 ADR-035 已裁定的 deployment constants。**

## 9. Testing requirements

精确测试名与断言见 TASK-S04 各 checkpoint 自己的测试表（本 ADR 不重复列名）。
**归属按 §5a 的顺序切分，不要都记在 B2-B2c 名下：**

| 归属 | 覆盖什么 |
|---|---|
| **B2-B2a** | DTO / manifest 契约本身：`egress_proxies` 按 `name` 排序进 manifest、`MANIFEST_VERSION` 由 `"1"` 升到 `"2"` 后旧 row 不被复用、`ForwarderEgressProxyDTO.__repr__()` 不含 `credential_secret_ref` |
| **B2-B2** | **fingerprint 不变式**：assignment node A→B ⇒ fingerprint 变 + 新 generation；credential ref A→B 及 `Secret.revision` 轮换 ⇒ 同上；credential precedence（override / 回落 / malformed 不回落）；loader 填充 `egress_proxies` **全程不解密** |
| **B2-B2c** | **render / finalization**：`dialer-proxy` 出现与省略、明文只在 render 边界出现、revision 漂移作废候选、协议白名单、跨来源重名 |
| **B2-B2d** | **Xray 交接**：Mihomo 模式下 outbound 指向 loopback + 对应 `mihomo_listen_port`、该跳不解析住宅凭据、非 Mihomo 模式边界、非 loopback 缺凭据 fail closed |

其中三条是本 ADR 的核心不变式，**缺一不可**（归属见上表）：

1. assignment node A→B ⇒ fingerprint 变（**B2-B2**）；
2. credential ref A→B（及 revision 轮换）⇒ fingerprint 变（**B2-B2**）；
3. manifest / snapshot / generation row 的任何序列化与 `repr()` **均不含明文**
   （**B2-B2a** 覆盖 DTO 层，**B2-B2c** 覆盖 render 层）。

## 10. 重新评估条件

- `ARCHITECTURE.md` §8 的两条 listener 验收被 User 改变或删除；
- 住宅出口改为不需认证，或改用非 socks5 协议；
- 钉住的 Mihomo 镜像不支持 `dialer-proxy`（见 §2.4 的实现期确认）；
- 出现「一个 egress 需要多条并行 transport 中继」的需求 ——
  当前契约是**每 egress 至多一条 ACTIVE assignment**，多路需要新 ADR；
- Mihomo 支持不含明文的 credential reference（届时可重评 §6 是否还需要明文
  进入 candidate，与 ADR-019 §4 末条同理）。
