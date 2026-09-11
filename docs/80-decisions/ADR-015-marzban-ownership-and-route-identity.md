# ADR-015: 关闭 Phase 2B 前置决策——Marzban 运行时归属、provider 分类、
route identity 收敛方案

- 状态: 已接受
- 日期: 2026-09-11（2026-09-11 第二次修订：独立审查指出并核实了四处
  事实错误——Marzban 确实会在特定管理 API 路径下写回 `XRAY_JSON`、
  普通用户 CRUD 走增量 Handler API 而非"每次都 include_db_users()+
  重启"、Marzban 确实有真实的远端节点管理能力（此前"完全没有"的说法
  错误）、候选 A 缺少既有数据的收敛方案——已全部修正，见下方各节）
- 决策范围: TASK-T16 Phase 2B0（只读研究 + ADR/TASK 决策，不实现代码）
- 前置: `docs/80-decisions/ADR-014-xray-desired-state-ownership.md`
  （Phase 2A，已接受，本 ADR 不重新讨论其中已 Accepted 的 Reality
  ownership 结论——inbound protocol/listen/port 归静态模板、Reality
  `dest`/`serverNames` 归环境变量、Reality `privateKey`/`shortIds` 归
  DB/Secret、routing/outbound/route identity 归 DB、运行时 config 只做
  只读校验/回滚/漂移检测——除非本 ADR 明确标注 `NEW EVIDENCE`）

## Context

ADR-014 在 Phase 2A 结尾留下三个未关闭的前置问题，明确写着不能在
`DesiredRoutingState`/DB schema/Xray renderer 代码改动之前跳过：

A. Marzban（当前 Compose 锁定版本 `gozargah/marzban:v0.8.4`，对应
   commit `7f396db3e703d71a28060bc9ce4a532ec64cb1f4`）在共享
   `xray_config.json` 拓扑下，是否/如何管理 `inbounds[].settings.clients`。
B. Marzban 在本仓库架构里最终应归为 `AccountingProvider`、
   `TransportProvider`，还是拆成两个 adapter；解决 `ADR-013` 与
   `config.py` 的现有矛盾。
C. `GatewayRouteBinding.gateway_principal`/`Subscription.
   accounting_user_id` 的收敛方案（ADR-014 的候选 A/B/C）。

本 ADR 只读研究这三个问题，产出明确结论，不实现任何代码/schema/
migration 改动——这些留给真正的 Phase 2B 实现 PR。

**方法**：全程只读检索 `github.com/Gozargah/Marzban` 精确 `v0.8.4` tag
（commit `7f396db3e703d71a28060bc9ce4a532ec64cb1f4`）下的公开源码
（`raw.githubusercontent.com/Gozargah/Marzban/7f396db3.../...`），未连接
任何真实 Marzban 实例，未使用任何真实凭据、真实 token，未调用真实 API，
未读取生产 SQLite/配置。

## Part A：Marzban v0.8.4 的 client lifecycle（第二次修订，已更正两处
事实错误）

**第一版这里的结论不完整/有误，独立审查指出后逐行核实确认，完整改正
如下。** 第一版只读了 `app/xray/config.py`/`app/xray/core.py` 里"从
数据库重建整个配置、通过 stdin 灌给 Xray 子进程"这一条路径，就得出了
"Marzban 从不写回 `XRAY_JSON`"和"每次用户变化都要 include_db_users()+
重启"这两个过度概括的结论——这条路径确实存在，但**不是唯一路径**，也
不是"每次普通用户变化"都会走的路径。核实 `app/routers/core.py`、
`app/xray/operations.py`、`app/routers/user.py` 之后，真实模型必须
拆成三条路径：

### Path A——Marzban 应用/core 启动，或管理员显式调用 core-config API

`app/xray/config.py::include_db_users()`——从 Marzban 自己的数据库
（`db_models.User`/`db_models.Proxy`）查询 active 用户，在**内存里**
重建 `XRayConfig` 对象的 `clients` 列表；随后 `app/xray/core.py` 的
`restart()`（`stop()`+`start()`）把这个内存对象序列化后**通过 stdin**
（`subprocess.Popen([..., "-config", "stdin:"], stdin=subprocess.PIPE)`,
`self.process.stdin.write(config.to_json())`）灌给 Xray 子进程——这一步
**不写文件**。这条路径在 Marzban 应用进程自己启动/重建时发生一次，
把磁盘上的 `XRAY_JSON`（本仓库对应共享的 `xray_config.json`）当作
**读取一次的初始骨架**。

**这一路径之外，还存在一条会写文件的路径，第一版完全没有覆盖**：
`app/routers/core.py::modify_core_config()`（对应 `PUT
/api/core/config` 管理 API）——核实其函数体，确认它执行：

```python
config = XRayConfig(payload, api_port=xray.config.api_port)  # 校验
xray.config = config
with open(XRAY_JSON, "w") as f:
    f.write(json.dumps(payload, indent=4))
xray.core.restart(startup_config)
for node_id, node in list(xray.nodes.items()):
    if node.connected:
        xray.operations.restart_node(node_id, startup_config)
```

**这确实会把 payload 写回 `XRAY_JSON` 磁盘文件**，同时重启 core 和所有
已连接的远端节点。这是一个真实存在、被管理员/管理 API 触发的写文件
路径，第一版"Marzban 从不写回这个文件"的结论是错的，必须更正。

### Path B——普通创建用户（不涉及 core-config API，不写文件，不整体
重启）

核实 `app/routers/user.py` 的 `add_user` 端点：创建用户记录之后，
`bg.add_task(xray.operations.add_user, dbuser=dbuser)`——不是走
`include_db_users()`+`restart()`。核实 `app/xray/operations.py::
add_user()`：内部调用 `_add_user_to_inbound(xray.api, inbound_tag,
account)`，最终落到 `api.add_inbound_user(tag=inbound_tag,
user=account, timeout=30)`——这是 Xray 自己的 **gRPC Handler API**
（对运行中的 Xray 进程做增量修改，不是重新灌一份完整配置，也不涉及
任何文件读写）。

### Path C——普通修改/禁用用户（同样走增量 Handler API，不写文件）

核实 `app/xray/operations.py`：`update_user()` 调用
`_alter_inbound_user(xray.api, inbound_tag, account)`，内部依次调用
`remove_inbound_user()` 再 `add_inbound_user()`（先删后加，仍是增量
Handler API 调用）；`remove_user()` 调用 `_remove_user_from_inbound
(xray.api, inbound_tag, email)`，落到 `api.remove_inbound_user(tag=
inbound_tag, email=email, timeout=30)`。这两者都**不调用
`include_db_users()`，不触发整体 core 重启，不涉及任何文件读写**。

### 逐项结论（更正后）

1. **Marzban 如何读取 `/code/xray_config.json`？**
   `CONFIRMED`——环境变量 `XRAY_JSON`（`.env.example` 默认
   `"xray_config.json"`）指定路径；Path A 在 Marzban 应用进程自己
   启动时读取一次，作为初始骨架。
2. **user/client 数据的 authoritative source 是什么？**
   `CONFIRMED`——Marzban 自己的数据库（本仓库部署拓扑对应挂载的
   `db.sqlite3`），不是 `xray_config.json`，不是本应用 MySQL。
3. **创建/disable/update Marzban user 时是否修改 `xray_config.json`？
   是否重新生成文件？只调用 Xray API？是否有内部 runtime config？**
   `CONFIRMED`（已更正为区分三条路径，不是笼统一句话）——**普通用户
   CRUD（Path B/C）不修改这个文件、不整体重启，走 Xray gRPC Handler
   API 增量修改运行中的进程**；**只有管理员显式调用 `PUT
   /api/core/config`（Path A 的一个分支）才会把新配置写进
   `XRAY_JSON` 并整体重启 core+已连接节点**；Marzban 应用/core 自己
   启动时（Path A 的另一个分支）会读这个文件一次做初始骨架，然后走
   `include_db_users()`+stdin，不写文件。
4. **Marzban 是否把用户映射为 `inbounds[].settings.clients`？**
   `CONFIRMED`——是，`include_db_users()` 的 `clients.append(client)`
   逻辑（Path A 场景下，在内存对象里）；Path B/C 的增量 Handler API
   调用不直接操作这个 JSON 结构，是对运行中 Xray 进程状态的增量修改，
   效果上等价于"让运行中的 inbound 多了/少了一个 client"，但不经过
   `XRayConfig.clients` 这个 Python 对象。
5. **共享的、bind-mounted 的 `xray_config.json` 会不会被 Marzban
   写回？**
   `CONFIRMED`（已更正，第一版的"不会"是错的）——**会**，通过
   `modify_core_config()`（`PUT /api/core/config`）这个管理 API。这是
   一个真实存在的写路径，不是"从不写回"。
6. **如果本仓库的 renderer 再次全量替换这个文件，会不会覆盖 Marzban
   管理的 clients？**
   `CONFIRMED`（部分更正）——**对普通用户 CRUD（Path B/C）产生的
   client 状态无影响**：这些状态活在运行中的 Xray 进程内存里（通过
   gRPC Handler API 维护），和磁盘文件无关，renderer 换文件不会
   影响它们，除非 Marzban 应用进程本身重启（这时才会重新读文件当
   骨架，重新 `include_db_users()`）。**但如果运维/管理员通过 Marzban
   的 `PUT /api/core/config` 写过这个文件（Path A 的写分支），本仓库
   renderer 再全量覆盖会丢弃管理员通过这个 API 做的改动**——这正是
   Major 1 指出的真实冲突场景：**两个都能写同一个文件的主体**（本仓库
   renderer 和 Marzban 的 core-config 管理 API），如果都被使用，会
   产生"最后写入者获胜"的竞态，不是"repo renderer 可以安全任意覆盖"
   这么简单。见下方"Decision 1"的 ownership policy。
7. **Marzban user UUID/password/email 等认证材料的 canonical source
   实际在哪里？**
   `CONFIRMED`——Marzban 自己的数据库，不变。
8. **本应用是否应该自己管理这些 client fields？**
   `INFERENCE`——不应该，理由不变：Marzban 已经完整拥有 client 生命
   周期管理，本应用应该通过 `AccountingProvider.create_user()` 驱动
   Marzban 管理，而不是重复实现。

### Decision 1（已更正）

**Marzban v0.8.4 是否管理 Xray inbound clients？**

**`YES`**——精确写法（按独立审查要求的措辞）：

> Marzban DB owns durable user/proxy state; ordinary user CRUD updates
> running Xray incrementally through the gRPC Handler API
> (`add_inbound_user`/`remove_inbound_user`), not by rewriting
> `XRAY_JSON`; Marzban's own app/core startup or a full core rebuild
> regenerates clients from its DB into an in-memory `XRayConfig` and
> feeds it to the Xray subprocess via stdin. Marzban does **not**
> persist ordinary user-CRUD changes into `XRAY_JSON`, **but its admin
> core-config API (`PUT /api/core/config`) can and does write
> `XRAY_JSON` directly** — so Lingsway must explicitly own/guard that
> shared-file write boundary, not assume Marzban is a pure reader.

**共享 `XRAY_JSON` 的 ownership policy（本 ADR 新增决定）**：

- **本仓库的 renderer（`ops/gateway/render_xray_routes.py`）是这份
  共享骨架文件唯一被允许的、有意的 writer。**
- **Lingsway 不得调用 Marzban 的 `PUT /api/core/config`**——本应用
  对 Marzban 的所有交互（通过真实
  `AccountingProvider`/Marzban-backed adapter 实现）只走用户 CRUD 类
  端点（Path B/C 的增量 Handler API 路径），不走 core-config 管理端点。
- **运维不得通过 Marzban 管理 UI/API 手动修改 core config**——这条是
  运维流程约束，不是代码能强制的技术边界；Phase 2B/后续实现阶段应该
  在权限层面评估能否限制到这个 API（例如不给运维账号开放
  `/api/core/config` 的调用权限，或只允许通过本仓库自己的部署脚本
  触发），本 ADR 只记录这个方向，不在这里替 Phase 2B 决定具体怎么做。
- **如果权限层面暂时做不到完全阻止**，Phase 2B/后续验证阶段必须有
  **drift detection**：在 renderer 每次运行前，先读取磁盘上当前
  `xray_config.json` 的 `outbounds`/`routing` 部分，和数据库期望态
  比对是否被非 renderer 的写入者动过手脚（例如与上一次 renderer 自己
  写入的内容不一致），如果检测到漂移，应该 **fail closed 并明确
  告警**，不能静默覆盖——这是把"repo renderer 可以安全覆盖"这个第一版
  过于乐观的结论，改成"repo renderer 是唯一合法 writer，其它写入需要
  被检测并拒绝/告警"这个更谨慎的模型。
- **不允许形成"DB renderer + Marzban core-config API"两个并列、互不
  知情的 source of truth**——这是本 ADR 明确否决的候选（不重新打开
  ADR-014 的铁律第 1 条讨论，只是把它应用到这个具体的双 writer 场景）。

Part A 第 6 点发现的另一个独立约束（renderer 产出的 inbound tag 不能
破坏 Marzban 依赖的协议匹配）仍然成立，一并记入 Phase 2B 验收标准。

## Part B：Marzban 的 provider classification（第二次修订，已更正
node capability 的事实错误）

### `AccountingProvider`/`TransportProvider` Protocol 与 `marzban_*`
配置字段（不变，第一版这部分证据是对的）

**`AccountingProvider` Protocol**（`backend/app/providers/base.py` +
ADR-011 扩展）：`create_user(username, quota_bytes, expire_at)`、
`disable_user(username)`、`set_quota`、`set_expire`、
`get_connection_links(username)`、`get_usage(username)`。

**`TransportProvider` Protocol**（`backend/app/providers/base.py`，
ADR-011）：`health_check()`、`sync_nodes()`、
`list_endpoints() -> list[TransportEndpointDTO]`、
`get_endpoint(external_id)`、`enable()`、`disable()`、
`get_capacity() -> TransportCapacityDTO | None`。

**`Settings` 里全部 `marzban_*` 字段**（`marzban_base_url`、
`marzban_admin_username`、`marzban_admin_password`、
`marzban_default_protocol`、`marzban_default_inbounds_json`、
`marzban_verify_tls`）——逐一对应"调用 Marzban admin API 创建/管理
用户"所需信息，精确匹配 `AccountingProvider`，和
`TransportProvider`（节点库存/容量）没有字段层面的交集。这一点第一版
是对的，不变。

### `TransportProvider` ↔ Marzban Node capability 映射表（本轮新增，
更正第一版"Marzban 完全没有 node 能力"的事实错误）

**第一版这里的结论是错误的，独立审查指出后核实确认，完整改正如下。**
重新读取 `app/routers/node.py`，确认 Marzban v0.8.4 **确实有**一套
完整的远端节点管理 API：`POST /api/node`（add）、`GET /api/nodes`
（list）、`GET /api/node/{id}`（get）、`PUT /api/node/{id}`（modify）、
`POST /api/node/{id}/reconnect`（reconnect）、
`DELETE /api/node/{id}`（remove）、`GET /api/nodes/usage`（usage）、
以及节点连接状态/日志相关端点。第一版"Marzban 完全没有节点库存能力"
这句话是事实错误，必须删除。

逐项能力映射：

| Lingsway `TransportProvider` | Marzban v0.8.4 capability | Exact / Partial / No match |
|---|---|---|
| `health_check` | 没有单独对应"transport 整体健康"的端点；有 `GET /api/system` 之类的系统统计，语义不对等 | No match |
| `sync_nodes` | `POST /api/node`（add）+ `GET /api/nodes`（list）能维护 Marzban **自己的远端 Xray 执行节点**清单，但这不是"从外部上游订阅/机场服务同步一份代理节点库存" | No match（不同 bounded context，见下） |
| `list_endpoints` | `GET /api/nodes` 列出 Marzban 自己的远端执行节点 | Partial（表面都是"列表"，语义不同，见下） |
| `get_endpoint` | `GET /api/node/{node_id}` | Partial（同上） |
| `enable` | 没有直接的"enable"，`modify_node` 可以间接影响节点状态 | No match |
| `disable` | 同上，且 `DELETE /api/node/{id}` 是彻底移除而不是"禁用" | No match |
| `get_capacity` | `GET /api/nodes/usage` 返回用量统计，不是"库存容量" | Partial（都涉及"用量/统计"但对象不同） |

**两个"node"概念不是同一个 bounded context，逐项论证**：

1. **Lingsway `TransportProvider`**（`transport/subscription.py::
   SubscriptionTransportProvider` 是唯一真实实现）描述的是"从一个
   外部机场/订阅供应商的 API/订阅链接同步一份**可用代理节点库存**"
   ——这些节点是**第三方运营的、供 Mihomo 转发层选择使用的上游出口**，
   Lingsway 对它们没有部署/运维控制权，只有"读取库存、选用哪个"的
   关系。
2. **Marzban `Node`**（`app/routers/node.py` 描述的对象）是 **Marzban
   自己部署管理的远端 Xray-core 执行服务器**，用于横向扩展 Marzban
   自己的入站流量处理能力（多地部署 Xray 核心，Marzban 面板统一
   管理/下发配置/看日志/看用量）——这些节点**由运营 Marzban 的一方
   自己部署和控制**，是 Marzban **自己的基础设施拓扑**，不是"从外部
   供应商同步来的库存"。

两者字段形状表面相似（都有"添加/列表/获取/修改/删除节点"），但描述的
是完全不同的东西：一个是"我们自己控制的、用来跑 Xray 核心的服务器
拓扑"，一个是"第三方运营的、我们只能读取库存的代理出口"。**为了复用
接口形状把 Marzban Node 塞进 `TransportProvider` contract 是错误的
硬映射**，不能因为方法名字看起来像就当同一件事处理。

### Decision 2（分类结论不变，理由更正）

**Marzban 的最终 provider classification：`ACCOUNTING`。**

**更正后的理由**（不再包含"Marzban 完全没有节点能力"这个错误前提）：
`marzban_*` 配置字段形状和 `AccountingProvider` Protocol 精确对应；
Marzban 确实有真实的节点管理能力，但那是它自己的 Xray 执行拓扑
（一个不同的 bounded context），不符合 Lingsway `TransportProvider`
描述的"上游第三方代理库存"契约——**因此不是"能力不存在"，而是"能力存在
但不匹配这个契约"，本仓库不应该为了这个不匹配的能力去实现
`TransportProvider`**。

**方案 3（SPLIT）不成立的更正后理由**：SPLIT 的前提是 Marzban 提供的
某项能力**符合** `TransportProvider` 契约、值得单独拆一个 adapter 去
实现。上面的映射表显示所有维度都是 No match/Partial（且 Partial 的
两项也是"表面相似、语义不同"），没有一项达到"值得实现"的程度——继续
否决 SPLIT，但否决的理由从"能力不存在"改为"能力存在但不是这个
contract 要的能力"。

**`ADR-013` 的表述**（"Marzban 在新架构中的定位是 transport 层，健康
检查归属该层一致"）——这句话仍然被本 ADR **supersede**，理由不变：
`marzban_*` 字段形状和 Marzban 真实产品核心职责（用户/账务管理）都
指向 Accounting；ADR-013 做这个决定的**真正理由**（"`providers/
base.py` 受铁律第 5 条约束，为单个健康检查端点修改核心契约不合比例"）
本身仍然成立，和 Marzban 该归哪一类无关。

### `/admin/accounting/health` 的最终设计（本轮新增，第一版遗漏）

**第一版把这里写成"无实际收益的实现捷径，可以永久保留"，独立审查
指出这个说法不能作为最终结论——必须先说清楚：`ACCOUNTING_PROVIDER=
marzban` 且 `TRANSPORT_PROVIDER_MODE=subscription` 时，
`admin_accounting_health()` 实际检查的是 `SubscriptionTransportProvider`
（一个和 Marzban 毫无关系的、管理订阅链接节点同步的 provider），不是
Marzban 本身——这是一个真实的语义错误，不能只说"是捷径，可以保留"就
结束。**

逐项评估用户列出的候选：

- **H1（给 `AccountingProvider` 加 `health_check()`）**：现在有
  ADR-015（本 ADR）作为"先有 ADR 再改 `providers/base.py`"的依据，
  满足 `AGENTS.md` 铁律第 5 条的前置要求，可行。代价是要给
  `MockAccountingProvider` 和未来真实的 Marzban-backed
  `AccountingProvider` 实现都补上这个方法。
- **H2（新增独立的 service-health 抽象，不塞进 accounting/transport）**：
  更"干净"，但引入一个新的抽象层，对当前只有一个健康检查端点的现状
  来说，复杂度收益比不如 H1。
- **H3（改端点语义/名字，让它明确检查 transport 而不是 accounting）**：
  治标不治本——如果运维真正想知道的是"Marzban 是否健康"（从端点名字
  `/admin/accounting/health` 判断，这是当前唯一合理的解读），改名字
  检查 transport 并不能回答这个问题，只是把语义错误从"检查错了 provider"
  变成"这个端点从此不再回答它名字暗示的问题"。
- **H4（SPLIT 场景下的复用正当性）**：本 ADR 已经否决 SPLIT，H4 不适用。

**最终选择：`H1`**——给 `AccountingProvider` Protocol 新增
`health_check() -> bool`，`admin_accounting_health()` 改为调用
`build_registry(...).accounting.health_check()`。理由：这是唯一一个
真正让端点名字（"accounting health"）和它实际检查的 provider
（Marzban = accounting）对齐的选项，且现在已经有 ADR-015 满足铁律
第 5 条的前置条件，不再是"为一个端点碰 base.py 不合比例"这个此前
阻止修改的理由——**这个理由本身已经因为本 ADR 的存在而不再阻塞**。

已更正 `ADR-013` 的 supersede note：**不再写"`/admin/accounting/
health` 现有接线明确不需要修改"**，改为记录 H1 这个新决定，作为
Phase 2B implementation matrix 的一项。

## Part C：Xray route identity 收敛方案（候选评估不变，新增既有数据
收敛小节）

### 候选对比（评估维度按用户要求逐一比较，不变）

| 维度 | 候选 A：恢复 `gateway_principal` = accounting principal | 候选 B：`accounting_user_id` 持久化时机提前 | 候选 C：`sub-{order.id}` 定为正式契约 |
|---|---|---|---|
| DB 单一 source of truth | 好——`gateway_principal` 本身就是单一字段，写入值改对即可 | 好，但依赖跨编排步骤读一个字段 | 好——纯函数推导，不依赖任何持久化字段 |
| 首次开通时序 | **不受影响**——`ensure_gateway_route_binding()` 就在 `APPLY_GATEWAY`（第 7 步）内部写入，不依赖后续步骤 | 需要把 `accounting_user_id` 的写入从 `admin_confirm_payment`（编排全部返回后）提前到第 6 步之后、第 7 步之前 | **不受影响**——`order_id` 在下单时就已持久化 |
| rollback/compensation | 不增加新失败模式（沿用 `ensure_gateway_route_binding()` 现有 upsert 逻辑） | **新增**：第 7 步失败时，第 6 步已提前写入的 `accounting_user_id` 要不要回滚，需要新设计 | 不增加新失败模式（纯函数无副作用） |
| idempotency | 好——`ensure_gateway_route_binding()` 已经按 `subscription_id` upsert | 需要新增"避免重复提前写入"的判断 | 好——纯函数天然幂等 |
| retry | 简单——重新计算/重新调用即可 | 需要处理"部分提前写入后重试"的状态判断 | 简单——纯函数 |
| accounting user disable 语义 | 不受影响 | 不受影响 | 不受影响 |
| 现有 schema 复用 | **最好**——复用 `gateway_principal` 现有列，只改写入的值 | 复用 `accounting_user_id` 现有列，但改写入时机涉及跨层代码移动 | 不新增列，靠推导，等于把"字段"换成了"函数" |
| **既有数据是否需要处理** | **需要**（见下方"既有数据收敛"小节，这是本轮新增维度） | 需要（提前持久化不会自动修正历史行） | 不需要（不依赖持久化字段） |
| 未来改用户名规则的兼容性 | **好**——`gateway_principal` 一旦写入即固定，后续改命名规则不影响已渲染的历史路由 | 好——`accounting_user_id` 同理一旦写入即固定 | **差**——推导公式一旦变化，历史值和新值不一致，没有检测机制 |
| renderer 重新渲染同一订阅时能否拿到一致的值 | **好**（新行）；旧行需要收敛后才好 | 好——直接读已持久化的 `accounting_user_id`（提前之后可用） | 只要推导公式不变就一致 |
| 最小改动范围 | **最小**——一行调用参数改动 + 一次性数据收敛 | 较大——跨层挪动持久化时机 | 较小，但有长期风险 |
| 测试难度 | 较容易——现有集成测试已用 `"marzban-user-1"` 这类值调用 `ensure_gateway_route_binding()`；新增数据收敛的测试 | 较难 | 中等 |

### Decision 3（选择不变：`A`，新增前提条件）

**Selected: `A`**——恢复 `GatewayRouteBinding.gateway_principal` 的
accounting-principal 语义，`desired_routing_state()` 调用
`ensure_gateway_route_binding()` 时改传 `request.username`（而不是
`tenant.tenant_id`）。**这个选择的前提是同时执行下方"既有数据收敛"
方案，两者是同一个 Decision 3 的两个组成部分，不是"选 A 就完事"。**

**Rejected: B/C**——理由不变，见上表和"约束"一节。

### 既有数据收敛（本轮新增，第一版遗漏）

**第一版遗漏了这个问题，独立审查指出后核实确认是真实存在的缺口。**
重新核实 `SqlAlchemyProvisioningState.ensure_gateway_route_binding()`：
只有当某个 `subscription_id` **再次**进入这个方法（例如重新走一次
provisioning）时才会更新已存在的 `binding.gateway_principal`；**已经
存在、状态是 active（`released_at IS NULL`、`enabled=True`）、但从
未被本方法重新处理过的历史行，`gateway_principal` 会继续保持旧的
写入值（`tenant.tenant_id`）**，不会因为 Phase 2B 改了 writer 代码就
自动变正确。而 `ops/gateway/render_xray_routes.py::_active_routes()`
直接查询所有 `enabled=True AND released_at IS NULL` 的
`GatewayRouteBinding` 行并把 `route.gateway_principal` 当 Xray
`"user"` 匹配值使用——**这意味着 Phase 2B 只改 writer，不处理历史行，
会让已有的活跃订阅的 Xray 路由继续匹配错误的值**，这是一个会影响真实
（未来会被真正渲染的）路由正确性的问题，不能只归为"schema 层面不需要
migration"就略过。

必须把 Decision 4 里"migration"拆成两个独立问题：

- **Schema change**：`NO`——不新增表/列，`gateway_principal` 是已有
  字段。
- **既有数据 reconciliation**：**`YES`，需要独立方案**。

**选定方案**：**A. data-only Alembic reconciliation migration**
（`AGENTS.md` 铁律第 7 条已经确立的"schema/代码不一致只能新增
reconciliation migration 补救，且必须幂等"这个模式，直接适用于这里的
数据不一致场景）。具体设计要点（Phase 2B 实现时展开，这里只定方向和
安全约束）：

1. 只处理 `GatewayRouteBinding.enabled = True AND released_at IS
   NULL`（即 `active_gateway_principal` 非空）的行。
2. 通过 `subscription_id` JOIN `Subscription`，只处理
   `Subscription.accounting_user_id IS NOT NULL` 的行——如果关联的
   `Subscription.accounting_user_id` 还是 `NULL`（理论上不应该出现在
   已经是 active binding 的订阅上，但要 fail-closed 处理，不假设），
   跳过并记录，不猜测/不生成一个值。
3. 只更新 `gateway_principal != accounting_user_id` 的行（幂等：
   重复跑这个 migration 不会对已经等于目标值的行做无意义的写入）。
4. **必须处理 `active_gateway_principal` 这个计算列的唯一约束**
   （`uq_gateway_route_active_principal`）：如果 reconciliation 会导致
   两条 active 行的目标 `gateway_principal` 撞车（正常情况下不应该
   发生，因为一个 subscription 只应该有一条 active binding，一个
   `accounting_user_id` 也只属于一个 subscription），必须在 migration
   里显式检测这种情况并 **fail closed（中止 migration，报告冲突的
   行，不做部分更新）**，不允许静默让 UNIQUE 约束报错中断在未知的
   中间状态，也不允许为了绕过约束而先删后插。
5. migration 必须幂等（重复执行结果一致），符合 `AGENTS.md` 铁律
   第 7 条对 reconciliation migration 的要求。

**为什么不选 B（一次性脚本/job）**：Alembic migration 是本仓库既有的、
`AGENTS.md` 已经背书的"reconciliation"标准路径，天然带有版本追踪和
可重复执行的保证；额外写一个独立脚本/job 只是重新发明这套已有机制，
没有必要。

**为什么不选 C（renderer 直接从 `Subscription.accounting_user_id`
读，不依赖 `gateway_principal`）**：这个方案确实可以绕开"历史行没有
被收敛"的问题（因为不读那一列），但会让候选 A 里"恢复
`gateway_principal` 的 accounting-principal 语义"这个模型契约修复
变得没有意义——如果 renderer 根本不读这一列，`gateway_principal`
存的是对是错就不再重要，等于放弃了修复模型/测试契约不一致这个目标，
只是把"路由匹配键从哪来"这个问题转移给了另一个字段。候选 A 选择
"修好 `gateway_principal` 本身"是因为这个字段的模型契约本来就该是
accounting principal，选 C 相当于承认这个字段的契约不值得维护，
两者目标不同，不能互相替代。

**为什么不选 D（部署前置检查证明当前数据库没有旧 active 行）**：
这依赖对当前生产数据库实际内容的现场核实，本 ADR 是 docs-only 只读
研究阶段，不能也不应该去读生产数据库来"证明"这件事；即使某次检查
证明了"当前没有"，未来只要 Phase 2B 上线前有任何一笔真实开通流程
跑过（包括测试/staging 环境如果共享同一套迁移历史），这个前提就可能
被打破——一次性检查不是可靠的长期保证，reconciliation migration 是
更稳妥的方案。

## Reality ownership：无 NEW EVIDENCE，维持 ADR-014 结论

本次研究过程中没有发现任何推翻 ADR-014 已 Accepted 的 Reality
ownership 结论（inbound protocol/listen/port → 静态模板；Reality
`dest`/`serverNames` → 环境变量；Reality `privateKey`/`shortIds` →
DB/Secret；routing/outbounds/route identity → DB；运行时 config →
只读校验/回滚/漂移来源）的证据，本 ADR 不重新打开这些问题。

## Decision 4：Phase 2B implementation matrix（已按本轮修正拆分为
更细的条目）

| 改动类型 | 需要？ | 说明 |
|---|---|---|
| `DesiredRoutingState` DTO change | **YES** | 扩展以表达完整 outbound 连接细节；route identity 部分不需要新增字段 |
| Settings change | **YES** | `XRAY_REALITY_DEST`/`XRAY_REALITY_SERVER_NAME` 读取路径（ADR-014 已定方向） |
| Secret persistence change | **YES** | Reality `privateKey`/`shortIds` 走 `Secret` 表（ADR-014 已定方向） |
| DB schema change | **NO** | `gateway_principal`/`accounting_user_id` 都是已有列 |
| **既有数据 reconciliation**（本轮新增独立条目） | **YES** | Alembic data-only reconciliation migration，见上方"既有数据收敛"小节的完整设计要点，含 unique constraint 冲突的 fail-closed 处理 |
| provisioning writer change | **YES，范围很小** | `desired_routing_state()` 里 `ensure_gateway_route_binding()` 的调用参数从 `tenant.tenant_id` 改为 `request.username` |
| **accounting health contract/API**（本轮新增独立条目） | **YES** | 给 `AccountingProvider` 新增 `health_check()`（Option H1），`admin_accounting_health()` 改为调用 `accounting.health_check()`；`MockAccountingProvider` 和未来的真实 Marzban adapter 都要实现 |
| query/adapter change | **YES** | 期望态查询参照 `render_xray_routes.py::_active_routes()` 的 JOIN 逻辑扩展到 provider 抽象层 |
| Xray renderer change | **YES** | 产出完整 outbound；Reality `dest`/`serverNames` 改为只读 env |
| preservation/validation change | **YES** | 按 ADR-014"合法删除语义"重写；保留 Marzban 协议匹配约束（Part A） |
| **共享 `XRAY_JSON` writer guard / drift policy**（本轮新增独立条目） | **YES** | 本仓库 renderer 是唯一合法 writer；本应用不得调用 Marzban `PUT /api/core/config`；需要 drift detection（renderer 运行前比对磁盘当前内容与预期上一次写入是否一致），检测到漂移 fail closed + 告警，不静默覆盖 |
| tests | **YES** | 覆盖：route identity 收敛后的契约（`gateway_principal == request.username`）、reconciliation migration 的幂等性和 unique-constraint 冲突场景、`accounting.health_check()` 契约、drift-detection 的 fail-closed 行为 |

## Phase 2B implementation handoff（严格收窄，不变）

只实现上表全部标 `YES` 的条目——DB → desired-state contract、Xray
renderer/测试护栏、既有数据 reconciliation、accounting health 契约、
共享文件 writer guard。**不包括**：`registry.py` 的真实 opt-in
wiring、生产环境启用、真实生产凭据、真实 Xray reload、部署——这些
仍然属于 Phase 2C。

## 约束

1. 本 ADR 不实现任何代码、schema、迁移改动——纯粹是研究结论和决策
   记录。
2. Marzban v0.8.4 相关的全部结论均标注 `CONFIRMED`/`INFERENCE`，附带
   具体源码文件/函数/API 路径证据；没有任何一项凭产品常识猜测；本轮
   修正的四处错误全部标注了"第一版错误"并给出改正后的证据。
3. 本 ADR **supersede** `ADR-013` 里"Marzban 在新架构中的定位是
   transport 层，健康检查归属该层一致"这一句具体表述；`ADR-013` 的
   supersede note 需要同步更新为记录 H1（给 `AccountingProvider` 加
   `health_check()`）这个新决定，**不能再写"现有接线明确不需要修改"**
   ——这条表述已被本轮修正推翻。
4. Decision 3（候选 A）在 Phase 2B 实现时必须同时包含：①一行 writer
   调用参数改动；②既有数据 reconciliation migration——两者是同一个
   决策的两个组成部分，缺一不可，不得只做①就视为完成 Decision 3。
5. Part A 发现的两条约束都必须写进 Phase 2B 的验收标准：①renderer
   产出的 inbound tag 不能破坏 Marzban 协议匹配；②本仓库 renderer 是
   共享 `XRAY_JSON` 唯一合法 writer，需要 drift detection 保护这个
   边界。
6. Decision 2 新增的 accounting health 决定（H1）必须写进 Phase 2B
   实现范围，不能停留在"以后再说"。

## 考虑过的替代方案

1. **继续把 Marzban 归为 transport（维持 ADR-013 原表述）**：与
   `marzban_*` 配置字段形状和 Marzban 真实产品职责证据矛盾，否决。
2. **Marzban SPLIT 成两个 adapter**：Marzban 确实有节点管理能力，但
   映射表显示这些能力属于不同 bounded context（Marzban 自己的 Xray
   执行拓扑，不是上游代理库存），不符合 `TransportProvider` 契约，
   否决（否决理由已按本轮修正更新，不再是"能力不存在"）。
3. **候选 B（提前 `accounting_user_id` 持久化时机）**：可行但引入不
   必要的编排/事务复杂度，否决。
4. **候选 C（`sub-{order.id}` 正式契约化）**：把路由正确性长期绑定在
   一个字符串推导公式永不改变的假设上，否决。
5. **既有数据方案 B（一次性脚本/job）**：重新发明已有的 reconciliation
   migration 机制，否决。
6. **既有数据方案 C（renderer 改读 `Subscription.accounting_user_id`，
   不依赖 `gateway_principal`）**：会让候选 A 修复模型契约这个目标
   失去意义，否决。
7. **既有数据方案 D（部署前置检查证明无旧数据）**：一次性检查不构成
   长期保证，且本 ADR 阶段不应读生产数据库来验证，否决。
8. **允许"repo renderer 任意覆盖，Marzban 只是 reader"（第一版隐含的
   立场）**：被 Part A 的更正推翻——Marzban 的 core-config 管理 API
   确实能写这个文件，继续假设它只是 reader 会产生未被检测的双 writer
   竞态，否决，改为"唯一合法 writer + drift detection"的更谨慎模型。

## 安全影响

- Part A 更正后的发现（Marzban 的 core-config 管理 API 会写共享
  文件）是一个此前未被识别的真实风险：如果运维习惯性通过 Marzban UI
  修改 core 配置，本仓库 renderer 下一次运行会静默覆盖运维的改动，
  或者运维的改动会覆盖 renderer 的期望态输出——**两个方向都可能导致
  配置意外偏离数据库期望态**，这正是需要 drift detection 的直接原因，
  优先级应视为 Phase 2B 的一部分，不是可以延后的次要项。
- Decision 3 选定候选 A 之后，`GatewayRouteBinding.gateway_principal`
  会开始存储 accounting username（此前存储的是 Webshare 出口租户
  ID）——如果不做既有数据 reconciliation，会造成"新开通的订阅路由
  正确、旧订阅路由错误"这种不一致状态，且没有任何自动检测机制会
  发现它，直到某个客户报告连不上——这是本轮新增 reconciliation
  migration 决定的直接动机。
- accounting health 决定（H1）修正后，`/admin/accounting/health`
  会真正检查 Marzban（而不是无关的 subscription transport）——在此
  之前，这个端点给运维的信号是误导性的（显示"健康"可能只是因为
  subscription transport 健康，Marzban 本身可能已经故障），这是一个
  被本轮修正之前一直存在、未被识别的运维可观测性缺口。

## 重新评估条件

当 Phase 2B 实际实现候选 A 时，如果发现 `GatewayRouteBinding.
gateway_principal` 在其它未被本次研究覆盖的代码路径里被依赖为
"Webshare 出口租户 ID"这个旧语义，需要重新评估候选 A 的改动范围和
reconciliation migration 的影响范围。如果 Phase 2B 实现 drift
detection 时发现 Marzban 的 `PUT /api/core/config` 在实际部署里从未
被运维使用过（例如权限层面已经天然不可达），可以相应降低 drift
detection 的实现优先级，但仍然应该保留检测能力，不应该假设"未来也
永远不会被调用"。
