# ADR-014: Xray desired-state 数据所有权边界

- 状态: 已接受
- 日期: 2026-09-11
- 决策范围: TASK-T16 Phase 2A（只读盘点 + 契约决策，不实现代码）

## Context

TASK-T16 Phase 1（PR #54）确认了 `backend/app/providers/gateway/xray_file.py`
的 `XrayFileProvider.render()` 存在自我校验缺口（不产出 `inbounds`），并且
按 `AGENTS.md` 铁律第 1 条（"配置渲染一律从数据库全量生成，禁止增量拼接"）
排除了"读取当前运行时文件回填缺口"这个方向。在评估"数据库能不能作为
Xray 配置的唯一 source of truth"之前，必须先搞清楚一个更基础的问题：
**应用到底认为自己拥有 Xray 配置的哪些部分**。本节记录逐项阅读源码后确认
的事实，作为下面"决定"部分的依据；凡是代码无法给出确定答案的地方，标注
为 `UNVERIFIED / DECISION REQUIRED`，不猜测。

### 事实一：仓库里有三个名字相似但职责不同的"渲染器"，其中两个都生成
Xray 服务端配置，且互不联通

1. **`backend/app/providers/gateway/xray_file.py::XrayFileProvider.render()`**
   ——`GatewayProvider` Protocol（ADR-009）的实现，输入
   `DesiredRoutingState(user_routes, outbound_tags)`，只产出
   `{"routing": {...}, "outbounds": [...]}`。`outbounds` 里每一项只有
   `{"tag": tag, "protocol": "blackhole"|"socks"}`——**没有
   `settings.servers`，没有 host/port/credential，也没有 `inbounds`**。
   `registry.py` 从未 import 这个类（TASK-T16 Phase 1 已确认）。

2. **`ops/gateway/render_xray_routes.py::render_config()`**——一个独立脚本
   （不经过 `GatewayProvider` Protocol，`main()` 守卫，推测由运维直接调用
   或走 cron），从 `infrastructure/marzban/xray_config.base.json`（**静态
   模板文件**，磁盘路径，非数据库）读取完整的 `inbounds`/Reality 骨架，
   深拷贝后**只覆盖 `outbounds` 和 `routing`**：`outbounds` 里每一项都有
   完整的 `settings.servers[0] = {address, port, users:[credential]}`，
   `routing.rules` 用 `route.gateway_principal` 作为 Xray 的
   `"user"` 匹配字段。数据来源是
   `GatewayRouteBinding` JOIN `EgressEndpoint`，`credential` 来自
   `EgressBinding.credential_secret_ref`（有则用，否则退回
   `EgressEndpoint.credential_secret_ref`），通过 `reveal_secret()` 解密。
   **这个脚本从未生成或修改 `inbounds`**——它继承的是静态模板里的
   `inbounds`，而模板里 `settings.clients` 写死是 `[]`（见下方事实二）。

3. **`backend/app/domain/subscription_render.py::render_routing_rules()`**
   ——**不是服务端 Xray 配置渲染器**，是生成**客户订阅文件**（Clash/Base64，
   客户自己设备用的分流规则）的函数，只是恰好也遵守"private BLOCK 开头、
   tcp/udp BLOCK 收尾、无 DIRECT"这条安全不变量（铁律第 2 条），因此和
   前两者外观相似。`backend/tests/guards/test_routing_invariants.py`
   测的是这一个，不是 Xray 网关渲染。三者不应该被混为一谈。

结论：`XrayFileProvider.render()`（ADR-009 的 provider 抽象路径）在功能上
明显落后于 `ops/gateway/render_xray_routes.py`（未经抽象层的独立脚本）——
后者才是真正贴近"数据库全量生成 outbounds+routing"这条铁律的实现，前者
甚至还没有 outbound 的连接细节。TASK-T16 后续任何"接入 XrayFileProvider"
的工作，事实上需要先把 `render_xray_routes.py` 里已经验证过的数据流程
（`GatewayRouteBinding` JOIN `EgressEndpoint`/`EgressBinding` → 完整
outbound）迁移或复用到 provider 抽象层，而不是从零设计——这是一个此前
没有被记录的重要事实，本 ADR 记录下来供 Phase 2B 参考。

### 事实二：`inbounds[].settings.clients` 目前在整个代码库里永远是空的，
没有任何一处代码写入它

`infrastructure/marzban/xray_config.base.json` 的 `inbounds[0].settings`
是 `{"clients": [], "decryption": "none"}`——`clients` 写死为空数组。
`render_xray_routes.py::render_config()` 对 `base_config` 做深拷贝后只
覆盖 `outbounds`/`routing`，从不touch `inbounds`，因此这个空数组会原样
进入每一次渲染出的运行时配置。全仓库搜索 `"clients"`
（`backend/app/**`、`ops/**`）**没有任何匹配**——没有任何代码路径会往
Xray 的 inbound client 列表里添加一个客户端。

同时，`AccountingProvider.get_connection_links(username)`
（`backend/app/providers/accounting/mock.py` 是唯一实现，返回
`vless://{username}@...`）是全仓库唯一"生成客户可用的 Xray 连接凭据"的
地方——但它的返回值只是拼出来给客户看的一个 URI 字符串，不写回任何
`inbounds` 结构，也不在 `GatewayRouteBinding` 或任何其他表中留下 UUID/
密码这类 Xray 认证材料。

**这是 `UNVERIFIED / DECISION REQUIRED`**：真实（非 mock）
`AccountingProvider`（配置里唯一承认的取值是 `marzban`，参见
`Settings.accounting_provider`/`validate_runtime_safety()`）在生产环境
下，客户端的 inbound 认证材料到底是：

（a）由 Marzban 自己管理一个**独立的、这个仓库代码完全看不到的 Xray 实例**
（Marzban 作为完整的 Xray 面板产品，通常会这样做），此时本仓库的
`XrayFileProvider`/`render_xray_routes.py` 管理的其实是**另一个专门做
出口路由的 Xray 实例**，天然不需要自己的 inbound 客户端列表；

还是（b）两者理应共享同一个运行时 config.json，客户端的加入需要某种
目前尚未实现的机制写入同一份 `inbounds`，而这个机制现在完全缺失。

代码本身给不出确定答案：`ADR-013` 说"Marzban 在新架构中的定位是
transport 层"，但 `Settings.validate_runtime_safety()` 把 marzban 校验
挂在 `accounting_provider == "marzban"` 分支下，`transport_provider_mode`
是完全独立的另一个配置项——**"Marzban 到底对应 accounting 还是
transport"这件事，ADR-013 的文字表述和 `config.py` 的实际校验分支互相
矛盾，本 ADR 不替它们下结论，只如实记录矛盾存在**，留给专门解决这个
矛盾的后续 ADR/TASK。

这个未决问题直接决定"数据库能不能作为 inbound/client 部分的唯一
source of truth"——如果答案是(a)，数据库这部分内容**根本不需要表达**
（不在本应用职责范围内）；如果是(b)，数据库目前**完全没有**能力表达它
（见下方"结论 1"）。

### 事实三：`GatewayRouteBinding.gateway_principal` 不是 Xray client 身份，
是 Webshare 出口租户 ID；真正的 Xray 路由匹配键没有持久化

逐行核实 `backend/app/infra/provisioning_state.py`：

```python
def ensure_gateway_route_binding(self, gateway_principal: str, endpoint): ...
def desired_routing_state(self, request, endpoint, tenant):
    binding = self.ensure_gateway_route_binding(tenant.tenant_id, endpoint)
    return DesiredRoutingState(
        user_routes={request.username: binding.outbound_tag},
        outbound_tags=(binding.outbound_tag, "BLOCK"),
    )
```

`ensure_gateway_route_binding()` 的调用方传入的是 `tenant.tenant_id`
——`tenant` 是 `EgressProvider.create_tenant()` 返回的 `TenantDTO`（Webshare
出口子账户 ID），不是账务/Xray 客户端身份。也就是说
`GatewayRouteBinding.gateway_principal` 记录的其实是"这条出口路由对应
哪个 Webshare 子账户"，是审计用途，**从未被读回任何渲染出的 Xray 配置
字段**（`render_xray_routes.py` 虽然把 `route.gateway_principal` 用作
`"user"` 匹配值，但这是巧合式复用同一个字段，不代表这个字段的设计初衷
就是 Xray 身份）。

真正参与 Xray 路由 `"user"` 匹配的值是 `ProvisionRequest.username`
（`backend/app/domain/provisioning.py:49`）——但 `GatewayRouteBinding`
表**没有 `username` 列**（见 `infrastructure/alembic/versions/
0019_gateway_route_bindings.py`，逐列核对，没有），`Subscription` 模型
也没有。**这是 `UNVERIFIED / DECISION REQUIRED`**：一个已存在的订阅，
如果需要在未来某次事件（例如 Phase 2B 要求的"从数据库完整期望态一次性
生成"）重新渲染路由规则，代码目前没有已验证的方式能查回当初开通时使用
的 `username` 值——除非这个值本来就等于某个已经持久化的字段（例如
`Customer`/`Subscription` 的某个可推导值），但这需要专门核实，本 ADR
不假设。

### 事实四：`TrafficRule` 表存在、有迁移、无任何代码读取

`backend/app/models/gateway.py::TrafficRule` 有完整字段
（`rule_set`/`match_type`/`match_value`/`target_egress`/`priority`/
`enabled`）和迁移（`0012_dynamic_traffic_policy.py`），但全仓库
`backend/app/**`、`ops/**` 搜索 `TrafficRule` **只有模型定义本身命中**
——没有任何 domain/provider/renderer 代码读取或写入它。它和
`DesiredRoutingState.user_routes` **没有任何关系**：两者是完全独立、
互不感知的两套机制，不是"细粒度 vs 粗粒度"的层次关系。

### 事实五：Reality 私钥在每次渲染时都可能被重新生成（一个真实的、
静态阅读即可确认的缺陷，不是 UNVERIFIED）

`render_xray_routes.py::_reality_settings()` 在 `base_config`（每次都是
**从磁盘重新读取的静态模板**）里找 Reality inbound，如果
`realitySettings.privateKey` 为空就调用 `xray x25519` 生成一个新的。
`render_from_database()` 从未把这个生成的 key 写回 `BASE_CONFIG`
（它只把渲染结果写到 `OUTPUT_CONFIG`，即运行时文件）。也就是说：只要
运维没有在部署时手动把生成好的私钥永久写进
`infrastructure/marzban/xray_config.base.json`，**每一次调用
`render_from_database()` 都会生成一把新的 Reality 私钥**，导致所有已经
拿到旧公钥的客户端连接失效。这不是"运行时文件当第二数据源"那种铁律
冲突，而是相反的问题：**某些字段（Reality 私钥、未来可能的每客户端
UUID）本质上需要跨多次渲染保持稳定，而"从静态模板文件全量拷贝"这个
实现本身就是无状态的，两者天然冲突**。这一点是本 ADR 决定"哪些内容必须
进数据库/Secret 表、不能只放模板文件"的直接证据。

## Decision

### A. 分层所有权（基于以上事实，能确定的部分）

| 层 | 内容 | 归属 | 依据 |
|---|---|---|---|
| 应用/数据库拥有 | 路由规则（用户 → outbound 映射） | `GatewayRouteBinding`（需要新增字段，见下） | `render_xray_routes.py` 已经这样做 |
| 应用/数据库拥有 | outbound 定义（host/port/protocol/凭据引用） | `EgressEndpoint` + `EgressBinding`/`Secret` | 同上，已验证的数据流 |
| 应用拥有，但**不来自数据库行数据**（固定不变量，允许硬编码） | private BLOCK 首条规则、tcp/udp BLOCK 兜底、禁止 DIRECT | 渲染器代码本身 | `AGENTS.md` 铁律第 2 条要求"一律"，属于应用级不变量而非按订阅变化的期望态 |
| 运维/部署时静态模板拥有 | inbound 的 protocol/listen/port/TLS-Reality 参数骨架 | `infrastructure/marzban/xray_config.base.json` | 事实一、二 |
| **尚未确定，需要专门 ADR/TASK 决策，不在本 ADR 范围内解决** | inbound client（UUID/密码/email）的创建、生命周期、持久化位置；Reality 私钥的持久化位置；`ProvisionRequest.username` 是否需要新增持久化字段；Marzban 到底对应 `accounting_provider` 还是 `transport_provider_mode`（ADR-013 与 `config.py` 现有矛盾） | UNVERIFIED / DECISION REQUIRED | 事实二、三、五 |
| 无归属，建议废弃或明确立项 | `TrafficRule` 表 | 现状是死代码；本 ADR 不删除它（改动 `models/**` 超出 Phase 2A 允许范围），但记录为"没有被任何渲染路径使用，不应被假设为路由数据源" | 事实四 |
| 运行时文件（Xray 当前 `config.json`）角色 | 只读——backup / rollback / drift detection / validation / post-reload 校验 | 明确禁止作为 desired state 的第二数据源（铁律第 1 条） | 已在 TASK-T16 前几轮记录 |

### B. 数据流（DB → desired state → renderer → candidate）

对已确定归属的部分（路由规则 + outbound 定义），数据流必须是：

```
GatewayRouteBinding (routing) ┐
EgressEndpoint (outbound 基础信息)  ├─→ 期望态 DTO（需要扩展，见下）─→ renderer ─→ candidate
EgressBinding/Secret (凭据引用)     ┘
```

不允许在这条链路的任何一环读取当前运行时 `config.json` 来补齐缺失字段。
静态模板文件（inbound 骨架）可以作为 renderer 的一个**输入**（不是数据库，
但也不是"当前运行时状态"，是部署时配置），只要 renderer 不依赖它保存
任何"跨渲染必须保持一致"的状态（这正是事实五暴露的问题——如果 Reality
私钥必须跨渲染稳定，它就不能只活在这个无状态模板里，需要挪进数据库或
Secret 表）。

### C. 合法删除语义

preservation 校验的目标改写为：**最终生效配置与数据库期望态一致，同时
保持安全不变量**，不是"运行时当前有什么就不能删什么"。具体：

- 如果数据库里一条 `GatewayRouteBinding` 被合法释放（`released_at` 非空
  或 `enabled=False`），下一次渲染的 candidate 就不应该再包含它对应的
  路由规则和 outbound——这是合法删除，不应该被任何 preservation 检查
  拦下。
- 校验应该验证"candidate 与本次从数据库计算出的期望态一致"+"安全不变量
  成立"，而不是"candidate 是不是运行时当前配置的超集"。
- inbound 部分（client 列表等）在事实二的问题解决之前，**不属于本
  renderer 的职责范围**，因此也不应该由这条渲染链路的 preservation 校验
  去检查它是否被"删除"——这正是 `XrayFileProvider._preservation_errors()`
  当前的错误所在：它把 inbound client 差异当成本 provider 的职责来校验，
  但如果最终决定 inbound 属于事实二里的方案(a)（Marzban 独立管理），这个
  校验本身的存在前提就不成立。

### D. Rollback 语义

不变：backup → candidate 生成 → 静态/动态校验（`xray run -test` + 安全
不变量 + 与期望态一致性）→ reload → health/post-check → 任一失败 restore
backup + reload + 复验，这条九步链路（`AGENTS.md` 铁律第 6 条）不因本 ADR
改变；本 ADR 只改变"candidate 该怎么生成、preservation 该比对什么"，不
改变失败后怎么回滚。

### E. 为什么禁止 runtime-config merge（补充证据）

除了此前几轮已经确认的"违反铁律第 1 条"之外，事实五提供了一个更具体的
反例：**即使允许读运行时文件，也解决不了"哪些字段需要跨渲染保持稳定"
这个问题**——运行时文件本身在这个代码库的实现里也是"每次全量重写"的
产物，不是一个可靠的"上一次状态"来源（`render_xray_routes.py` 直接
`temporary.replace(OUTPUT_CONFIG)` 整个替换）。真正需要跨渲染稳定的字段
（Reality 私钥、未来可能的 client UUID）必须有一个明确的、持久化的
source of truth（数据库或 Secret 表），而不是寄希望于"读一下当前文件"
——当前文件本身都靠不住。

### F. 对 `DesiredRoutingState` 的影响

`backend/app/providers/base.py::DesiredRoutingState` 目前：

```python
@dataclass(frozen=True, slots=True)
class DesiredRoutingState:
    user_routes: Mapping[str, str] = field(default_factory=dict)
    outbound_tags: tuple[str, ...] = ()
```

只能表达"用户 → outbound tag 名字"和"有哪些 tag"，**不能表达**
`render_xray_routes.py` 已经证明是必需的 outbound 连接细节
（host/port/protocol/凭据），也不能表达 inbound/client（事实二的问题
解决之前，这也许本来就不该由它表达）。这个 DTO 目前的抽象层级低于
`ops/gateway/render_xray_routes.py` 里已经跑通的数据形状，本 ADR 认定
它对"生成完整、可用的 Xray outbound"这个目标是**不足的**（见"结论 2"）。

按 `AGENTS.md` 铁律第 5 条，修改 `backend/app/providers/base.py` 前必须
先有 ADR——本 ADR 记录这个不足，但**不在这里改这个文件**（Phase 2A 明确
禁止修改代码），具体怎么扩展留给 Phase 2B，Phase 2B 修改 `base.py` 时
可以引用本 ADR 作为其"先有 ADR"的依据，但仍需要在 Phase 2B 自己的 PR 里
写清楚最终选择的具体字段形状（本 ADR 不预先决定，因为这取决于事实二/
事实三两个 UNVERIFIED 问题先有答案）。

### G. 是否需要新增/修改 DB schema

需要，但具体列留给 Phase 2B/后续 ADR 决定，本 ADR 只记录必要性和已知
候选：

- `GatewayRouteBinding` 缺少能重新推导 Xray `"user"` 路由匹配键的持久化
  字段（事实三）——需要新增列，或证明可以从别处推导（需要专门核实）。
- Reality 私钥需要一个持久化位置（数据库列或 Secret 表条目），不能只靠
  静态模板文件（事实五）。
- 如果事实二最终判定为方案(b)（inbound client 由本应用管理），需要一张
  新表或对 `GatewayRouteBinding` 的扩展来持久化 client UUID/密码等认证
  材料，并通过 `Secret` 机制加密存储。
- 如果判定为方案(a)（Marzban 独立管理），则不需要新增任何 inbound
  相关 schema，但需要在文档里明确写清楚"本仓库管理的 Xray 网关和
  Marzban 管理的 Xray（如果存在）是两个独立实例"这件事，目前没有任何
  现存文档这样明确说过。

## 约束

1. 本 ADR 不实现任何代码、schema、迁移改动——纯粹是所有权边界和数据流
   的决策记录。
2. 事实二（inbound/client 归属）、事实三（路由匹配键持久化）、Marzban
   对应 `accounting` 还是 `transport` 的矛盾，三者均标记
   `UNVERIFIED / DECISION REQUIRED`，本 ADR 不替它们下结论；Phase 2B
   开始前必须先有针对这三点的明确决定（可以是本 ADR 的后续修订，也可以
   是新的 ADR）。
3. `ops/gateway/render_xray_routes.py` 现有的、已经在生产路径上跑的数据
   流（`GatewayRouteBinding` JOIN `EgressEndpoint`/`EgressBinding` →
   完整 outbound）在 Phase 2B 设计新的期望态 DTO 时应当作为参考实现，
   不应该无视它重新发明一遍。

## 考虑过的替代方案

1. **继续假设"数据库已经能完整表达 Xray 配置"，直接在 `render()` 里
   拼一个看起来完整的 candidate**：会掩盖事实二/事实三两个真实存在的
   数据缺口，产出一个在真实环境下无法正确认证客户端连接的配置，否决。
2. **把运行时 `config.json` 当作 inbound/client 部分的权威来源，读取后
   原样保留**：直接违反 `AGENTS.md` 铁律第 1 条，且事实五证明运行时文件
   本身也不可靠（每次可能被全量替换），否决。
3. **现在就决定 Marzban 管理独立 Xray 实例（方案 a）**：没有仓库内的
   代码或文档证据支持这个结论，武断决定可能与运维实际部署方式不符，
   否决——保留为 UNVERIFIED，留给能够核实真实部署拓扑的人决定。

## 安全影响

- 事实五描述的 Reality 私钥轮换问题如果在生产环境发生，会导致存量客户
  连接失效（客户端固定了旧公钥）——这是一个高优先级的、独立于 Phase 2A
  本身的运维风险，建议记录为单独的 Issue/TASK 尽快核实生产环境
  `xray_config.base.json` 是否已经手动固定了私钥。
- 事实三如果最终确认无法恢复 `username`，意味着任何需要"重新渲染某个
  已存在订阅的路由规则"的操作（例如 Phase 2B 要求的"从数据库完整期望态
  一次性生成"）在设计上必须先解决这个持久化缺口，否则重新渲染会产生
  和原始开通时不一致的路由匹配键，导致客户连接静默失效。

## 重新评估条件

当事实二、事实三、"Marzban 对应哪个 provider 分类"这三个 UNVERIFIED
问题任一得到确认的答案时，重新评估并更新本 ADR 或提交后续 ADR，明确
最终的 schema 变更范围。
