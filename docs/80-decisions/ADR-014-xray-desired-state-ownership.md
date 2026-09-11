# ADR-014: Xray desired-state 数据所有权边界

- 状态: 已接受
- 日期: 2026-09-11（2026-09-11 第二次修订：修正 route-match 用户名持久化、
  `TrafficRule` 用途、Reality shortIds、部署拓扑四处独立审查指出的事实
  错误；2026-09-11 第三次修订：新增"A2"明确铁律第 1 条与静态模板的边界，
  修正 `docs/10-deploy-new-server.md` 里仍不准确的 Reality 来源描述，
  更正 `GatewayRouteBinding.gateway_principal` 的设计意图判断，补充
  `Subscription.accounting_user_id` 持久化时机晚于渲染发生这一时序缺口；
  2026-09-11 第四次修订：收敛 Reality `dest`/`serverNames` 的最终归属
  （运维部署配置，唯一来源为环境变量/`Settings`，不再允许从模板读取），
  移除 A2 里"暂不强制归类"这个自相矛盾的中间状态）
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

### 事实三（已更正，本轮独立审查又补充了两处遗漏）：Xray 路由匹配用的
accounting username 有一个持久化 canonical source——
`Subscription.accounting_user_id`——但它的持久化时机晚于渲染发生的时刻，
且 `GatewayRouteBinding.gateway_principal` 当前实际写入的值和模型/测试
契约期望的值不一致

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

**`GatewayRouteBinding.gateway_principal` 的"设计意图 vs. 当前实现"这里
需要更正（本轮独立审查指出后核实确认，第一版把当前实现误判成设计
意图）。** 第一版说这个字段"是审计用途，设计上就不是 Xray 身份"——重新
核实模型 docstring、集成测试和实际调用方之后，这个说法把"当前实现的
错误/偏离"当成了"设计契约"，两者不是一回事：

- **模型契约**：`backend/app/models/gateway.py::GatewayRouteBinding` 的
  docstring 明确写"Maps one accounting user principal to exactly one
  Egress outbound."——按这个契约，`gateway_principal` 的设计意图就是
  **accounting principal**（账务/Xray 客户端身份），不是 Webshare 出口
  租户 ID。
- **测试契约**：`backend/tests/integration/test_db_adapters.py` 里
  `state.ensure_gateway_route_binding("marzban-user-1", dto)` 传入的是
  一个 accounting 风格的用户名（`"marzban-user-1"`），和 docstring 的
  契约一致。
- **当前生产 writer**：`SqlAlchemyProvisioningState.desired_routing_state()`
  实际调用的是 `self.ensure_gateway_route_binding(tenant.tenant_id, endpoint)`
  ——`tenant.tenant_id` 是 `EgressProvider.create_tenant()` 返回的
  Webshare 出口子账户 ID，不是 accounting principal。
- **部署 renderer**：`render_xray_routes.py::render_config()` 把
  `route.gateway_principal` 直接当 Xray 路由的 `"user"` 匹配值使用。

三者放在一起，暴露的是一个**当前实现内部不一致**，不是一个已经想清楚
的设计：模型/测试的契约期望这里存的是 accounting principal，但生产
writer 实际写入的是 egress tenant id，renderer 又把这个被写歪的字段
直接当 Xray 用户身份使用。第一版把"当前 writer 的行为"误当成"这个字段
的设计意图"来描述，这是需要更正的地方——正确的记录方式是：**这是一处
已确认的实现不一致，不是刻意的审计字段设计**，具体怎么收敛（改 writer
让它写 accounting principal，还是改模型契约承认它现在存的是 tenant id
另开字段存 accounting principal）是 Phase 2B 需要做的决策，本 ADR 不
替它决定。

**在这处不一致收敛之前，"通过 `GatewayRouteBinding.subscription_id` →
`Subscription.accounting_user_id` 就能拿到 Xray 路由匹配键"这条结论
需要加一个限定条件（见下方的时序问题）**：即使 schema 层面不需要新字段，
Phase 2B 仍然需要先解决 `gateway_principal` 当前写入的到底是什么、和
`accounting_user_id` 是否应该是同一个值这两个问题，而不是假设两者已经
自动对齐。

### 事实三补充：`Subscription.accounting_user_id` 的持久化时机晚于
`APPLY_GATEWAY` 步骤，Phase 2B 不能假设渲染时它已经存在

**本轮独立审查指出的时序问题，核实确认为真实存在。** 重新核对
`backend/app/domain/provisioning.py` 的编排顺序和
`backend/app/api/admin.py::admin_confirm_payment` 的调用顺序：

```python
# domain/provisioning.py（节选，编号对应 ProvisionStep）
self._start(run_id, ProvisionStep.CREATE_ACCOUNTING_USER)      # 步骤 6
self.accounting.create_user(...)
...
self._start(run_id, ProvisionStep.APPLY_GATEWAY)                # 步骤 7
desired_routing = self.state.desired_routing_state(request, endpoint, tenant)
candidate = self.gateway.render(desired_routing)
...
self.gateway.apply(candidate)
...
self._start(run_id, ProvisionStep.ISSUE_SUBSCRIPTION)           # 步骤 8
...
# 步骤 9 NOTIFY 之后，confirm_payment_and_provision() 才返回
```

```python
# api/admin.py::admin_confirm_payment（节选）
outcome = confirm_payment_and_provision(...)          # 完整跑完九步编排
subscription = db.scalar(select(Subscription)...)
...
if subscription.accounting_user_id is None:
    subscription.accounting_user_id = request.username  # 编排全部完成之后才写
    db.commit()
```

也就是说：`desired_routing_state()`（在 `APPLY_GATEWAY`，即第 7 步）
执行的时候，`Subscription.accounting_user_id` **还没有被写入**——它是在
整个九步编排（含第 8 步 `ISSUE_SUBSCRIPTION`、第 9 步 `NOTIFY`）全部跑完、
`confirm_payment_and_provision()` 返回之后，`admin_confirm_payment()`
才把它 commit 进去。这对 Phase 2B 的"完整 candidate 必须从数据库完整
期望态一次性生成"这个目标很关键：**如果 Phase 2B 的期望态查询打算读
`Subscription.accounting_user_id` 来生成路由匹配键，在首次开通这个时间
点上，这一列在渲染发生时可能还是 `NULL`**，不能假设它已经就绪。

第一版这里只写了"通过 `GatewayRouteBinding.subscription_id` JOIN
`Subscription.accounting_user_id` 即可"，没有考虑这个时序缺口，本轮
补充记录，不在本 ADR 里替 Phase 2B 选定解法，只列出候选（Phase 2B 自己
决定选哪个）：

- **候选 A**：恢复 `GatewayRouteBinding.gateway_principal` 的
  accounting-principal 语义（对齐模型 docstring 和测试契约），并确保
  在生产 writer 里及时、正确地持久化这个值——这同时解决上面"字段设计
  意图不一致"和"时序"两个问题，因为 `gateway_principal` 本身就是在
  `APPLY_GATEWAY` 这一步写入的，不依赖后续步骤。
- **候选 B**：把 `Subscription.accounting_user_id` 的写入提前到
  `CREATE_ACCOUNTING_USER`（第 6 步）成功之后、`APPLY_GATEWAY`（第 7 步）
  之前，并确保这段代码的事务/补偿语义正确（例如第 7 步失败时这一列要
  不要回滚）。
- **候选 C**：继续用 `Subscription.order_id` → `f"sub-{order.id}"` 这个
  确定性推导，但必须明确写成**正式契约**（不只是"没有持久化时的
  fallback"），并且要说明未来如果这个命名规则本身需要变更，会有什么
  迁移风险（例如已经渲染过的 Xray 路由规则里嵌入的旧 `sub-{id}` 值和
  新规则不一致）。

本轮不在这里选定 A/B/C 中的哪一个，留给 Phase 2B 自己决策。

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
| 应用/数据库拥有，**但持久化时机和字段一致性都还有缺口，不能当成已经解决** | Xray `"user"` 路由匹配键 | `Subscription.accounting_user_id`（有 canonical source，但在 `APPLY_GATEWAY` 渲染时可能还未写入）；`GatewayRouteBinding.gateway_principal`（模型/测试契约期望是 accounting principal，当前 writer 实际写入 Webshare tenant id，两者不一致） | 事实三（含第二轮补充） |
| 应用拥有，但**不来自数据库行数据**（固定不变量，允许硬编码） | private BLOCK 首条规则、tcp/udp BLOCK 兜底、禁止 DIRECT | 渲染器代码本身 | `AGENTS.md` 铁律第 2 条要求"一律"，属于应用级不变量而非按订阅变化的期望态 |
| 运维/部署时静态模板拥有，**范围严格限定为不因客户/部署内容而变的固定基础设施骨架**（见下方"A2"，不是数据库之外的第二个可变 desired-state 来源） | inbound 的 protocol/listen/port | `infrastructure/marzban/xray_config.base.json` | 事实一、二、A2 |
| **不应继续留在静态模板里、按铁律第 1 条的严格解读目前不合规，需要 Phase 2B 挪进数据库/Secret 表** | Reality `privateKey`/`shortIds` | 目前：模板为空时由 `_reality_settings()` 现场生成，从不持久化；应该：数据库或 `Secret` 表的持久化 canonical source | 事实五、A2 |
| **运维部署配置，唯一来源应为环境变量/`Settings`，不是数据库，也不该继续允许从模板文件读取**（本轮明确收敛，不再是"暂不归类"） | Reality `dest`/`serverNames` | 目前：模板优先、env 兜底（两个来源并存，不明确）；应该：只从 `XRAY_REALITY_DEST`/`XRAY_REALITY_SERVER_NAME` 这类环境变量/`Settings` 字段读取，Phase 2B 移除模板作为这两个字段的来源 | A2 |
| 与 Xray desired state 无关，但真实在用（不要混进本 ADR 范围） | Mihomo 动态流量策略 | `TrafficRule` → `ops/forwarder/render_mihomo_config.py` | 事实四 |
| **尚未确定，需要专门 ADR/TASK 决策或人工核实部署拓扑，不在本 ADR 范围内解决** | Marzban 是否/如何动态管理 `inbounds[].settings.clients`（范围已缩小，见事实二）；Marzban 到底对应 `accounting_provider` 还是 `transport_provider_mode`（ADR-013 与 `config.py` 现有矛盾） | UNVERIFIED / DECISION REQUIRED | 事实二 |
| 运行时文件（Xray 当前 `config.json`）角色 | 只读——backup / rollback / drift detection / validation / post-reload 校验 | 明确禁止作为 desired state 的第二数据源（铁律第 1 条） | 已在 TASK-T16 前几轮记录 |

### A2. `AGENTS.md` 铁律第 1 条与静态模板的边界（回应独立审查 Major 1，
本轮又收敛了 `dest`/`serverNames` 这个此前留白的候选）

**上一版把"静态模板拥有 inbound 的 protocol/listen/port/TLS-Reality 参数
骨架"整体列成一行，容易读成"模板是和数据库并列的第二个 desired-state
owner"，这和铁律第 1 条（"配置渲染一律从数据库全量生成，禁止增量拼接"）
表面上冲突——独立审查指出后核实确认这个混淆是真实存在的，需要把四类
概念明确分开，而不是笼统地说"模板也是一种归属"。第二次修订已经区分出
前三类，但把 Reality `dest`/`serverNames` 用"暂不强制归类"搁置，被
独立审查指出这个中间状态本身就是自相矛盾——同一个 ADR 一边说"会因部署/
客户/服务器身份变化而影响候选配置的内容都必须有 DB/Secret canonical
source"，一边又把符合这个定义的 `dest`/`serverNames` 排除在外。本轮
补上第四类，给出单一、明确的结论，不再留白：**

1. **可变 desired state（必须有数据库/Secret canonical source，不允许
   活在无状态模板里）**——凡是会因**客户/订阅/业务事件**变化而影响候选
   配置的内容属于这一类。已确认属于这一类、且已经有数据库 canonical
   source 的：路由规则、outbound 连接细节。**已确认属于这一类、但目前
   没有数据库/Secret canonical source 的（这是不合规现状，不是可以
   接受的模板归属）**：Reality `privateKey`/`shortIds`——这两个值由
   系统自己生成（`_x25519_private_key()`/`secrets.token_hex(8)`），
   不是运维手填的固定配置，理论上每套部署应该固定、可追踪、可轮换、
   可审计，性质上更接近需要持久化状态的系统生成材料，而不是运维一次性
   录入的部署参数——这是把它们和 `dest`/`serverNames` 区分开、放进不同
   类别的关键判据（见下方第 4 类）。
2. **代码级固定安全不变量（允许硬编码在渲染器代码里，不是数据源问题）**
   ——private BLOCK 首条规则、tcp/udp BLOCK 兜底、禁止 DIRECT。这些不
   随客户/部署变化，`AGENTS.md` 铁律第 2 条本身要求"一律"，硬编码是
   正确做法，不构成对铁律第 1 条的例外，因为它们根本不是"配置渲染"意义
   上的期望态数据，是渲染逻辑本身的不变约束。
3. **静态模板（严格限定为不随客户/部署内容变化的固定基础设施骨架）**
   ——本 ADR **不**把整个 `xray_config.base.json` 都当作合法的、和数据库
   并列的 desired-state 来源；模板真正应该保留的内容收窄为
   **inbound 的 protocol/listen/port** 这类纯粹的基础设施接线信息（同一
   套部署里几乎不会因为哪个客户下单而改变），不包括任何 Reality 字段
   （包括 `dest`/`serverNames`——本轮之前的版本还允许它们从模板读取，
   本轮收窄为不再允许，理由见下方第 4 类）。
4. **（本轮新增，收敛第一次修订遗留的留白）运维部署配置——不是可变
   desired state，不是数据库来源，也不该继续允许从模板文件读取，唯一
   canonical source 应为环境变量/`Settings`**：Reality `dest`/
   `serverNames`。判据：这两个值是运维在搭建某一套 Xray Reality 伪装
   身份时**一次性手动选定**的参数（伪装成哪个目标域名/哪些 SNI），不是
   系统生成、也不会随客户下单/退订变化——变化频率和触发方式和
   `protocol`/`listen`/`port`、以及本仓库已有先例 `SITE_DOMAIN`/
   `API_DOMAIN`/`SUBSCRIPTION_DOMAIN`（ADR-006，同样是"影响渲染结果但
   由运维经环境变量配置，不进数据库"的先例）一致，因此不落进铁律第 1 条
   针对的"业务期望态"范畴；但它们目前的实现（模板优先、env 兜底）比
   `protocol`/`listen`/`port`（只从模板读）更含糊，本轮予以收敛：
   **唯一 canonical source 定为 `XRAY_REALITY_DEST`/
   `XRAY_REALITY_SERVER_NAME` 这类环境变量/`Settings` 字段，Phase 2B
   移除模板作为这两个字段的读取来源**，不再允许模板和 env 两个源并存。

**本 ADR 的决定是：不对 `AGENTS.md` 铁律第 1 条做例外覆盖，且不允许
任何 Reality 字段继续从静态模板读取**——不采用"模板作为 desired-state
的一个合法来源，需要写覆盖范围/版本管理/漂移检测"这条路径。理由：
Reality `privateKey`/`shortIds` 一旦被承认为"模板可以合法拥有的内容"，
就需要一整套模板版本管理、漂移检测、DB/模板冲突时优先级判定的机制，这套
机制目前完全不存在，而且这两个值本质上是**应用需要能追踪、轮换、审计的
系统生成材料**，更适合走已有的 `Secret` 表机制，而不是新开一套"模板
管理"体系；`dest`/`serverNames` 虽然不是敏感材料，但它们是运维手填的
部署参数，和数据库期望态的性质不同，也不需要新建模板治理机制——直接
类比已有的 `SITE_DOMAIN` 等环境变量配置模式即可，不需要额外机制。

以下逐项回答本轮审查要求的验收清单（针对 `dest`/`serverNames` 归为
"运维部署配置"这个结论）：

- **为什么不属于铁律第 1 条的 desired state**：铁律第 1 条约束的是随
  客户/订阅/业务事件变化的期望态（路由、outbound），`dest`/
  `serverNames` 不随这些事件变化，变化触发方式和运维改
  `SITE_DOMAIN`（ADR-006）一致。
- **authoritative source**：`XRAY_REALITY_DEST`/`XRAY_REALITY_SERVER_NAME`
  环境变量（读取方式待 Phase 2B 决定是否正式收进 `Settings` 类，本 ADR
  只定"必须是环境变量，不是模板文件"这个边界）。
- **运维如何修改**：编辑目标机 `.env`/`/etc/lingsway/*.conf` 并重新执行
  渲染（`ops.gateway.render_xray_routes`），与修改 `SITE_DOMAIN` 等现有
  环境变量的流程一致，不需要新机制。
- **版本化**：不需要应用层版本化机制，和其它环境变量一样，由运维自己的
  变更记录/部署清单（`inventory.yml` 等）追踪。
- **审计**：不是敏感材料，部署时的常规变更记录已经足够，不需要新增
  专门的审计表；这一点和 `privateKey`/`shortIds`（需要走 `Secret` 机制
  因而自带审计能力）不同。
- **漂移检测**：Phase 2B 的验证步骤（类比 `70_verify.sh`）可以断言渲染
  出的候选配置里的 `dest`/`serverNames` 与当前环境变量一致；这是 Phase
  2B 的实现细节，本 ADR 只要求这个检测点存在。
- **数据库与运维配置冲突时谁优先**：不会发生冲突——按本决定，
  `dest`/`serverNames` 永远不来自数据库，只有"环境变量缺失"这一种失败
  模式。
- **缺失时是否 fail-closed**：是，现状已经如此
  （`_reality_settings()` 在两者都为空时 `raise XrayRenderError`），
  Phase 2B 把来源从"模板或 env"收窄为"只有 env"之后，这个 fail-closed
  行为不变。
- **Phase 2B renderer 如何读取且仍满足项目铁律**：`render_xray_routes.py`
  今后读取 `dest`/`serverNames` 时只查环境变量/`Settings`，不再读
  `base_config` 里的这两个字段；`base_config`（模板）继续只提供
  `protocol`/`listen`/`port` 这类结构骨架。这样一来，模板不再是任何
  Reality 字段的数据来源，数据库继续是路由规则/outbound 这些真正业务
  desired state 的唯一来源，`privateKey`/`shortIds` 走 `Secret`，
  `dest`/`serverNames` 走环境变量——四类各自唯一，不再有任何字段可以
  从两个不同来源读到不同的值。

这不代表模板本身要被移除——protocol/listen/port 这类真正固定的基础
设施接线信息继续留在模板里是合理的（这类内容变化时通常伴随一次有意识
的运维操作，而不是"客户下单/退订"这种业务事件驱动的变化，用固定文件
承载比每次查数据库更简单，也不违反铁律第 1 条的精神——铁律第 1 条要
禁止的是"业务期望态的增量拼接"，不是"禁止一切非数据库配置输入"）。

### B. 数据流（DB → desired state → renderer → candidate）

对已确定归属的部分（路由规则 + outbound 定义 + 路由匹配键），数据流
必须是：

```
GatewayRouteBinding (routing) ─────────────┐
Subscription.accounting_user_id (匹配键，*) ├─→ 期望态 DTO（需要扩展，见下）─→ renderer ─→ candidate
EgressEndpoint (outbound 基础信息)          │
EgressBinding/Secret (凭据引用)            ─┘

  * 见事实三：这一列的持久化时机晚于 APPLY_GATEWAY 渲染发生的时刻，
    且 GatewayRouteBinding.gateway_principal 当前写入值与模型/测试
    契约期望值不一致——Phase 2B 必须先在候选 A/B/C 中选定收敛方向，
    这张数据流图才算真正成立，本 ADR 不预先假设已经解决。
```

不允许在这条链路的任何一环读取当前运行时 `config.json` 来补齐缺失字段。
按 A2 收敛后的结论，renderer 的输入分三个互不重叠的来源，每类字段只有
唯一 canonical source：

- 静态模板文件——仅限 inbound `protocol`/`listen`/`port` 这类固定基础
  设施骨架，不再提供任何 Reality 字段。
- 环境变量/`Settings`——仅限 Reality `dest`/`serverNames` 这类运维一次性
  录入的部署参数（A2 第 4 类），Phase 2B 移除模板作为这两个字段的
  读取来源。
- 数据库/`Secret` 表——路由规则、outbound 连接细节、路由匹配键（均已有
  数据流，见上）以及 Reality `privateKey`/`shortIds`（A2 第 1 类，目前
  尚未实现，是 Phase 2B 的持久化目标）。

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
候选（**"`GatewayRouteBinding` 需要新增 username 字段"这一条候选仍然
不需要**——事实三核实后确认 `Subscription.accounting_user_id` 已有
canonical source；但本轮新增了两项和"字段一致性/写入时机"相关的候选，
不是新的 schema 列，而是**代码/编排层面**需要 Phase 2B 解决的问题）：

- Reality `privateKey`/`shortIds` 需要一个持久化位置（数据库列或 Secret
  表条目），不能只靠静态模板文件（事实五、A2——A2 明确了这是按铁律第 1
  条严格解读目前不合规的现状，不是可以接受的模板归属）。
- **（本轮明确，不再是候选）Reality `dest`/`serverNames` 不需要任何
  数据库 schema**——A2 第 4 类已经决定它们的 canonical source 是环境
  变量/`Settings`，Phase 2B 只需要把 renderer 的读取来源从"模板或 env"
  收窄为"只读 env"，不涉及数据库改动。
- 如果事实二最终确认 Marzban 不会自己动态管理 client（即缺失机制这个
  分支成立），需要一张新表或对 `GatewayRouteBinding` 的扩展来持久化
  client UUID/密码等认证材料，并通过 `Secret` 机制加密存储。
- 如果确认 Marzban 自己动态管理这部分，则不需要新增任何 inbound 相关
  schema，但需要在文档里明确写清楚"Marzban 如何、以什么频率、通过什么
  机制往共享的 `xray_config.json` 写入 client"这件事，目前没有任何
  现存文档说清楚过。
- **（本轮新增）`GatewayRouteBinding.gateway_principal` 当前写入值和
  模型/测试契约期望值不一致的问题**：不一定需要新增 schema 列，但
  Phase 2B 必须先决定收敛方向（事实三候选 A/B/C 之一），否则"路由匹配键
  从数据库哪里读"这件事本身就是模糊的。
- **（本轮新增）`Subscription.accounting_user_id` 的持久化时机问题**：
  同样不一定需要新增 schema 列（取决于选哪个候选），但如果 Phase 2B
  选择候选 B（提前持久化时机），需要重新设计这段代码在
  `CREATE_ACCOUNTING_USER` 之后、`APPLY_GATEWAY` 之前的事务/补偿语义。

## 约束

1. 本 ADR 不实现任何代码、schema、迁移改动——纯粹是所有权边界和数据流
   的决策记录。
2. 事实二收窄后的 Marzban client 管理机制问题、Marzban 对应
   `accounting` 还是 `transport` 的矛盾，均标记
   `UNVERIFIED / DECISION REQUIRED`，本 ADR 不替它们下结论；Phase 2B
   开始前必须先有针对这两点的明确决定（可以是本 ADR 的后续修订，也可以
   是新的 ADR）。Reality 材料持久化位置**不再标记为 UNVERIFIED**——A2
   已经决定"必须挪进数据库/Secret，不接受模板长期拥有"这个方向，具体
   落在哪张表/哪个字段留给 Phase 2B，但方向已定。
3. `ops/gateway/render_xray_routes.py` 现有的、已经在生产部署路径上跑的
   数据流（`GatewayRouteBinding` JOIN `EgressEndpoint`/`EgressBinding` →
   完整 outbound）在 Phase 2B 设计新的期望态 DTO 时应当作为参考实现，
   不应该无视它重新发明一遍。
4. `TrafficRule` 是 Mihomo/forwarder 渲染器的真实数据源，Phase 2B 及
   之后任何工作都不得把它当作死代码删除或忽略；它和 Xray 期望态 DTO
   的扩展是两件不相关的事，不要混在一起改。
5. **（本轮新增）Phase 2B 开始前，必须先在事实三的候选 A/B/C 中选定
   `gateway_principal`/`accounting_user_id` 的收敛方向**——这不是可以
   在写 DTO 的过程中顺便决定的细节，它直接决定期望态查询该读哪个字段、
   该在编排的哪一步读。
6. 静态模板（`xray_config.base.json`）今后只承载 A2 里收窄后的固定
   基础设施骨架（inbound protocol/listen/port），不得把新的、会随部署/
   客户变化的内容悄悄塞回模板来"绕开"这条边界；Reality `dest`/
   `serverNames`/`privateKey`/`shortIds` 四个字段一律不得继续从模板
   读取（前两者改读环境变量/`Settings`，后两者改读数据库/`Secret`）。

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
4. **（本轮新增）把静态模板正式定义为和数据库并列的合法 desired-state
   来源，为它写覆盖范围/版本管理/漂移检测/DB-模板冲突优先级/验收测试**：
   这条路径技术上可行，但需要新建一整套目前完全不存在的模板治理机制，
   而 Reality 身份材料本质上更适合用已有的 `Secret` 表机制管理（可
   追踪、可轮换、可审计），没有必要为了保留"模板"这个形式而新建一套
   平行的治理体系，否决——见 A2。

## 安全影响

- 事实五描述的 Reality `privateKey`/`shortIds` 轮换问题如果在生产环境
  发生，会导致存量客户连接失效——这是一个高优先级的、独立于 Phase 2A
  本身的运维风险，建议记录为单独的 Issue/TASK 尽快核实生产环境
  `xray_config.base.json` 是否已经手动固定了这两个值。A2 进一步明确：
  这不是"模板管理方式的选择问题"，而是"这两个值按铁律第 1 条本来就不
  应该只活在模板里"，优先级应视为需要尽快解决，不是可以无限期搁置的
  设计讨论。
- **（本轮新增）Reality `dest`/`serverNames` 目前"模板优先、env 兜底"
  的双来源实现本身是一个较低但真实的风险**：如果某次部署的模板文件和
  当前环境变量的值不一致（例如运维只改了 env 却忘了模板也曾经手填过
  值），渲染结果会悄悄使用模板里的旧值而不是 env 里的新值，且没有任何
  报错或提示——这不是本 ADR 之前分析出的"轮换导致失效"那类风险，而是
  "配置来源不唯一导致悄悄用错值"的风险。A2 第 4 类的决定（唯一来源收窄
  为 env）直接消除这个风险，Phase 2B 实现时应确保移除模板读取路径，
  不能只是"多一个来源判断优先级"。
- 事实三已更正：route-match 用户名有一个明确的持久化来源
  （`Subscription.accounting_user_id`），但**本轮（第三次修订）发现这个
  来源的持久化时机晚于 `APPLY_GATEWAY` 渲染发生的时刻**，且
  `GatewayRouteBinding.gateway_principal` 当前实际写入值与模型/测试
  契约期望值不一致——"这一条安全风险不再成立"的说法需要撤回：如果
  Phase 2B 不解决这两个问题就直接假设"读
  `Subscription.accounting_user_id` 就能拿到正确的路由匹配键"，仍然
  可能产生和实际客户连接不一致的 Xray 路由规则，具体后果取决于
  Phase 2B 选择候选 A/B/C 中的哪一个来解决。

## 重新评估条件

当事实二收窄后的问题（Marzban 是否/如何动态管理共享 `xray_config.json`
里的 client）、"Marzban 对应哪个 provider 分类"、事实三的
`gateway_principal`/`accounting_user_id` 收敛方向（候选 A/B/C）这几个
问题任一得到确认的答案时，重新评估并更新本 ADR 或提交后续 ADR，明确
最终的 schema 变更范围。
