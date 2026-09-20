# ADR-037: Mihomo projection 的 egress credential 契约

- Status: **Accepted**
- 状态：已接受
- 日期：2026-09-20
- 范围：`DesiredForwarderState` 的 repo-owned egress proxy 表示、egress
  credential 的 opaque identity 进入 canonical manifest 的方式、plaintext
  resolve 边界、以及 Mihomo document 对住宅出口的表达
- 承接：**ADR-019**（Xray desired state 与 credential boundary）——本 ADR 是
  它在 forwarder 侧的对应物，**刻意逐条对齐而不是另起一套**
- 补充：ADR-023（全量渲染与三类输入）、ADR-025（generation authority +
  canonical manifest）、ADR-035（deployment 常量落点）。**不取代其中任何一条**
- 实施 TASK：`docs/82-tasks/TASK-S04-mihomo-activation.md`（新 checkpoint **B2-B2c**）

> **本 ADR 不实现任何代码。** 它只关闭「Mihomo 怎样表达住宅出口的认证凭据」
> 这一个架构问题。B2-B2c 在本 ADR 合并后才可开工。

## 1. 为什么现在要这条 ADR

`ARCHITECTURE.md` §8 的部署验收里有两行**机器可执行**的要求：

```
[ ] Mihomo listener 数 == 数据库出口数
[ ] 每个 listener 出口 IP == 数据库记录 IP
```

第二行的含义是：**流量经过某个 Mihomo listener 出去之后，公网看到的 IP 必须
等于该出口在数据库里记录的 IP**。也就是说 **Mihomo 必须亲自拨通那个住宅出口**。

而住宅出口是**需要认证的 socks5**：

- `EgressEndpoint.protocol` 被 `xray_composition.py:383` 限定在 `{"socks","socks5"}`；
- Xray 侧必须提供 username/password 才能拨通（`xray_composition.py:385-389`
  把 `CredentialDTO` 写进 `outbounds[].settings.servers[].users`）。

**当前 Mihomo projection 渲染出的 proxy 条目只有 `{name, type, server, port}`，
没有任何凭据字段。** 因此当前形状的 projection **永远无法通过上面第二行验收**。

> **一次必须记下来的判断更正。** 我在 PR #164 里把
> 「`EgressBinding.credential_secret_ref` 变化是否必须改变 manifest」裁决为
> **NO**，理由是「实测该 credential 当前流向 Xray，Mihomo 侧没有任何一跳读它」。
>
> **那个实测是对的，但用它回答这个问题是错的。** 「今天的代码把它送去哪里」是
> **实现现状**；「Mihomo projection 是否必须覆盖它」是**架构问题**。我让前者
> 决定了后者——方向反了。`ARCHITECTURE.md` §8 是产品验收要求，它已经要求
> Mihomo 拨住宅出口；凡是 Mihomo 拨号所必需的输入，按 ADR-025 §4 就是
> **effective input**，必须进 canonical manifest。
>
> **本 ADR 因此把 Q2 的最终答案定为 YES，并 supersede PR #164 里的 NO。**
> Q1（`EgressTransportAssignment`）的 NO **不受影响**，理由见 §9。

## 2. 已核实的既有事实（全部对着当前 `main` 复核）

1. `DesiredForwarderState.proxies` 是 `tuple[Mapping[str, object], ...]`
   （`providers/base.py:241`），**无类型、无凭据字段**。同文件里
   `ForwarderListenerDTO`（:227）已经是 typed frozen DTO，说明 forwarder 侧
   **已有** typed DTO 的先例。
2. `DesiredForwarderState.__post_init__` 的 `freeze()` 只接受
   `str/int/float/bool/None` 与嵌套 Mapping/序列（:258-264），
   **opaque ref 字符串与整数 revision 天然可放**。
3. `mihomo_projection.py:411-421`：Mihomo 的最终 proxies 是
   `desired.proxies + materialized_proxies`，即 **repo-owned egress proxies**
   与 **transport materialization 产物**两类合并。两类来源不同，不得混为一谈。
4. **credential precedence 已由 ADR-019 §7 裁定并已实现**：
   active `EgressBinding.credential_secret_ref` 非空则用它，否则回落
   `EgressEndpoint.credential_secret_ref`（`provisioning_state.py:388-394`）。
5. **resolver 已经存在，不需要新建**：
   `infra/credential_resolver.py` 的 `SqlAlchemyCredentialResolver.resolve(secret_ref)
   -> CredentialDTO`（:72-101）做 current-read + 解密 + `{username,password}`
   校验，并以稳定错误码 fail closed。它是 Xray 当前在用的同一个 resolver。
6. `CredentialDTO`（`providers/base.py:75-84`）是 transient 明文 DTO，
   `__repr__` 已脱敏。
7. `Secret.revision` 是 `BigInteger NOT NULL default 1`（`models/ops.py:77`），
   与 ADR-035 给 controller secret 用的是同一个字段。
8. ADR-025 §4 要求 manifest 覆盖
   **“every effective input that can affect the repo-owned Mihomo projection”**，
   并明确排除 plaintext credentials。

## 3. 决定

### 3.1 provider-neutral 表示：新增 typed DTO，不塞进无类型 mapping

在 `backend/app/providers/base.py` 新增（与 `ForwarderListenerDTO` 并列）：

```python
@dataclass(frozen=True, slots=True)
class ForwarderEgressProxyDTO:
    name: str
    protocol: str
    host: str
    port: int
    credential_secret_ref: str = field(repr=False)
    credential_revision: int = 0
```

并在 `DesiredForwarderState` 新增：

```python
egress_proxies: tuple[ForwarderEgressProxyDTO, ...] = ()
```

**语义逐条固定：**

- `name` = `EgressEndpoint.code`，与 `ForwarderListenerDTO.proxy` 指向的名字
  **必须完全一致**；
- `protocol` / `host` / `port` 来自同一 `EgressEndpoint`；
- `credential_secret_ref` 是**按 ADR-019 §7 precedence 选出的 opaque handle**，
  **不是明文**；`repr=False` 与 ADR-019 的 `XrayOutboundDTO` 一致；
- `credential_revision` 是该 ref 对应 `Secret.revision` 的 **fresh current-read**
  取值，正整数；`bool` 不算整数（与 ADR-035 对 `api-secret-revision` 的口径同形）。

**为什么是 typed DTO 而不是往 `proxies` 的 mapping 里加两个键**：
① 与 `ForwarderListenerDTO` / ADR-019 `XrayOutboundDTO` 同形，不引入第三种风格；
② `repr=False` 能在类型层面固定脱敏，mapping 做不到；
③ **`proxies` 这个无类型通道要留给 transport materialization 产物**（那是 cache
内容，形状由订阅源决定，本仓库不拥有），两类来源必须在类型上可区分。

**`proxies` 字段保留，语义收窄为「transport materialization 产出的 proxies」。**
repo-owned 的住宅出口 proxy **一律走 `egress_proxies`**。不保留兼容期双写——
Mihomo registry 尚未放行，一次性迁移内部调用方与测试即可（与 ADR-019 §1 对
`outbound_tags` 的处理同理）。

### 3.2 credential precedence：**直接复用 ADR-019 §7，不新立规则**

```text
active EgressBinding.credential_secret_ref   （非空时）
  否则
active EgressEndpoint.credential_secret_ref
```

- 只考虑 `released_at IS NULL` 的 active binding；
- override 为 `NULL` / 空字符串 = 没有 override，允许回落 endpoint-level；
- **whitespace-only、指向不存在 Secret、或 revision 非法的非空 override 是
  malformed，必须 fail closed，禁止静默回落 endpoint ref**（否则会把客户接到
  另一份凭据上）；
- endpoint-level ref 为空或 malformed 同样 fail closed。

**这条与 Xray 共用同一份 precedence，是刻意的**：同一个出口在两条路径上必须
选出同一个 credential，否则会出现「Xray 认为用 A、Mihomo 实际用 B」的分裂。

### 3.3 进入 canonical manifest 的是什么

`build_projection_source_manifest()` 新增一个键：

```text
"egress_proxies": [
    {name, protocol, host, port, credential_secret_ref, credential_revision},
    ...
]   # 按 name 稳定排序
```

**只有 opaque ref 与整数 revision 进 manifest。明文永不进。**
这与 ADR-025 §4 对 controller secret 的既有处理
（“controller-secret opaque reference and durable `Secret.revision`”）**完全同形**。

### 3.4 为什么 A→B 必须产生新 generation

`desired_fingerprint` 的定义是「完整 repo-owned Mihomo projection 的身份」。
凭据换了，Mihomo 实际拨号所用的身份就换了，**渲染出的 document 内容随之改变**
（§3.6 的 `username`/`password` 会不同）。所以：

| 变化 | manifest | fingerprint | generation |
|---|---|---|---|
| `credential_secret_ref` A→B | 变 | 变 | **必须新开一代** |
| 同一 ref 的 `Secret.revision` 轮换 N→N+1 | 变 | 变 | **必须新开一代** |
| 只改 Secret 明文但 revision 不变 | 不变 | 不变 | 不新开 —— **所以轮换必须递增 revision**，这是既有契约，本 ADR 只是再点名一次 |

第三行是一个真实的失效面：**明文改了而 revision 没改，projection 不会重算**。
凭据轮换路径必须递增 `Secret.revision`，否则 Mihomo 会继续用旧凭据直到下一次
因别的原因触发新 generation。

### 3.5 plaintext 在哪里 resolve —— 与 ADR-019 §4 同一条流水线

```text
EgressBinding/EgressEndpoint.credential_secret_ref
  -> （loader：只取 ref + Secret.revision，不解密）
  -> DesiredForwarderState.egress_proxies   ← 仍然只有 opaque ref
  -> canonical manifest / generation         ← 仍然只有 opaque ref + revision
  -> 【forwarder render 边界】operation-scoped CredentialResolver
  -> transient CredentialDTO(username, password)
  -> MihomoProjectionWriter 的 render()
  -> transient CandidateConfig.content
  -> 受权限保护的 Mihomo runtime config 文件
```

**规则：**

- **明文第一次出现，只能在 render 边界的 resolver 返回 `CredentialDTO` 时。**
- **loader 不解密**——与 ADR-035 §6 给 controller secret 定的边界一致：
  loader 只读 `secret_ref` 与 `Secret.revision`。
- resolve 必须用 **current/locking read**（`_current_secret_statement()` 已提供），
  并且与 snapshot 在**同一个 Mihomo projection 命名锁 span 内**完成；
  若 resolve 时读到的 `Secret.revision` **与 snapshot 里的 `credential_revision`
  不一致**，说明凭据在 render 期间被轮换，**候选立即作废、fail closed、
  按 ADR-023 §2.2 重新 fresh-read 重试**，绝不用新明文配旧 fingerprint。
- operation 结束后不保留 resolver / `CredentialDTO` / candidate 的引用。

### 3.6 Mihomo document 怎么表达

每个 repo-owned 住宅出口渲染成一个 socks5 proxy：

```yaml
proxies:
  - name:     <EgressEndpoint.code>
    type:     socks5
    server:   <EgressEndpoint.host>
    port:     <EgressEndpoint.port>
    username: <resolved plaintext>
    password: <resolved plaintext>
listeners:
  - name:  listener-<code>
    type:  mixed
    listen: 127.0.0.1
    port:  <EgressEndpoint.mihomo_listen_port>
    proxy: <code>
```

`protocol` 为 `socks` 时同样渲染成 Mihomo 的 `socks5`（Mihomo 没有独立的
`socks` 类型）；**除 `{socks, socks5}` 外一律
`MIHOMO_EGRESS_PROTOCOL_UNSUPPORTED` fail closed**，与
`xray_composition.py:383` 的白名单保持一致。

### 3.7 fail-closed 清单（缺一不可）

| 情况 | 行为 |
|---|---|
| 选出的 ref 为空 / whitespace-only | `MIHOMO_EGRESS_CREDENTIAL_REF_INVALID` |
| 非空 override 指向不存在的 Secret | fail closed，**禁止回落 endpoint ref** |
| `Secret.revision` 非正整数或为 `bool` | `MIHOMO_EGRESS_CREDENTIAL_REVISION_INVALID` |
| resolve 时 revision 与 snapshot 不一致 | 候选作废，重新 fresh-read 重试 |
| 解密失败 / payload 不是 `{username,password}` / 任一为空 | 复用 resolver 既有的 `CREDENTIAL_MALFORMED` 系列错误，**整体**失败 |
| 任一 in-scope 出口解析失败 | **整份候选失败**，不得产出「少了某个出口」的部分配置（铁律 1 + ADR-019 §2 第 3 条同理） |
| `egress_proxies` 里的 `name` 与某个 listener 的 `proxy` 对不上 | `MIHOMO_LISTENER_PROXY_UNBOUND` |

### 3.8 与 transport materialization 的关系

**两者不得互相命名、不得互相覆盖。**

- `egress_proxies` 的 `name` 空间 = `EgressEndpoint.code`；
- materialized proxies 的 name 空间来自订阅 cache 内容；
- **合并后出现重名即 `MIHOMO_PROXY_NAME_COLLISION` fail closed**。
  当前实现只做 `desired.proxies + materialized_proxies` 拼接
  （`mihomo_projection.py:421`），**没有查重**——这是本 ADR 顺带关闭的一个真实缺口。

## 4. 明确不属于本 ADR 的内容

### 4.1 egress → transport 的链式出站（Q1）**不在本 ADR 内，维持 unsupported**

`EgressTransportAssignment` 在整个仓库里**没有 writer、reader、测试或 ADR**；
模型 docstring 写着 “Persistent placeholder for future Transport-side node
allocation”，迁移文件名是 `0018_transport_assignment_placeholders.py`。

**本 ADR 不为它定义任何 provider-neutral 表示，也不引入 `dialer-proxy`。**
理由：定义「egress 经由 transport node 出站」需要选择一种**全新的链式抽象**，
而当前没有任何产品要求或验收条款要求它——不像 §1 里 `ARCHITECTURE.md` §8 对
credential 的要求那样是硬性的。按 ADR-036 §2 与 ADR-023 §1.1：
**没有被要求、也无法完整表达的语义，归类为 unsupported，fail closed，
不得用命名技巧掩盖。**

> **要支持它，必须另写一条 ADR**（定义链式 proxy 抽象、assignment ↔ materialized
> proxy 的身份对应、以及 unmatched relation 的 fail-closed 规则）。
> **那条 ADR 不存在之前，ACTIVE assignment 一律 fail closed。**

### 4.2 本 ADR 不授权

不授权生产激活；不授权 `build_registry()` 放行 `FORWARDER_PROVIDER=mihomo`；
不授权部署；不授权创建或轮换任何真实 Secret；不改 Xray 侧任何既有行为；
不改 `SqlAlchemyCredentialResolver` 的 purpose-unbound 现状（那是 ADR-019
留下的既有属性，要改需单独评估）。

## 5. 被否决的替代方案

| 方案 | 否决理由 |
|---|---|
| 把 `username`/`password` **明文**放进 `DesiredForwarderState` | 直接违反 ADR-023（desired snapshot 可流转于 DB/契约/测试）与 ADR-025 §4（manifest secret-free）；明文会进 generation row 与 fingerprint 输入 |
| 把凭据当作 **deployment constant** | ADR-035 §2 的判据：逐部署不同 ≠ 逐**出口**不同。凭据是 per-egress 的 DB 数据，不是部署常量 |
| 往 `proxies` 的无类型 mapping 里加两个键 | 见 §3.1 三条理由；且会让 repo-owned 与 materialized 两类 proxy 在类型上不可区分 |
| 只放 `credential_secret_ref`、不放 `credential_revision` | 凭据轮换（ref 不变、明文变）将不产生新 generation，Mihomo 会静默继续用旧凭据 |
| 让 Mihomo 复用 Xray 已解析的明文 | 跨 provider 传递明文，且两条路径的 snapshot 边界不同，会产生第二个 desired-state 事实来源 |
| 继续让 Xray 独占住宅出口拨号、Mihomo 不拨 | 那样 `mihomo_listen_port`（`unique=True`，每出口一个）与 `ARCHITECTURE.md` §8 两条验收都失去意义；要走这条路必须先改 §8 的验收定义，属更大的架构变更 |

## 6. Testing requirements（B2-B2c 必须覆盖）

- `credential_secret_ref` A→B ⇒ manifest 变、fingerprint 变、新 generation；
- 同 ref 的 `Secret.revision` N→N+1 ⇒ 同上；
- binding override 非空时**不**回落 endpoint ref；override 为 `NULL`/空时**回落**；
- 非空但 malformed 的 override ⇒ fail closed，**不回落**；
- `DesiredForwarderState` / manifest / generation row 的 `repr()` 与序列化
  **均不含明文**（并入既有 `test_secret_leak.py` 的覆盖面）；
- render 期间 revision 漂移 ⇒ 候选作废而非用新明文配旧 fingerprint；
- repo-owned proxy name 与 materialized proxy name 重名 ⇒ fail closed；
- 非 `{socks, socks5}` 协议 ⇒ fail closed。

## 7. 重新评估条件

- `ARCHITECTURE.md` §8 的「每个 listener 出口 IP == 数据库记录 IP」被 User 改变
  或删除——那时 §1 的推理前提消失，本 ADR 须重评；
- 住宅出口改为不需认证，或改用非 socks5 协议；
- Mihomo 支持不含明文的 credential reference（届时可重评 §3.5 是否还需要
  明文进入 candidate，与 ADR-019 §4 末条同理）；
- 出现真实需求要求 egress → transport 链式出站——那是 §4.1 说的**另一条 ADR**，
  不是本 ADR 的重评。
