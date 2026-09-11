# ADR-014: Xray desired-state 数据所有权边界

- 状态: 已接受
- 日期: 2026-09-11（2026-09-11 第二次修订：修正 route-match 用户名持久化、
  `TrafficRule` 用途、Reality shortIds、部署拓扑四处独立审查指出的事实
  错误）
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
Xray 服务端配置，且互不联通；`render_xray_routes.py` 已经是当前部署流程
里明确被调用的一步，不是"推测由运维调用的独立脚本"

1. **`backend/app/providers/gateway/xray_file.py::XrayFileProvider.render()`**
   ——`GatewayProvider` Protocol（ADR-009）的实现，输入
   `DesiredRoutingState(user_routes, outbound_tags)`，只产出
   `{"routing": {...}, "outbounds": [...]}`。`outbounds` 里每一项只有
   `{"tag": tag, "protocol": "blackhole"|"socks"}`——**没有
   `settings.servers`，没有 host/port/credential，也没有 `inbounds`**。
   `registry.py` 从未 import 这个类（TASK-T16 Phase 1 已确认）。

2. **`ops/gateway/render_xray_routes.py::render_config()`**——一个独立于
   `GatewayProvider` Protocol 的脚本，但**不是"推测由运维直接调用或走
   cron"**（这是第一版的错误猜测，独立审查指出后核实确认）：
   `deploy/lib/40_stack_up.sh` 里明确写着——

   ```bash
   log 'rendering Xray runtime config from the migrated database before Marzban'
   compose build backend-api
   compose run --rm --no-deps backend-api \
     python -m ops.gateway.render_xray_routes
   validate_runtime_files
   compose up --detach
   ```

   即：部署脚本在 `compose up` 之前，显式地、一次性地跑这个模块，把结果
   写到 `MARZBAN_DATA_DIR/xray_config.json`；随后
   `infrastructure/compose/compose.transport.yml` 把这同一个宿主机文件
   挂载进 `marzban` 容器的 `/code/xray_config.json`。这是仓库内**唯一**
   声明的服务端 Xray 部署路径，不是一个游离于部署流程之外的旁路脚本
   （详见事实四）。

   该脚本从 `infrastructure/marzban/xray_config.base.json`（**静态模板
   文件**，磁盘路径，非数据库）读取完整的 `inbounds`/Reality 骨架，深
   拷贝后**只覆盖 `outbounds` 和 `routing`**：`outbounds` 里每一项都有
   完整的 `settings.servers[0] = {address, port, users:[credential]}`，
   `routing.rules` 用 `route.gateway_principal` 作为 Xray 的
   `"user"` 匹配字段。数据来源是 `GatewayRouteBinding` JOIN
   `EgressEndpoint`，`credential` 来自 `EgressBinding.credential_secret_ref`
   （有则用，否则退回 `EgressEndpoint.credential_secret_ref`），通过
   `reveal_secret()` 解密。**这个脚本从未生成或修改 `inbounds`**——它
   继承的是静态模板里的 `inbounds`，而模板里 `settings.clients` 写死是
   `[]`（见下方事实二）。

3. **`backend/app/domain/subscription_render.py::render_routing_rules()`**
   ——**不是服务端 Xray 配置渲染器**，是生成**客户订阅文件**（Clash/Base64，
   客户自己设备用的分流规则）的函数，只是恰好也遵守"private BLOCK 开头、
   tcp/udp BLOCK 收尾、无 DIRECT"这条安全不变量（铁律第 2 条），因此和
   前两者外观相似。`backend/tests/guards/test_routing_invariants.py`
   测的是这一个，不是 Xray 网关渲染。三者不应该被混为一谈。

结论：`XrayFileProvider.render()`（ADR-009 的 provider 抽象路径）在功能上
明显落后于 `ops/gateway/render_xray_routes.py`（已经是部署流程里实际调用
的实现）——后者才是真正贴近"数据库全量生成 outbounds+routing"这条铁律
的实现，前者甚至还没有 outbound 的连接细节。TASK-T16 后续任何"接入
XrayFileProvider"的工作，事实上需要先把 `render_xray_routes.py` 里已经
验证过的数据流程（`GatewayRouteBinding` JOIN `EgressEndpoint`/
`EgressBinding` → 完整 outbound）迁移或复用到 provider 抽象层，而不是
从零设计——这是一个此前没有被记录的重要事实，本 ADR 记录下来供 Phase 2B
参考。

### 事实二：`inbounds[].settings.clients` 目前没有任何生产 renderer/
provider 路径生成或写入它；但部署拓扑证明 Marzban 用的就是本仓库渲染的
同一份 `xray_config.json`，不是一个完全独立、看不见的 Xray 实例

**用词更正（独立审查指出后核实确认）**：第一版这里写"全仓库搜索
`"clients"` 在 `backend/app/**`/`ops/**` 零匹配"，这个说法不准确——
`backend/app/providers/gateway/xray_file.py` 里确实有 `_inbound_clients()`
函数（读取 `inbounds[].settings.clients` 做 preservation 校验用）。准确
的说法应该是：**没有发现任何生产 renderer/provider 路径生成或写入
`inbounds[].settings.clients`**——`_inbound_clients()` 只是读取/校验，
不是写入/生成；`infrastructure/marzban/xray_config.base.json` 的
`inbounds[0].settings` 是 `{"clients": [], "decryption": "none"}`，
`render_xray_routes.py::render_config()` 对 `base_config` 深拷贝后只覆盖
`outbounds`/`routing`，从不 touch `inbounds`，因此这个空数组会原样进入
每一次渲染出的运行时配置。

同时，`AccountingProvider.get_connection_links(username)`
（`backend/app/providers/accounting/mock.py` 是唯一实现，返回
`vless://{username}@...`）是全仓库唯一"生成客户可用的 Xray 连接凭据"的
地方——但它的返回值只是拼出来给客户看的一个 URI 字符串，不写回任何
`inbounds` 结构，也不在 `GatewayRouteBinding` 或任何其他表中留下 UUID/
密码这类 Xray 认证材料。

**部署拓扑证据已经缩小了这里的 UNVERIFIED 范围（独立审查指出后核实
确认）**：第一版把"Marzban 独立管理一个仓库完全看不到的 Xray 实例"和
"两者共享同一个 config.json"列为同等权重的两个候选——**这个对等关系
不成立**。`infrastructure/compose/compose.transport.yml` 里
`marzban` 服务的 volumes 明确挂载：

```yaml
volumes:
  - ../../data/marzban/xray_config.json:/code/xray_config.json
```

而这个宿主机路径正是 `render_xray_routes.py::OUTPUT_CONFIG` 的默认写入
目标（`XRAY_RUNTIME_CONFIG_PATH` 环境变量覆盖，默认
`/app/data/marzban/xray_config.json`，`40_stack_up.sh` 用的
`MARZBAN_DATA_DIR` 也指向同一个宿主机目录）。也就是说：**当前 Compose
拓扑里只有一份 `xray_config.json`，本仓库的渲染脚本生成它，Marzban 容器
直接挂载读取它，不存在"repo 管理一个 Xray、Marzban 另外管理一个看不见的
Xray"这种拓扑**。

真正仍然回答不了的问题收窄为：**在这套"repo 渲染文件 → Marzban 容器
挂载读取"的拓扑下，Marzban 进程启动之后，是否、以及通过什么机制会动态
地把它自己面板里新增的用户写回这同一份 `inbounds[].settings.clients`**
——`marzban` 服务自己还挂载了 `../../data/marzban/db.sqlite3`（Marzban
自己的 SQLite 数据库，与本应用的 MySQL 完全独立），这暗示 Marzban 可能
有自己的一套用户管理状态和写回机制，但**这属于 Marzban 自身的运行时行为，
本仓库的代码看不到、验证不了**，继续标记为
`UNVERIFIED / DECISION REQUIRED`，不猜测 Marzban 内部实现细节。

代码本身也给不出"Marzban 到底对应 accounting 还是 transport"这个问题的
确定答案：`ADR-013` 说"Marzban 在新架构中的定位是 transport 层"，但
`Settings.validate_runtime_safety()` 把 marzban 校验挂在
`accounting_provider == "marzban"` 分支下，`transport_provider_mode`
是完全独立的另一个配置项——**这件事 ADR-013 的文字表述和 `config.py`
的实际校验分支互相矛盾，本 ADR 不替它们下结论，只如实记录矛盾存在**，
留给专门解决这个矛盾的后续 ADR/TASK。

这个未决问题直接决定"数据库能不能作为 inbound/client 部分的唯一
source of truth"——如果 Marzban 自己动态管理 client 写入（上面收窄后的
候选），数据库这部分内容**可能根本不需要表达**（不在本应用职责范围内，
但仍需要人工确认，而不是假设）；如果确认这个机制缺失，数据库目前
**完全没有**能力表达它（见下方"结论 1"）。

### 事实三（已更正）：Xray 路由匹配用的 accounting username 已经有明确
的持久化 canonical source——`Subscription.accounting_user_id`，
`GatewayRouteBinding` 不需要新增字段

**第一版这里的结论是错误的，独立审查指出后核实确认，完整改正如下。**
第一版声称"`ProvisionRequest.username` 没有持久化在任何表里"、"数据库
没有办法恢复原 username"、"`GatewayRouteBinding` 需要新增字段"——重新
核实 `backend/app/api/admin.py::admin_confirm_payment` 和
`backend/app/models/subscription.py` 之后，这些说法都不成立：

```python
# backend/app/api/admin.py::admin_confirm_payment
request = ProvisionRequest(
    order_id=str(order.id),
    customer_id=str(order.customer_id),
    username=f"sub-{order.id}",
    ...
)
...
subscription = db.scalar(select(Subscription).where(Subscription.order_id == order.id))
...
if subscription.accounting_user_id is None:
    subscription.accounting_user_id = request.username
    db.commit()
```

```python
# backend/app/models/subscription.py
accounting_user_id: Mapped[str | None] = mapped_column(String(128), unique=True)
```

核实结果：

1. **当前生产 purchase provisioning 下，Xray/accounting username 的
   canonical persisted source 已经是 `Subscription.accounting_user_id`**
   ——`admin_confirm_payment` 在开通成功后立刻把 `request.username` 写入
   这一列并 commit，这一列本身 `unique`，后续任何需要这个 username 的
   代码（例如 `admin_sync_usage` 读 `subscription.accounting_user_id`）
   都从这里读，不是从 `GatewayRouteBinding` 或别处。
2. **`sub-{order.id}` 本身就是一个确定性的 fallback/derivation**——
   `order_id` 是 `Subscription` 的持久化外键，即使不读
   `accounting_user_id` 这一列，也能用 `f"sub-{subscription.order_id}"`
   确定性地重新推导出同一个值（只要这个命名规则不变）。
3. **`GatewayRouteBinding` 不需要新增 username 字段**——它已经通过
   `subscription_id` 外键关联到 `Subscription`，而 `Subscription` 已经
   持久化了 `accounting_user_id`；重新渲染一条已存在订阅的路由规则时，
   通过 `GatewayRouteBinding.subscription_id → Subscription.
   accounting_user_id` 这条已经存在的路径就能拿到 Xray `"user"` 匹配
   所需要的值，不需要额外 schema。

`GatewayRouteBinding.gateway_principal` 这个字段本身的定位不变：核实
`ensure_gateway_route_binding()`/`desired_routing_state()`，它被赋值为
`tenant.tenant_id`（`EgressProvider.create_tenant()` 返回的 Webshare
出口子账户 ID），不是 Xray 客户端身份，是审计用途——这一点第一版的结论
是对的，本轮不改；改的只是"这个字段之外，是否还缺一个能恢复 username
的持久化位置"这个判断，答案是**不缺，已经有了**。

（如果未来出于"想让路由匹配键和 accounting username 解耦"这类设计考虑，
要给 `GatewayRouteBinding` 或路由匹配值单独设计一个字段，那是 Phase 2B
自己可以做的**设计选择**，不是本 ADR 描述的"当前数据缺失"这一既成事实
——本 ADR 不预先替 Phase 2B 做这个选择。）

### 事实四（已更正）：`TrafficRule` 不是死代码——它是 Mihomo/forwarder
渲染器的真实动态流量策略数据源，只是和 Xray 的 `DesiredRoutingState.
user_routes` 无关

**第一版这里的结论是错误的，独立审查指出后核实确认，完整改正如下。**
第一版只搜索了 `backend/app/**` 和当时读过的 `ops/gateway/**`，没有读
`ops/forwarder/render_mihomo_config.py`，因此错误地得出"零代码引用/
dead code"的结论。重新核实：

```python
# ops/forwarder/render_mihomo_config.py
from backend.app.models import (..., TrafficRule, ...)

def load_transport_topology(db):
    ...
    traffic_rules = [
        TrafficRuleConfig(item.id, item.rule_set, item.match_type,
                           item.match_value, item.target_egress, item.priority)
        for item in db.scalars(
            select(TrafficRule).where(TrafficRule.enabled.is_(True))
            .order_by(TrafficRule.priority, TrafficRule.rule_set, TrafficRule.id)
        )
    ]
    ...

def _render_traffic_rules(traffic_rules, default_transport_route, residential_target):
    if not traffic_rules:
        raise ValueError("At least one enabled traffic rule is required")
    ...  # 生成 Mihomo 的 "rules" 列表（MATCH / 其它匹配类型 → target）

def build_config(providers, routes, runtime, traffic_rules, egress_sources=None):
    ...
    rules = _render_traffic_rules(traffic_rules, default_route, egress_sources[0].mihomo_name)
    return {..., "rules": rules}
```

`load_transport_topology()` 查询 enabled 的 `TrafficRule`、
`_render_traffic_rules()` 把它们渲染成 Mihomo 配置的 `rules` 字段、
`build_config()` 把结果放进最终配置——这是一条完整、真实在用的数据流。

正确的结论需要区分两件可以同时成立的事：

- `TrafficRule` **是** Mihomo/forwarder 渲染器（`render_mihomo_config.py`）
  的真实动态流量策略数据源（AIRPORT/RESIDENTIAL 分流规则），不是死代码，
  不应该被假设为"没有归属"或"建议废弃"。
- `TrafficRule` **不是** 当前 Xray `DesiredRoutingState.user_routes`
  的数据源——两者服务于两个不同的渲染目标（Xray 出口路由 vs. Mihomo
  转发规则），本 ADR 关注的是 Xray desired state，`TrafficRule` 对这个
  范围而言确实无关，但"无关"不等于"无归属"或"dead"。

### 事实五（已扩充）：Reality 的 `privateKey` **和** `shortIds` 都在每次
渲染时可能被重新生成，两者都需要评估持久化

**第一版这里只记录了 `privateKey`，独立审查指出后核实确认还漏了
`shortIds`，一并改正。** `render_xray_routes.py::_reality_settings()`
在 `base_config`（每次都是**从磁盘重新读取的静态模板**）里找 Reality
inbound：

```python
if not reality.get("privateKey"):
    reality["privateKey"] = _x25519_private_key(
        os.getenv("XRAY_BINARY", "/usr/local/bin/xray")
    )
reality["shortIds"] = reality.get("shortIds") or [secrets.token_hex(8)]
```

`infrastructure/marzban/xray_config.base.json` 里这两个字段都是空
（`"privateKey": ""`、`"shortIds": []`）。`render_from_database()` 从未
把生成后的值写回 `BASE_CONFIG`（只把渲染结果写到 `OUTPUT_CONFIG`，即
运行时文件）。也就是说：只要运维没有在部署时手动把生成好的
`privateKey`/`shortIds` 永久写进
`infrastructure/marzban/xray_config.base.json`，**每一次调用
`render_from_database()` 都会同时重新生成一把 Reality 私钥和一组新的
short ID**，两者都是 Reality 协议里客户端配置需要固定下来的服务端身份
材料，任一变化都可能导致已有客户端连接失效（具体失效方式取决于 Reality
协议对 shortId 校验的严格程度，这里不展开协议细节，只记录"两者都来自
同一个每次可能重新生成、从不持久化"的代码路径这一事实）。

这不是"运行时文件当第二数据源"那种铁律冲突，而是相反的问题：**某些
字段（Reality 私钥、shortIds、未来可能的每客户端 UUID）本质上需要跨
多次渲染保持稳定，而"从静态模板文件全量拷贝"这个实现本身就是无状态的，
两者天然冲突**。这一点是本 ADR 决定"哪些内容必须进数据库/Secret 表、
不能只放模板文件"的直接证据。

## Decision

### A. 分层所有权（基于以上事实，能确定的部分）

| 层 | 内容 | 归属 | 依据 |
|---|---|---|---|
| 应用/数据库拥有 | 路由规则（用户 → outbound 映射） | `GatewayRouteBinding`（不需要新增 username 字段，见事实三） | `render_xray_routes.py` 已经这样做 |
| 应用/数据库拥有 | outbound 定义（host/port/protocol/凭据引用） | `EgressEndpoint` + `EgressBinding`/`Secret` | 同上，已验证的数据流 |
| 应用/数据库拥有 | Xray `"user"` 路由匹配键 | `Subscription.accounting_user_id`（通过 `GatewayRouteBinding.subscription_id` 关联可得） | 事实三 |
| 应用拥有，但**不来自数据库行数据**（固定不变量，允许硬编码） | private BLOCK 首条规则、tcp/udp BLOCK 兜底、禁止 DIRECT | 渲染器代码本身 | `AGENTS.md` 铁律第 2 条要求"一律"，属于应用级不变量而非按订阅变化的期望态 |
| 运维/部署时静态模板拥有 | inbound 的 protocol/listen/port/TLS-Reality 参数骨架 | `infrastructure/marzban/xray_config.base.json` | 事实一、二 |
| 与 Xray desired state 无关，但真实在用（不要混进本 ADR 范围） | Mihomo 动态流量策略 | `TrafficRule` → `ops/forwarder/render_mihomo_config.py` | 事实四 |
| **尚未确定，需要专门 ADR/TASK 决策或人工核实部署拓扑，不在本 ADR 范围内解决** | Marzban 是否/如何动态管理 `inbounds[].settings.clients`（范围已缩小，见事实二）；Reality `privateKey`/`shortIds` 的持久化位置；Marzban 到底对应 `accounting_provider` 还是 `transport_provider_mode`（ADR-013 与 `config.py` 现有矛盾） | UNVERIFIED / DECISION REQUIRED | 事实二、五 |
| 运行时文件（Xray 当前 `config.json`）角色 | 只读——backup / rollback / drift detection / validation / post-reload 校验 | 明确禁止作为 desired state 的第二数据源（铁律第 1 条） | 已在 TASK-T16 前几轮记录 |

### B. 数据流（DB → desired state → renderer → candidate）

对已确定归属的部分（路由规则 + outbound 定义 + 路由匹配键），数据流
必须是：

```
GatewayRouteBinding (routing) ─────────────┐
Subscription.accounting_user_id (匹配键)    ├─→ 期望态 DTO（需要扩展，见下）─→ renderer ─→ candidate
EgressEndpoint (outbound 基础信息)          │
EgressBinding/Secret (凭据引用)            ─┘
```

不允许在这条链路的任何一环读取当前运行时 `config.json` 来补齐缺失字段。
静态模板文件（inbound 骨架）可以作为 renderer 的一个**输入**（不是数据库，
但也不是"当前运行时状态"，是部署时配置），只要 renderer 不依赖它保存
任何"跨渲染必须保持一致"的状态（这正是事实五暴露的问题——如果 Reality
私钥/shortIds 必须跨渲染稳定，它们就不能只活在这个无状态模板里，需要
挪进数据库或 Secret 表）。

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
  但如果最终确认 Marzban 自己动态管理这部分（事实二收窄后的候选），这个
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
（Reality 私钥、shortIds、未来可能的 client UUID）必须有一个明确的、
持久化的 source of truth（数据库或 Secret 表），而不是寄希望于"读一下
当前文件"——当前文件本身都靠不住。

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
它对"生成完整、可用的 Xray outbound"这个目标是**不足的**（见"结论 2"）
——这一条结论不受本轮事实三改正的影响（`user_routes`/`outbound_tags`
本身的字段形状不够用，和"route-match 用户名有没有持久化"是两个独立
问题）。

按 `AGENTS.md` 铁律第 5 条，修改 `backend/app/providers/base.py` 前必须
先有 ADR——本 ADR 记录这个不足，但**不在这里改这个文件**（Phase 2A 明确
禁止修改代码），具体怎么扩展留给 Phase 2B，Phase 2B 修改 `base.py` 时
可以引用本 ADR 作为其"先有 ADR"的依据，但仍需要在 Phase 2B 自己的 PR 里
写清楚最终选择的具体字段形状（本 ADR 不预先决定，因为 inbound 部分取决
于事实二这个 UNVERIFIED 问题先有答案）。

### G. 是否需要新增/修改 DB schema

需要，但具体列留给 Phase 2B/后续 ADR 决定，本 ADR 只记录必要性和已知
候选（**本轮已删除"`GatewayRouteBinding` 需要新增 username 字段"这一条
——事实三核实后确认不需要**）：

- Reality `privateKey`/`shortIds` 需要一个持久化位置（数据库列或 Secret
  表条目），不能只靠静态模板文件（事实五）。
- 如果事实二最终确认 Marzban 不会自己动态管理 client（即缺失机制这个
  分支成立），需要一张新表或对 `GatewayRouteBinding` 的扩展来持久化
  client UUID/密码等认证材料，并通过 `Secret` 机制加密存储。
- 如果确认 Marzban 自己动态管理这部分，则不需要新增任何 inbound 相关
  schema，但需要在文档里明确写清楚"Marzban 如何、以什么频率、通过什么
  机制往共享的 `xray_config.json` 写入 client"这件事，目前没有任何
  现存文档说清楚过。

## 约束

1. 本 ADR 不实现任何代码、schema、迁移改动——纯粹是所有权边界和数据流
   的决策记录。
2. 事实二收窄后的 Marzban client 管理机制问题、Reality 材料持久化位置、
   Marzban 对应 `accounting` 还是 `transport` 的矛盾，三者均标记
   `UNVERIFIED / DECISION REQUIRED`，本 ADR 不替它们下结论；Phase 2B
   开始前必须先有针对这三点的明确决定（可以是本 ADR 的后续修订，也可以
   是新的 ADR）。
3. `ops/gateway/render_xray_routes.py` 现有的、已经在生产部署路径上跑的
   数据流（`GatewayRouteBinding` JOIN `EgressEndpoint`/`EgressBinding` →
   完整 outbound）在 Phase 2B 设计新的期望态 DTO 时应当作为参考实现，
   不应该无视它重新发明一遍。
4. `TrafficRule` 是 Mihomo/forwarder 渲染器的真实数据源，Phase 2B 及
   之后任何工作都不得把它当作死代码删除或忽略；它和 Xray 期望态 DTO
   的扩展是两件不相关的事，不要混在一起改。

## 考虑过的替代方案

1. **继续假设"数据库已经能完整表达 Xray 配置"，直接在 `render()` 里
   拼一个看起来完整的 candidate**：会掩盖事实二里仍未解决的 inbound/
   client 缺口，产出一个在真实环境下可能无法正确认证客户端连接的配置，
   否决。
2. **把运行时 `config.json` 当作 inbound/client 部分的权威来源，读取后
   原样保留**：直接违反 `AGENTS.md` 铁律第 1 条，且事实五证明运行时文件
   本身也不可靠（每次可能被全量替换），否决。
3. **现在就断定 Marzban 管理一个完全独立、仓库看不见的 Xray 实例**：
   事实二的部署拓扑证据（compose 挂载同一份 `xray_config.json`）已经
   排除了这个候选和"共享同一份文件"候选的对等关系，继续断言"完全独立"
   与仓库内证据矛盾，否决——保留为收窄后的 UNVERIFIED（Marzban 是否/
   如何动态管理这份共享文件里的 client），不再假设两个独立实例。

## 安全影响

- 事实五描述的 Reality `privateKey`/`shortIds` 轮换问题如果在生产环境
  发生，会导致存量客户连接失效——这是一个高优先级的、独立于 Phase 2A
  本身的运维风险，建议记录为单独的 Issue/TASK 尽快核实生产环境
  `xray_config.base.json` 是否已经手动固定了这两个值。
- 事实三已更正：route-match 用户名有明确的持久化来源
  （`Subscription.accounting_user_id` + 确定性 fallback），这一条此前
  记录的"可能导致客户连接静默失效"的安全风险**不再成立**，本轮予以
  撤销。

## 重新评估条件

当事实二收窄后的问题（Marzban 是否/如何动态管理共享 `xray_config.json`
里的 client）、"Marzban 对应哪个 provider 分类"这两个 UNVERIFIED 问题
任一得到确认的答案时，重新评估并更新本 ADR 或提交后续 ADR，明确最终的
schema 变更范围。
