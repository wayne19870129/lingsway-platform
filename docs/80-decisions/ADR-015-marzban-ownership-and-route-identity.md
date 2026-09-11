# ADR-015: 关闭 Phase 2B 前置决策——Marzban 运行时归属、provider 分类、
route identity 收敛方案

> **2026-09-11 补记**：本 ADR 的 **Decision 3**（route identity 收敛
> 方案，`BLOCKED`）及其"既有数据收敛"结论已被
> `docs/80-decisions/ADR-016-route-identity-architecture-unblock.md`
> **supersede**——ADR-016 选定 Candidate B（Marzban `UserResponse`
> 补丁暴露 `routing_principal` 字段），把 Decision 3 从 `BLOCKED`
> 推进为 `SELECTED`。本 ADR 的 Part A（drift detection 三态模型/
> 共享文件 writer guard）、Part B（provider classification =
> `ACCOUNTING`、accounting health = H1）不受影响，继续有效。

- 状态: 已接受（Part C 的 Decision 3/既有数据收敛截至第三次修订为
  `BLOCKED`，见下方——本 ADR 整体状态仍是"已接受"，指"这些研究结论
  已被接受为当前权威记录"，不代表 Decision 3 已经有一个可以直接实现
  的答案）
- 日期: 2026-09-11（2026-09-11 第二次修订：独立审查指出并核实了四处
  事实错误——Marzban 确实会在特定管理 API 路径下写回 `XRAY_JSON`、
  普通用户 CRUD 走增量 Handler API 而非"每次都 include_db_users()+
  重启"、Marzban 确实有真实的远端节点管理能力（此前"完全没有"的说法
  错误）、候选 A 缺少既有数据的收敛方案——已全部修正，见下方各节。
  2026-09-11 第三次修订：独立审查针对 head `79032282a62d952f478631f
  ff3a37ec8a8f2404d` 提出三个新 Major——(1) Decision 3 选定的
  `gateway_principal = request.username` 用错了 Xray 实际匹配的
  identity：精确核实 Marzban v0.8.4 源码后确认 Xray 侧真正匹配的
  client email 是 `f"{marzban_db_user_id}.{username}"`，不是纯
  accounting username，且 Marzban 公开 API（`UserResponse` 家族）不
  暴露 DB `id`，Decision 3 及既有数据收敛方案在未解决这个问题前不能
  维持"Selected: A"的结论，本轮改为 `BLOCKED`；(2) 既有数据收敛的
  NULL/未匹配行处理语言存在"fail closed"与"跳过并记录"的自相矛盾，
  本轮改为统一的"全量 preflight → 检测到任何异常即整体中止"模型；
  (3) Decision 1 的 drift detection 算法把"磁盘当前状态 vs DB 期望态"
  当作漂移判定，会把正常的业务驱动配置变更误判为漂移，本轮引入
  `last_applied_state`/`current_disk_state`/`new_db_desired_state`
  三态模型并重新定义比对逻辑。以上三处新发现全部记入下方对应小节，
  第一版/第二版内容除被明确替换的部分外保持不动。2026-09-11 第三次
  修订第二轮：针对 head `0d4b1aaf1cc4cbeb83ce57bee714a596cf5dee4b`
  的独立审查又提出三个新 Major——(1) drift baseline 的 fingerprint
  scope 只覆盖 `outbounds`/`routing`，漏掉 Marzban `PUT
  /api/core/config` 能写的 `inbounds`/Reality 等其它字段，本轮改为
  覆盖整份 repo-owned 配置对象并要求显式 include/exclude 清单；(2)
  基线提交时机没有对齐 `XrayFileProvider.apply()` 真实的九步
  backup/validate/install/reload/health/rollback 流程，本轮改为只在
  `ApplyResult(True, ...)` 成功返回后提交，并补齐 rollback 成功/
  rollback 自身失败/baseline 持久化失败三种场景各自的处理规则；(3)
  `ADR-014` 第 5 条把"选定 route identity 收敛方向"设为**整个** Phase
  2B 的前置门槛，与本 ADR 第三次修订第一轮"Decision 3 BLOCKED 期间
  仍可以先做部分 Phase 2B"的结论直接冲突且未显式声明 supersede，本轮
  采纳独立审查建议的更保守路径：不 supersede ADR-014 第 5 条，
  Decision 3 解锁之前不启动任何 Phase 2B 代码实现，下一步改为专门
  解决 route identity 的任务。同时一并处理一条 Minor：PR 标题需要
  反映"记录了一个 BLOCKED"而不是"关闭了所有前置决策"。2026-09-11
  第三次修订第三轮：针对 head
  `fd74f4e047e9286aa327242fadbab99990ddb7fd` 的独立审查指出，第二轮把
  `XrayFileProvider.apply()` 描述成"任何失败都完整走
  `restore(backup)` 回滚"，这个描述和当前代码/测试的真实行为不符——
  `install()`/第一次 `reload()` 周围没有 `try/except`，两者抛异常会
  绕过回滚路径直接传播，现有 `test_safe_reload.py` 也没有覆盖这两种
  异常场景。本轮据实更正 ADR 对当前代码行为的描述，新增"场景 D"
  （异常绕过回滚、状态未知，必须 fail closed 处理），并把"加固
  `apply()` 捕获这两处异常"列为 Phase 2B 的必需前提，同时要求补齐
  对应的护栏测试。2026-09-11 第三次修订第四轮：独立审查确认第三轮的
  分类和修复方向正确（`VALID`），但要求把"场景 D"更精确命名为
  "pre-health mutating exception"、显式区分它和"场景 C"（apply 成功
  之后基线持久化失败）分别发生在 apply 流程的哪个时间点、并把未来
  Phase 2B 护栏测试的要求从 2 类扩展到 4 类（`install()` 异常、首次
  `reload()` 异常、rollback `restore()` 异常、rollback 第二次
  `reload()`/健康复核失败），逐条采纳并写入下方对应小节，不涉及
  Decision 3/Phase 2B gate 等已确认结论的重新讨论。）
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
  **drift detection**——**第三次修订：第二版这里的算法是错的，本轮
  重写，见下方"drift detection 三态模型"小节**。第二版把"磁盘当前
  状态 vs 数据库当前期望态"的任何差异都称为"漂移"，这个算法会把
  **正常的、由业务变更驱动的配置更新**（例如新增一个订阅、一次正常
  的 renderer 重新渲染）也误判成"漂移"并 fail closed——这会导致
  renderer 在完全正常的业务场景下无法工作，是一个算法层面的错误，
  不是措辞问题。

#### drift detection 三态模型（第三次修订新增，替换上一段被推翻的
算法）

必须区分三个不同的状态，而不是只比较"磁盘现状"和"DB 现状"两个值：

- **(A) `last_applied_state`**——renderer **上一次自己成功写入磁盘
  时**的配置内容（或其指纹/hash），代表"上一次被这个仓库自己确认过
  的、合法的磁盘状态"。
- **(B) `current_disk_state`**——现在读取磁盘上 `xray_config.json`
  的实际内容（或其指纹/hash）。**（第三次修订的第二轮独立审查指出
  并核实：这里的 scope 定义有严重缺口，第一稿把它写成只覆盖
  `outbounds`/`routing`，本轮更正为下方"保护范围（第三次修订第二轮
  更正）"一节的定义。**
- **(C) `new_db_desired_state`**——现在从数据库计算出的、renderer
  即将渲染的新期望态（或其指纹/hash）。

#### 保护范围（第三次修订第二轮更正，修复本轮 Major 1）

**第一稿的错误**：把 `current_disk_state`/`last_applied_state` 都
定义成"只包含 `outbounds`/`routing` 部分"。这个 scope 太窄——Marzban
的 `PUT /api/core/config` 写的是**整个 payload**，不是只写
`outbounds`/`routing`。如果一次未授权写入只改了 `inbounds`
（协议/`listen`/端口）或 Reality 相关字段，而没有改 `outbounds`/
`routing`，按第一稿的 scope，`current_disk_state == last_applied_
state` 仍然成立，drift 检测会报告"无漂移"——**恰好漏掉这个机制本来
就是为了检测的那种双 writer 场景**，这是一个会让 drift detection
名不副实的真实缺口。

**更正后的定义**：`current_disk_state`/`last_applied_state` 必须覆盖
**整个本仓库拥有的、写入共享 Xray 配置文件的对象**（不只是
`outbounds`/`routing`，也包括 `inbounds`（协议/`listen`/端口/
`settings.clients` 骨架部分——不含 Marzban 通过 gRPC Handler API 增量
维护的运行时 client 列表，那部分活在 Xray 进程内存里，不落盘，见 Part
A 第 6 点）、Reality 相关字段、以及 renderer 实际写入的其它任何顶层
字段），逐字段核对/白名单化，而不是只挑两个字段比较。**如果未来需要
排除某个字段不纳入指纹**（例如某个已知会被 Marzban 运行时正常改写、
不代表未授权修改的字段），必须显式列出该字段并写明排除理由，不能
默认排除任何字段。
- **canonicalization**：`current_disk_state`/`last_applied_state`
  的指纹计算必须用同一套确定性序列化规则（例如递归排序 key 后再
  hash），格式/空白/key 顺序的差异不应该被判定为漂移——只有语义内容
  的差异才算。
- **无法解析时 fail closed**：如果磁盘上的文件不是合法 JSON（无法
  解析），不能当作"和上次一样"处理，必须视为一种漂移信号并 fail
  closed。
- **一致性要求**：基线（`last_applied_state`）和当前磁盘读取
  （`current_disk_state`）必须使用**完全相同**的 canonicalization
  scope 和序列化规则计算指纹，否则两者不可比较。

**正确的比对逻辑**：

1. `current_disk_state != last_applied_state` → **未授权/未知的漂移**
   ——说明磁盘文件在 renderer 不知情的情况下被别的写入者改动过（例如
   运维通过 Marzban `PUT /api/core/config` 写过），**fail closed 并
   明确告警，不覆盖**。
2. `new_db_desired_state != last_applied_state` **且** `current_disk_
   state == last_applied_state` → **正常的、待处理的业务变更**——
   磁盘和上一次 renderer 自己写入的内容一致，说明没有被别人动过，
   现在只是数据库期望态本身发生了变化（新订阅/新路由），renderer
   应该生成新的候选配置并正常校验、正常写入。**不应该被当作"漂移"
   拒绝。**
3. 两者都相等 → 无变化，renderer 可以是 no-op。

第二版的算法本质上混淆了 1 和 2——它把"磁盘 vs DB 期望态"的比较结果
当成了"磁盘 vs 上一次合法写入"的比较结果，而 (C) 几乎总是会和 (B)
不同（因为 DB 期望态本来就会随业务变化），导致第二版算法在几乎所有
正常场景下都会误报漂移。

#### last-applied 基线的持久化位置（第三次修订新增）

独立审查要求先盘点现有的 `TransportVersion`/`EgressVersion` 等
config/version/snapshot/hash 类表，判断能否复用，而不是因为字段形状
像（都有 `config_hash`）就直接复用。核实 `backend/app/models/
gateway.py`：

- `TransportVersion`/`EgressVersion` 均有 `config_hash`/
  `snapshot_json`/`generated_at`/`generated_by`/`reason`/
  `previous_version_id` 字段，按 `(route_group_id, version_no)`
  唯一约束。
- **`CONFIRMED`（grep 核实）**：这两个模型在 `gateway.py` 定义之外
  **没有任何代码引用**——完全未被使用。
- **bounded context 不匹配**：两者都按 `route_group_id` 组织（一个
  Mihomo/egress 路由分组的概念），而本 ADR 需要的基线是"**一份共享
  的 Xray gateway 配置文件**"这个单一对象的上一次合法写入状态，不是
  按 route group 分片的版本历史。把一个"Xray 单文件的写入基线"塞进
  一个为"Mihomo route group 版本历史"设计的表，是和 Part B 里"因为
  方法名字像就把 Marzban Node 塞进 TransportProvider"同一类型的错误
  硬映射，不应该重复。

**结论**：`TransportVersion`/`EgressVersion` **不是**合适的复用对象。
本 ADR 认为 Phase 2B 需要为"Xray 共享配置文件的 last-applied 基线"
新增一个**独立的、职责单一的持久化位置**（具体是新表还是在现有的
某个单例配置记录上加两个字段——`last_applied_config_hash`/
`last_applied_at`——留给 Phase 2B 实现阶段的详细设计，本 ADR 只确定
方向："不复用 `TransportVersion`/`EgressVersion`，需要一个新的、
scope 限定为这一份共享文件的持久化机制"）。**这意味着 Decision 4 的
"DB schema change"不能再统一写"NO"**——drift detection 这一项本身
就需要新的持久化，即使 Decision 3 的部分不需要 schema 变更。

#### bootstrap（首次运行）行为（第三次修订新增）

- **无基线时（首次运行，`last_applied_state` 不存在）**：不能假设
  `current_disk_state == last_applied_state` 成立（这会把"从未记录
  过基线"和"确认过没有漂移"混为一谈）。正确行为：**如果磁盘上已经
  存在一个文件，且这是第一次引入基线机制，需要一次显式的、有意的
  "采纳当前磁盘状态为初始基线"操作**（记录下来是谁在什么时间做的
  这个决定，而不是代码自动静默采纳），而不是把"没有基线"直接当成
  "无漂移"处理。
- **文件缺失时**：视为需要 renderer 正常渲染并写入（不是漂移，因为
  没有"被别人动过"这件事可比较），写入后建立新的 `last_applied_
  state`。
- **基线更新时机（第三次修订第二轮更正，修复第三次修订第二轮 Major
  2；第三次修订第三轮再次更正，修复本轮 Major 1）**：
  **第二轮的错误尚未完全更正**：第二轮把 `apply()` 描述成"任何失败都
  会进入 `restore(backup)` 回滚流程"，暗示这是一个完整的九步安全
  reload/rollback 保证。**独立审查第三轮重新核实
  `backend/app/providers/gateway/xray_file.py::XrayFileProvider.
  apply()` 和 `backend/tests/guards/test_safe_reload.py` 的现有实现/
  测试覆盖后指出，这个描述和当前代码的真实行为不符，本轮据实更正**：

  真实 `apply()` 的代码结构是：

  ```python
  backup = self._runtime.backup()
  validation = self.validate(candidate, new_username=new_username)
  if not validation.valid:
      raise XrayValidationError(...)

  self._runtime.install(candidate.content)   # 没有 try/except 包裹
  self._runtime.reload()                     # 没有 try/except 包裹
  report = self._runtime.health()
  post_reload_errors = _preservation_errors(...)
  if report.healthy and not post_reload_errors:
      return ApplyResult(True, candidate.version)
  # 只有走到这里（health/preservation 检查"返回"不健康，而不是抛异常）
  # 才会进入 restore(backup) 回滚流程
  ```

  **`install()` 和第一次 `reload()` 周围没有 `try/except`**——
  `LocalXrayRuntime.reload()` 用 `subprocess.run(reload_command,
  check=True)`，reload 命令失败会直接抛
  `subprocess.CalledProcessError`；`install()` 内部的文件写入/
  `Path.replace()` 也可能抛异常。**这两处异常都会直接从 `apply()`
  向外传播，完全绕过 `restore(backup)` 回滚路径**——现有的
  `test_safe_reload.py` 只覆盖了"校验失败在 reload 之前被拒绝"和
  "reload 成功、但 post-reload health **返回** `False`"这两种情况，
  没有对 `install()`/第一次 `reload()` **抛异常**的场景做任何测试，
  说明这确实是当前实现的一个真实空白，不是文档层面的措辞问题。

  **更正**：本 ADR 不再把当前 `XrayFileProvider.apply()` 描述为"对
  每一种 apply 失败都提供完整的九步回滚保证"——这不是当前代码的真实
  行为，只对"reload 成功后，health/preservation 检查以返回值形式报告
  不健康"这一种失败模式成立。`install()`/第一次 `reload()` 抛异常是
  一类需要单独承认的失败——这种情况下磁盘可能已经被换成了新
  candidate（如果是 `install()` 之后、`reload()` 抛异常），但 Xray
  运行时是否真的在跑这份新配置是未知的（reload 命令失败通常意味着
  没有成功生效，但也可能是部分生效），而磁盘的旧内容已经不在原地，
  且**没有任何 `restore()` 被调用**——这是一个第二轮"场景 A/B"矩阵
  没有覆盖的、真实存在于当前代码里的第三种状态，本轮补上为"场景 D"
  （第三次修订第四轮更正命名：这一类失败的准确描述是 **"pre-health
  mutating exception"**——已经 `backup()` 完成、随后 `install()`/
  第一次 `reload()` 阶段发生异常，且无法证明磁盘/runtime 此时仍处于
  旧的 known-good 状态；"场景 D"这个编号继续沿用，只是补充这个更精确
  的名字，避免和下方 Decision 4"既有数据 reconciliation"矩阵或
  Part C 的候选 A/B/C 混淆）。**这个场景和"场景 C"（`ApplyResult
  (True, ...)` 已经产出之后、基线持久化本身失败）是两个不同的失败
  时间点，不能混为一谈**：场景 C 发生在 apply 成功**之后**（Xray
  运行时已经确认应用了新 candidate，只是基线记录没跟上）；场景 D
  （pre-health mutating exception）发生在 apply **尚未**产出任何结果
  之前（`ApplyResult(True, ...)` 从未被返回，异常直接终止了整个
  `apply()` 调用）。

  **`last_applied_state` 的更新规则不变**：仍然表示 **last known-good
  successfully applied config**，不是 **last file write attempt**，
  只应该在 `apply()` 返回 `ApplyResult(True, ...)` 之后才提交更新——
  场景 D 下 `apply()` 根本不会正常返回（异常直接向外传播），所以基线
  同样不会被推进，这一点结论不变；但下方矩阵需要新增场景 D，明确这
  不是"和场景 A/B 一样安全"的失败，而是一个**当前实现下未被兜底**的
  状态，Phase 2B 必须先修复这个代码缺口，而不是假设它已经被现有
  `apply()` 处理好了。

  **Phase 2B 必须做的加固**（本轮新增要求）：

  1. 把 `install()` 和第一次 `reload()` 也纳入失败处理路径——任何在
     `backup()` 之后、且已经可能改变磁盘/运行时状态的操作抛出异常，
     都必须被捕获，不能让异常未经处理直接从 `apply()` 传播出去。
  2. 捕获后必须尝试走等价于场景 A/B 的路径：能确认成功
     `restore(backup)`+`reload()`+复核健康，则按场景 A 处理（基线
     不推进，保持旧值）；如果这个恢复尝试本身也失败或无法确认磁盘/
     运行时回到已知良好状态，必须按场景 B 处理（基线标记未知/
     degraded，fail closed，不能静默假装恢复成功）。
  3. **（第三次修订第四轮扩展）** 必须新增以下四类护栏测试，纳入未来
     Phase 2B 的验收标准——不能只依赖现有覆盖"health 返回 False"这
     一种失败模式的测试：
     - **`install()` 抛异常测试**——验证 `apply()` 能捕获这个异常并
       走场景 A/B 路径，而不是让异常直接传播出去。
     - **第一次 `reload()` 抛异常测试**——同上，覆盖
       `LocalXrayRuntime.reload()` 的 `subprocess.run(...,
       check=True)` 失败时 `apply()` 的处理方式。
     - **rollback 阶段 `restore()` 本身抛异常测试**——对应场景 B 的
       "`restore(backup)` 本身失败"这个分支，验证此时基线被正确标记
       为未知/degraded 并 fail closed，而不是假装恢复成功。
     - **rollback 阶段第二次 `reload()`/健康复核失败测试**——对应
       场景 B 的另一个分支（`restore()` 复制文件成功，但 rollback
       reload 命令异常或 `rollback_reverified` 健康检查返回不健康），
       同样验证 fail closed，不假设旧配置已经确认生效。
     以上四类测试都必须验证同一个不变量：**不允许出现"磁盘已经是新
     candidate 内容 + 基线仍是旧值 + runtime 是否成功不确定"这种
     组合被静默当作正常状态**——尤其是"第一次 `reload()` 抛异常"这
     一类，必须证明测试能验证 `apply()` 不会把这种组合遗留下来且不
     报警。

  **rollback/baseline 矩阵（本轮按独立审查要求逐场景明确，第三轮
  新增场景 D）**：

  - **场景 A：candidate apply 后（reload/health/preservation 任一）
    失败，rollback（`restore(backup)`+`reload()`）成功**——磁盘已经
    恢复到 apply 之前的已知良好配置。`last_applied_state` **保持
    apply 之前的旧基线不变，不推进到失败的 candidate**——因为整个
    流程从未成功产出一个 `ApplyResult(True, ...)`，本来就不该被推进；
    这也不需要"回滚基线"这个动作，只是"从未更新过"。
  - **场景 B：`restore(backup)` 本身把文件复制回去了，但随后的
    `reload()`/`rollback_reverified` 健康检查失败**——系统这时已经
    不能再声称处于"已知良好"状态：既不能确认磁盘上的旧配置正在被
    Xray 正确运行（reload/health 都可能已经出问题），也不该假装
    rollback 完全成功。**必须 fail closed**：把 `last_applied_state`
    标记为**未知/degraded**（不是简单"保持旧值"，因为旧值所代表的
    "已生效"状态本身现在也存疑），下一次 drift 检测在基线未知时必须
    直接 fail closed 并告警，要求人工介入确认真实运行状态，不能假设
    磁盘或运行时任一侧当前是可信的。
  - **场景 C：Xray 侧已经成功走完 apply()（即已经产出
    `ApplyResult(True, ...)`），但把这个结果落成 `last_applied_state`
    持久化记录（写数据库/持久化存储）这一步本身失败**（例如 DB 写入
    异常）——这时 Xray 运行时的真实状态是"已经应用了新 candidate"
    （场景描述里的"B"，指运行时已经生效的新状态，不要与上面 drift
    矩阵的场景 B 混淆），但基线记录还停留在旧值，两者不一致。**不允许
    对外继续同时声称"baseline=旧值"且"runtime=新值"是正常状态**——
    必须二选一并明确记录选哪个：①**立即把 Xray 运行时回滚回旧配置**
    （调用等价于 `restore(backup)`+`reload()`+health 复核的路径），
    让运行时状态与仍然有效的旧基线重新一致；②**接受运行时已经是新
    状态，把系统标记为 unknown/fail-closed**，直到基线持久化重试
    成功、或人工确认并手动修复基线记录为止，期间 drift 检测必须
    fail closed，不能静默假设基线迟早会追上。本 ADR 不替 Phase 2B
    选定①或②，但要求 Phase 2B 实现时必须明确选一个并写进代码/文档，
    不能把这个不一致状态放任不处理。
  - **场景 D（第三次修订第三轮新增）：`install()` 或第一次 `reload()`
    抛出异常，异常未被捕获、直接从 `apply()` 传播出去，`restore()`
    从未被调用**——这是当前 `XrayFileProvider.apply()` 实现里真实
    存在、尚未被兜底的一种失败模式（见上方代码结构分析）。这种情况下
    磁盘/运行时可能处于三者之一：①`install()` 异常发生在写盘完成
    之前，磁盘仍是旧内容，但 Xray 进程从未 reload，状态其实接近"什么
    都没发生"；②`install()` 成功写盘、`reload()` 命令异常，磁盘已经
    是新 candidate，但 Xray 进程是否真的加载了这份新配置不确定；
    ③以上两种之外的其它异常时序。**因为这三种子情况在异常发生的
    当下无法可靠区分，必须统一按"未知"处理**：`last_applied_state`
    **不推进**（`apply()` 没有正常返回，规则和场景 A/B 一样不推进），
    且必须**同时**把这次失败标记为需要人工介入的 fail-closed 状态
    （不能简单等同于场景 A"安全地什么都没发生"），因为和场景 A 不同，
    场景 D 下磁盘内容有没有变、Xray 有没有重新加载都是不确定的，
    不能假设"没有走到 restore 就等于没有风险"。Phase 2B 必须先做到
    "上方加固要求"里的第 1/2 条，把场景 D 尽量收窄/转化为场景 A 或 B，
    而不是长期依赖这个未兜底的异常传播路径。

  如果 Phase 2B 决定这里的语义特意只是"renderer 上一次写盘的字面
  内容"而不是"上一次成功 apply 的配置"，必须明确改名以避免歧义，并
  单独写清楚它在上述四个场景下的行为，不能含糊地沿用"last applied"
  这个已经暗示"已生效"语义的名字。
- **基线的定位**：`last_applied_state` **只是完整性元数据（用于
  检测"有没有被意外改动"），永远不是配置数据的 source of truth**——
  真正的期望态 source of truth 仍然是数据库（ADR-014 已确立），
  基线只用于第 1 步的漂移判定，不能被误用为"渲染时的输入数据源"。
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

## Part C：Xray route identity 收敛方案（第三次修订：Decision 3 及既有
数据收敛改为 `BLOCKED`，候选对比表作为历史记录保留）

### 三个必须区分的概念（第三次修订新增，此前所有版本都把这三者混为
一谈）

独立审查要求先把以下三个不同的值分开定义，本轮逐一核实：

1. **Lingsway accounting username**——本仓库自己的账务系统用户名，
   例如 `request.username`（形如 `"sub-123"`）。这是 Decision 3
   第二版选定的 `gateway_principal` 写入值。
2. **Marzban 内部 DB user id**——Marzban 自己数据库里 `User` 记录的
   自增主键（`db_models.User.id`），例如 `42`。这个值**完全是
   Marzban 内部实现细节**，本仓库从未持久化过它。
3. **Xray routing principal / client email**——Xray Core 运行时用来
   匹配 `routing.rules[].user` 规则的那个字符串，也就是 Marzban 传给
   Xray 的 client 对象的 `email` 字段。

### Exact-source 发现（第三次修订新增）：(2) 和 (3) 不等于 (1)

**`CONFIRMED`**——核实精确 commit `7f396db3e703d71a28060bc9ce4a532
ec64cb1f4` 下的两条独立代码路径，Marzban 构造 Xray client `email`
字段时使用的都是复合值，不是纯 accounting username：

- `app/xray/config.py::include_db_users()`（Path A，全量重建路径）：
  为每个 DB 用户构造 client 时使用 `"email": f"{user_id}.{username}"`
  （`user_id` 是 Marzban DB 里的 `User.id`，`username` 是 accounting
  username）。
- `app/xray/operations.py`（Path B/C，增量 Handler API 路径）：
  `add_user()`/`update_user()`/`remove_user()` 构造/引用的 `account`/
  `email` 值同样是 `email = f"{dbuser.id}.{dbuser.username}"`。

即：Marzban 传给 Xray、Xray 实际用于路由匹配的 client 标识，形如
`"42.sub-123"`，**不是** `"sub-123"`。

**`CONFIRMED`（一般行为，非本次逐行核实的具体 runtime 匹配代码行）**
——Xray Core `v26.3.27`（`infra/conf/router.go`）里路由规则的 `"user"`
字段填充的是 `RoutingRule.UserEmail`（`protobuf:"bytes,7,rep,
name=user_email,..."`），按 Xray 公开文档/一般行为描述，运行时用它
匹配已认证连接的 client email——也就是上面 Marzban 构造出来的复合
`email` 值，不是 Marzban 数据库里存的纯 `username` 字段。

**结论**：`request.username`（"sub-123"）**不等于** Xray 实际匹配的
client email（"42.sub-123"）。第二版 Decision 3 选定的
`gateway_principal = request.username` 写入的是一个 Xray 侧从不会
用来匹配任何东西的值——如果 renderer 把这个值渲染进
`routing.rules[].user`，这条路由规则在真实 Marzban+Xray 组合下**永远
不会命中**，是一个此前未被发现的、会导致路由完全失效（而不是"路由到
错误的值"）的更严重问题。

### Exact-source 发现（第三次修订新增）：公开 API 是否能拿到 Marzban
DB user id 或复合 email

按独立审查要求，明确排除以下几种被禁止的推导方式：猜测自增 id、按
用户数量倒推 id、直接读生产 SQLite、把未公开的 DB 内部实现当作正式
API 契约依赖。逐一核实公开、受支持的 Admin API 端点：

- `app/models/user.py::UserResponse`（`POST /api/user`、
  `GET /api/user/{username}`、`GET /api/users` 等端点的响应模型）
  ——**`CONFIRMED`**（narrow 逐字段核对，纠正了此前一次基于
  `model_validate(dbuser)` 的不可靠推断）：这个类及其继承链
  （`username, status, used_traffic, lifetime_used_traffic,
  created_at, links, subscription_url, proxies, excluded_inbounds,
  admin` 及 `User` 基类字段 `expire, data_limit,
  data_limit_reset_strategy, inbounds, note, sub_updated_at,
  sub_last_user_agent, online_at, on_hold_expire_duration,
  on_hold_timeout, auto_delete_in_days, next_plan`）**不包含任何
  `id: int` 或等价字段**。Pydantic 的 `model_validate()` 只序列化
  目标 schema 声明过的字段，`dbuser`（ORM 对象）本身有 `id` 不代表
  响应会包含它——第一次基于"用 `model_validate(dbuser)` 构造响应"就
  推断"响应含 id"的判断方式不可靠，已被更精确的逐字段核对结果推翻。
- `add_user`（`app/routers/user.py`）的响应模型同样是
  `UserResponse`——同样不含 `id`。
- `generate_v2ray_links()`（`app.subscription.share`）构造订阅链接/
  二维码时使用的 `extra_data=self.model_dump()`，其中 `self` 同样是
  `UserResponse` 实例——同样不含 `id`，这条路径也无法把 DB id 暴露
  给任何客户端可见的产出物。

三个独立信号（`UserResponse` 字段列表、`add_user` 的响应模型、订阅
链接生成器的数据来源）**一致指向同一个结论**：Marzban v0.8.4 公开、
受支持的 Admin API **没有任何路径能返回 Marzban DB user id，因此也
没有任何路径能返回 Xray 实际使用的复合 client email
（`f"{id}.{username}"`）**。

**这是一个 `BLOCKED` 结论，不是一个可以绕过的实现细节**——按独立审查
明确要求："正确地留下一个有证据支持的 BLOCKED，比错误地宣布问题已
关闭更好"，本 ADR 在此明确记录：**在没有找到 Marzban 官方支持的、
能获取该复合 email 的方式之前，route identity 收敛问题不能被认为
已经解决。**

已考虑但未采纳的绕过方式（逐一说明为何不可行）：

- **假设 Marzban DB user id 从 1 开始自增，按创建顺序推导**——被
  用户明确禁止（"不得猜测自增 id"），且即使技术上可行也无法应对
  历史删除行导致的 id gap，脆弱且不可验证。
- **直接读 Marzban 的生产 SQLite 文件**——被用户明确禁止（"不得把
  生产 SQLite 当作正常 provider 契约读取"），且这是一个未公开的
  实现细节，不是 Marzban 承诺维持稳定的接口，随时可能因为 Marzban
  自身的数据库迁移而失效。
- **要求 Marzban 部署方额外暴露一个只读的 DB 连接给本应用查询
  `User.id`**——理论上可行，但这会让本应用依赖 Marzban 的内部 schema
  （不是公开 API），一旦 Marzban 升级版本改了表结构就会静默破坏，且
  这已经超出"只读研究现有公开 API"的范围，属于需要新 ADR 明确评估的
  架构决定，本 ADR 不在这里替 Phase 2B 做出这个决定。

### AccountUserDTO / 编排数据流影响（第三次修订新增，contingent on
上述 BLOCKED 状态，不是可以现在就实现的方案）

核实 `backend/app/providers/base.py::AccountUserDTO`（`create_user()`
的返回类型）目前字段为 `username, quota_bytes, expire_at, enabled`
——**没有任何字段可以携带一个"routing principal"值**。核实
`backend/app/domain/provisioning.py` 的 `CREATE_ACCOUNTING_USER`
步骤（第 6 步）：

```python
self._start(run_id, ProvisionStep.CREATE_ACCOUNTING_USER)
try:
    self.accounting.create_user(
        request.username, quota_gb_to_bytes(request.quota_gb), request.expire_at
    )
except Exception as exc:
    ...
self._success(run_id, ProvisionStep.CREATE_ACCOUNTING_USER)
```

**`create_user()` 的返回值被直接丢弃**——即使假设未来某个真实 Marzban
adapter 能通过某种方式取得复合 email，当前的编排代码里也**没有任何
数据通道**能把这个值从第 6 步传到第 7 步（`APPLY_GATEWAY`，调用
`self.state.desired_routing_state(request, endpoint, tenant)`）。

**候选设计（仅作为方向记录，不是本 ADR 的决定，contingent on 上一节
的 BLOCKED 被解决）**：如果未来找到 Marzban 官方支持的方式能取得复合
email，可以考虑：①给 `AccountUserDTO` 增加一个 `routing_principal:
str | None` 字段；②`CREATE_ACCOUNTING_USER` 步骤保留
`accounting.create_user(...)` 的返回值而不是丢弃；③把这个值经过
`ProvisioningService` 传给 `APPLY_GATEWAY` 步骤，再传给
`desired_routing_state()`/`ensure_gateway_route_binding()`；④
`GatewayRouteBinding.gateway_principal` 的语义相应地从"accounting
username"重新定义为"Xray routing principal"。**这个设计只有在
Marzban API 能可靠提供该值时才成立**——在 BLOCKED 状态解除之前，
不得把这个候选设计当作已经拍板的实现方案写进 Phase 2B 范围。

### `gateway_principal` 的最终语义（第三次修订）

`BLOCKED`——不能在本轮给出最终定义。已排除的错误定义：`accounting
username`（Xray 侧从不匹配这个值）。当前唯一已知的正确值
（`f"{marzban_db_user_id}.{username}"`）**无法通过 Marzban 公开 API
可靠获取**，因此本 ADR 不能在这里写下一个可实现的最终字段语义，只能
记录"候选设计"一节里 contingent 的方向。

### 候选对比（第二版评估维度，作为历史记录保留——候选 A 已因上述
exact-source 发现被推翻，不再是可选项；表格保留是为了让后续读者理解
候选 A 曾经为何被选中、现在为何不能再选）

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

### Decision 3（第三次修订：从 `Selected: A` 改为 `BLOCKED`）

**`BLOCKED`**——第二版"Selected: A"的结论被本轮 exact-source 发现
推翻：`gateway_principal = request.username` 写入的值不是 Xray 实际
匹配的 client email，而当前已知能反映真实 Xray client email 的值
（`f"{marzban_db_user_id}.{username}"`）无法通过 Marzban v0.8.4 公开
API 可靠获取（见上方"Exact-source 发现"两节）。

**候选 A/B/C 现状**：

- **候选 A**（`gateway_principal = request.username`）——**推翻**，
  理由如上，不再是可选项。
- **候选 B**（`accounting_user_id` 持久化时机提前）——同样使用
  accounting-side 的值（`Subscription.accounting_user_id`，本质上和
  accounting username 是同一类值），**同样不等于 Xray 实际匹配的
  client email**，同样被推翻，理由与候选 A 相同。
- **候选 C**（`sub-{order.id}` 正式契约化）——同样是一个纯
  accounting-side 推导值，同样不等于 Xray 实际匹配的 client email，
  同样被推翻。

**即：本轮的 exact-source 发现使候选 A/B/C 全部失效**，不是"在三者
之间重新选择"，而是"这三者共同的前提（Xray 匹配的是某种 accounting-
side 的值）本身就是错的"。第二版的比较表格因此只有历史参考价值——它
比较的是"三种得到错误目标值的方式哪种代价最小"，本轮发现问题出在
目标值本身，表格结论不再适用于选择正式方案。

**在 Marzban 官方 API 提供获取复合 email 的方式之前，本 ADR 不能给出
一个新的 Decision 3 结论**，只能记录：真正正确的 `gateway_principal`
应该是"Xray routing principal"（即 `f"{marzban_db_user_id}.
{username}"`），但这个值当前拿不到，因此正式方案 `BLOCKED`。

**已排除的绕过方式**：见上方"Exact-source 发现"一节列出的三种绕过
方式（猜测自增 id、直接读生产 SQLite、要求额外暴露只读 DB 连接）
及其被排除的理由，不在此重复。

### 既有数据收敛（第三次修订：改为 `BLOCKED`，contingent on Decision
3；NULL/未匹配行处理统一为严格 fail-closed 模型）

**第二版这里选定的方案（JOIN `Subscription.accounting_user_id` 把
`gateway_principal` 收敛成 accounting-side 的值）本身的目标值现在
已知是错的**——`accounting_user_id` 和 `request.username` 属于同一类
"accounting-side 值"，不等于 Xray 实际匹配的 client email，收敛到一个
错误值上不能解决问题，只是把错误值从"部署时写入的旧值（Webshare 出口
租户 ID）"换成"另一个同样错误的新值（accounting username/
`accounting_user_id`）"。

**这个方案在 Decision 3 解决之前是 `BLOCKED`**，不能继续作为既定
方案往下推进。以下记录 Decision 3 解决之后，无论最终目标值来源是
什么，既有数据收敛都必须遵守的通用要求（这部分与目标值具体是什么
无关，是本轮独立审查明确要求"NULL/未匹配行处理必须是真正的、无
自相矛盾的 fail-closed"这一要求的直接产物）：

1. **全量 preflight，禁止边跑边判断**：migration 必须先对全部目标行
   （`GatewayRouteBinding.enabled = True AND released_at IS NULL`）
   计算出目标 `gateway_principal` 值（或判定"无法计算"），**在对
   任何一行执行实际 UPDATE 之前**，先完成对全部行的检查。
2. **任何一行无法解析出目标值，整个 migration 中止，不做任何
   UPDATE**——不存在"跳过这一行，继续处理其它行，最后报告成功但有
   若干行被跳过"这种中间状态。第二版"跳过并记录，不猜测"这句话被
   本轮明确废止：**跳过不是一个可接受的落地行为，只有整体中止才是**；
   "记录"应该发生在中止之后的错误报告里，而不是发生在继续执行的
   过程中。
3. **任何一行的目标值会与另一行的目标值发生 `active_gateway_
   principal` 唯一约束冲突**（`uq_gateway_route_active_principal`），
   同样在 preflight 阶段检测到，整个 migration 中止，不做任何
   UPDATE，不依赖数据库 UNIQUE 约束在写入时报错来"发现"冲突。
4. **中止后的行为是"什么都不做"，不是"部分回滚"**——因为第 1/2/3 条
   已经保证在检测到任何异常时**尚未执行过任何 UPDATE**，所以不存在
   "已经改了一半，需要回滚"的场景；migration 的实现必须保证这个
   顺序（先全量校验，后全量写入），而不是"边校验边写，出错了再回滚
   已写的部分"。
5. migration 必须幂等（重复执行在相同输入下产生相同结果），符合
   `AGENTS.md` 铁律第 7 条对 reconciliation migration 的要求；幂等性
   要求与上述 fail-closed 要求不冲突——"重复执行结果一致"既可以是
   "重复执行都成功且不做多余写入"，也可以是"重复执行都在 preflight
   阶段检测到同样的问题并同样中止"，两种情况都满足幂等定义。
6. **历史数据能否被正确收敛，取决于 Decision 3 的解决方式**：如果
   最终确定的目标值（Xray routing principal）**无法从本应用当前的
   本地 DB 计算出来**（例如它依赖 Marzban 内部的 DB user id，而
   本应用从未持久化过这个 id），那么"纯本地 DB 数据收敛 migration"
   在结构上就无法计算出正确的目标值——这种情况下，正确的下一步不是
   "退而求其次收敛到一个已知错误的值"，而是诚实记录：需要一个额外的
   数据来源（例如要求 Marzban 侧提供一次性的 `username → 
   marzban_user_id` 映射导出，作为一次性的、经人工核实的迁移输入），
   这已经超出"纯 Alembic data-only migration"能独立解决的范围，必须
   在 Decision 3 解决时一并明确，不能假设"总能找到一种本地数据收敛
   方式"。
7. **是否需要 DB schema 变更**同样 `BLOCKED`，contingent on Decision
   3：如果最终确定 `gateway_principal` 需要存储一个当前 schema 没有
   持久化过的值（例如需要一个新字段存储"Marzban DB user id"本身，
   而不只是复合后的字符串），schema change 就会从"NO"变成"YES"——
   第二版"Schema change: NO"的结论不能在 Decision 3 BLOCKED 期间
   继续被当作已确定的结论保留，本轮改为 `BLOCKED`。

**为什么不能像第二版一样直接给出"选定方案"**：第二版在"目标值是
`accounting_user_id`"这个（现在已知错误的）前提下比较了方案 A/B/C/D
四种"如何把旧值收敛到目标值"的路径。本轮发现问题出在目标值本身，
在目标值确定之前讨论"如何收敛到目标值"的具体路径没有意义——一旦
Decision 3 确定了正确的目标值来源，本节第 1-7 条的通用 fail-closed
要求仍然适用，但"具体从哪张表/哪个字段 JOIN 出目标值"这部分需要
重新设计，不能照搬第二版 JOIN `Subscription.accounting_user_id` 的
具体做法。

## Reality ownership：无 NEW EVIDENCE，维持 ADR-014 结论

本次研究过程中没有发现任何推翻 ADR-014 已 Accepted 的 Reality
ownership 结论（inbound protocol/listen/port → 静态模板；Reality
`dest`/`serverNames` → 环境变量；Reality `privateKey`/`shortIds` →
DB/Secret；routing/outbounds/route identity → DB；运行时 config →
只读校验/回滚/漂移来源）的证据，本 ADR 不重新打开这些问题。

## ADR-014 Phase 2B gate 与本 ADR 的关系（第三次修订第二轮新增，修复
本轮 Major 3）

**冲突**：`ADR-014` 的"约束"第 5 条明确写着——**"Phase 2B 开始前，
必须先在事实三的候选 A/B/C 中选定 `gateway_principal`/
`accounting_user_id` 的收敛方向……这不是可以在写 DTO 的过程中顺便
决定的细节"**——这是对**整个 Phase 2B**（不是只对 route identity
相关的那部分代码）设的前置门槛。本 ADR 第三次修订第一轮的"Decision
4 implementation matrix"/"Phase 2B implementation handoff"两节曾经
写"Decision 3 `BLOCKED` 的同时，非 route-identity 的其它 `YES` 条目
（Reality Settings/Secret、accounting health、drift detection 等）
可以先行实现"——**这和 ADR-014 的第 5 条门槛直接冲突，且本 ADR 当时
没有显式声明 supersede 这一条**，导致两份都处于"已接受"状态的 ADR
对"Decision 3 BLOCKED 期间能不能开始 Phase 2B"给出了不同的答案，是
一个真实的治理缺口，不是文字表述问题。

**本轮采纳的解决方案：保持 Phase 2B 整体 gated，不 supersede ADR-014
第 5 条**——按独立审查建议的更保守路径：这是一个涉及真实生产
provider（Marzban）和 Xray 运行时配置的高风险改动序列，本仓库一贯的
序列是"契约收敛 → 测试护栏 → 实现 → opt-in → 隔离验证 → 人工生产
批准"；在 route identity（本次改动序列里最核心的契约问题）仍然
`BLOCKED` 的情况下开始实现 DTO/schema/renderer 的其它部分，一旦
Decision 3 解锁后发现需要的改动和已经落地的部分冲突（例如 DTO 形状、
渲染器的字段假设），会造成返工，代价高于"等 Decision 3 解锁后再一次性
实现"。

**具体规则**：

1. `ADR-014` 第 5 条继续完整有效，本 ADR **不 supersede** 它——在
   Decision 3（route identity 收敛方向）解锁之前，**不得开始任何
   Phase 2B 代码实现**，包括下方 Decision 4 矩阵里标 `YES` 的条目
   （Reality Settings/Secret、accounting health、drift detection、
   preservation/validation、共享文件 writer guard 等）——这些条目
   在下方矩阵里标 `YES` 表示"设计方向已经确定、不再是 UNVERIFIED"，
   **不表示"可以现在开始写代码"**，两者是不同的问题，第三次修订第
   一轮把两者混为一谈，本轮更正。
2. **PR #56（本 ADR 所在的 Phase 2B0 研究）可以作为"blocker-discovery
   / blocker-record"合并**——它本身不实现任何代码，记录的是"哪些前置
   问题已经关闭、哪个还 BLOCKED"这个诚实的研究结论，合并它不等于
   授权开始 Phase 2B 实现。
3. **下一步不是 Phase 2B 实现 PR，而是一个专门解决 Decision 3 /
   route identity architecture 的任务**——目标是让 Decision 3 从
   `BLOCKED` 变成一个有 exact-source 支持的、可实现的结论（见 Part C
   "BLOCKED 解除条件"一节列出的几种可能路径：Marzban 未来版本新增
   支持、新写一份 ADR 评估"要求 Marzban 部署方暴露只读 DB 连接"这个
   选项、或核实到当前遗漏的其它公开机制）。
4. **只有 Decision 3 解锁之后，才能启动真正的 Phase 2B 实现 PR**，
   届时下方 Decision 4 矩阵里所有 `YES`/一并解锁的 `BLOCKED` 条目
   才能一起开始实现——矩阵本身仍然有价值，作为"Decision 3 解锁后
   Phase 2B 需要做哪些事"的范围记录，但不是"现在就能开始做哪些事"
   的授权清单。

**如果未来项目决定不采纳这个更保守的路径**，需要走另一条路：新写一份
明确的 ADR 修订（可以是本 ADR 的后续修订，也可以是新 ADR），显式写
`本 ADR supersede ADR-014 第 5 条关于"Phase 2B 开始前必须先选定
route identity 收敛方向"的门槛`，并逐项证明允许先实施的每一类工作
（Settings/Secret/health/drift baseline/preservation）都和 route
identity **完全解耦**——不能像第三次修订第一轮那样，两份 ADR 各写
各的规则、不显式互相引用地并存。本轮不采纳这条路径，只记录它作为
"如果未来要推翻本轮结论"的显式选项。

## Decision 4：Phase 2B implementation matrix（第三次修订：按独立
审查要求，允许 `BLOCKED`/`DECISION_REQUIRED`；第二轮更正：本表格是
"Decision 3 解锁后 Phase 2B 的范围记录"，不是"现在可以开始实现"的
授权——见上方"ADR-014 Phase 2B gate 与本 ADR 的关系"一节）

| 改动类型 | 需要？ | 说明 |
|---|---|---|
| `DesiredRoutingState` DTO change | **YES** | 扩展以表达完整 outbound 连接细节；route identity 部分 `BLOCKED`（见下方专项） |
| `AccountUserDTO`/accounting 返回契约 change | **BLOCKED** | contingent on Decision 3——只有 Marzban API 能提供 routing principal 时才需要加 `routing_principal` 字段，见 Part C"AccountUserDTO / 编排数据流影响" |
| provisioning 编排/数据流 change（`CREATE_ACCOUNTING_USER` → `APPLY_GATEWAY`） | **BLOCKED** | 同上，contingent on Decision 3；当前编排丢弃 `create_user()` 返回值，若 Decision 3 需要传递 routing principal，这里需要新增数据通道 |
| Settings change | **YES** | `XRAY_REALITY_DEST`/`XRAY_REALITY_SERVER_NAME` 读取路径（ADR-014 已定方向），与 route identity 无关，不受本轮 BLOCKED 影响 |
| Secret persistence change | **YES** | Reality `privateKey`/`shortIds` 走 `Secret` 表（ADR-014 已定方向），与 route identity 无关 |
| **DB schema change（route identity 部分）** | **BLOCKED** | 第二版"NO"的结论不再成立——如果 Decision 3 最终需要持久化 Marzban DB user id 或类似的新数据，需要新列/新表 |
| **DB schema change（drift-detection 基线部分）** | **YES（新增独立条目）** | `TransportVersion`/`EgressVersion` 确认不适用（bounded-context 不匹配、且当前零引用），需要新的、scope 限定为共享 Xray 配置文件的 `last_applied_config_hash`/`last_applied_at` 持久化位置，具体形态留 Phase 2B 详细设计 |
| **route identity 收敛方案本身**（Decision 3） | **BLOCKED** | Marzban 公开 API 无法提供 Xray 实际匹配的复合 client email（`{marzban_user_id}.{username}`），见 Part C |
| **既有数据 reconciliation** | **BLOCKED** | contingent on Decision 3；目标值未定之前无法设计具体的 JOIN/收敛逻辑；通用 fail-closed 要求（全量 preflight、检测到任何异常整体中止、不允许"跳过并记录"）已经确定，不受 BLOCKED 影响，可以先写进验收标准 |
| provisioning writer change（route identity 部分） | **BLOCKED** | 同 Decision 3 |
| **accounting health contract/API**（H1） | **YES，不受本轮三个 Major 影响** | 给 `AccountingProvider` 新增 `health_check()`，`admin_accounting_health()` 改为调用 `accounting.health_check()`；`MockAccountingProvider` 和未来的真实 Marzban adapter 都要实现——这一项是 Part B 的结论，本轮三个 Major 只涉及 Part C（route identity）和 Decision 1（drift detection），不涉及 Part B，继续保持 YES |
| query/adapter change | **YES（不含 route identity 部分）** | 期望态查询参照 `render_xray_routes.py::_active_routes()` 的 JOIN 逻辑扩展到 provider 抽象层；route identity 相关的具体 JOIN 逻辑 `BLOCKED` |
| Xray renderer change | **YES（不含 route identity 部分）** | 产出完整 outbound；Reality `dest`/`serverNames` 改为只读 env；route identity 相关的 `routing.rules[].user` 渲染逻辑 `BLOCKED`（渲染 `request.username` 会产生一条永远不会命中的路由规则，不能先上线这部分） |
| preservation/validation change | **YES** | 按 ADR-014"合法删除语义"重写；保留 Marzban 协议匹配约束（Part A）；与 route identity 无关 |
| **共享 `XRAY_JSON` writer guard / drift policy** | **YES，算法已重写** | 本仓库 renderer 是唯一合法 writer；本应用不得调用 Marzban `PUT /api/core/config`；drift detection 改用本轮"三态模型"（`last_applied_state`/`current_disk_state`/`new_db_desired_state`），需要新的基线持久化（见上）和显式 bootstrap 策略（见下） |
| **drift-detection 首次运行 (bootstrap) 策略** | **YES（本轮新增独立条目）** | 无基线时需要一次显式的"采纳当前磁盘状态为初始基线"操作并记录，不能静默假设"无基线=无漂移"；文件缺失时按正常渲染处理；基线只在实际写盘成功后更新，不在校验通过但未写盘时更新 |
| tests | **部分 YES，部分 BLOCKED** | 可以现在写：`accounting.health_check()` 契约测试、drift-detection 三态模型的单元测试（含 bootstrap/文件缺失/基线更新时机场景）、既有数据 reconciliation 的通用 fail-closed 行为测试（不依赖具体目标值，只测试"任何一行无法解析就整体不写入"这个不变量）；不能现在写：`gateway_principal` 最终值的契约测试（因为最终值本身 `BLOCKED`） |

## Phase 2B implementation handoff（第三次修订第二轮更正：整体维持
`ADR-014` 第 5 条的 gate，本 ADR 不 supersede 它——不是"部分可以先做"）

**第一轮的错误**：写了"可以实现的部分（Reality Settings/Secret、
accounting health、drift detection 等）"和"明确排除的部分（route
identity 相关）"，暗示 Decision 3 `BLOCKED` 期间仍可以启动前者的
代码实现。**这个结论已被本轮 Major 3 推翻**：`ADR-014` 第 5 条的
门槛适用于**整个 Phase 2B**，本 ADR 没有（也不打算，见上方"ADR-014
Phase 2B gate 与本 ADR 的关系"一节）显式 supersede 它，因此不能一边
维持这条门槛一边又说"部分工作可以先做"。

**更正后的规则**：**在 Decision 3 解锁之前，不启动任何 Phase 2B 代码
实现**，无论该项在 Decision 4 矩阵里标的是 `YES` 还是 `BLOCKED`。
下一步是一个专门解决 Decision 3 / route identity architecture 的
任务，不是 Phase 2B 实现 PR——具体见上方"ADR-014 Phase 2B gate 与
本 ADR 的关系"一节的规则 3。Decision 4 矩阵保留作为"Decision 3 解锁
后 Phase 2B 的范围记录"，供那时候的实现 PR 参照，不是当前的实施
授权。

以上仍然**不包括**：`registry.py` 的真实 opt-in wiring、生产环境
启用、真实生产凭据、真实 Xray reload、部署——这些仍然属于 Phase 2C，
在 Phase 2B 本身开始之前更不适用。

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
4. **（第三次修订更正，原第 4 条已被推翻）** Decision 3 目前是
   `BLOCKED`：不得在 Marzban 官方 API 能否提供 Xray 实际匹配的复合
   client email（`{marzban_user_id}.{username}`）这一问题解决之前，
   实现"写入 `gateway_principal = request.username`"或任何形式的
   既有数据 reconciliation migration——这两者都建立在一个已被
   exact-source 证据推翻的错误目标值上，实现它们不会解决问题，只会
   把错误值继续写进生产数据。
5. Part A 发现的两条约束都必须写进 Phase 2B 的验收标准：①renderer
   产出的 inbound tag 不能破坏 Marzban 协议匹配；②本仓库 renderer 是
   共享 `XRAY_JSON` 唯一合法 writer，需要 drift detection 保护这个
   边界。
6. Decision 2 新增的 accounting health 决定（H1）必须写进 Phase 2B
   实现范围，不能停留在"以后再说"。
7. **（第三次修订新增）** drift detection 必须使用"三态模型"
   （`last_applied_state`/`current_disk_state`/`new_db_desired_
   state`），不得使用第二版"磁盘 vs DB 期望态"的两态比较算法；必须
   新增 scope 限定为共享 Xray 配置文件的基线持久化（不得复用
   `TransportVersion`/`EgressVersion`）；必须有显式的 bootstrap
   （首次运行）策略，不得静默假设"无基线=无漂移"。
8. **（第三次修订新增）** 既有数据 reconciliation 的 NULL/未匹配行
   处理必须是"全量 preflight → 检测到任何异常整体中止，不做任何
   UPDATE"，不允许"跳过并记录、继续处理其它行"这种部分成功的模式。
9. **（第三次修订第二轮新增）** drift baseline 的 fingerprint 必须
   覆盖整份本仓库拥有的、写入共享 Xray 配置文件的对象（不只是
   `outbounds`/`routing`），任何排除字段必须显式列出并说明理由；
   `last_applied_state`/`current_disk_state` 必须使用完全相同的
   canonicalization 规则；无法解析的 JSON 必须 fail closed，不能
   当作"和上次一样"处理。
10. **（第三次修订第二轮新增）** `last_applied_state` 只能在
    `XrayFileProvider.apply()`（或其等价实现）整个九步流程成功返回
    `ApplyResult(True, ...)` 之后才提交更新；rollback 成功时基线保持
    不变；rollback 自身失败时基线必须标记未知并 fail closed；Xray
    运行时已生效但基线持久化失败时，必须在"回滚运行时"或"标记系统
    unknown/fail-closed"之间明确选一个，不允许放任 baseline 和
    runtime 长期不一致却不做任何标记。
11. **（第三次修订第二轮新增）** 本 ADR 不 supersede `ADR-014` 第 5
    条关于"Phase 2B 开始前必须先选定 route identity 收敛方向"的门槛；
    在 Decision 3 从 `BLOCKED` 解锁之前，不得启动任何 Phase 2B 代码
    实现（无论 Decision 4 矩阵里标 `YES` 还是 `BLOCKED`）；下一步是
    专门解决 route identity 的任务，不是 Phase 2B 实现 PR。
12. **（第三次修订第三轮新增，第四轮扩展）** 当前 `XrayFileProvider.
    apply()` 对 `install()`/第一次 `reload()` 抛出的异常没有回滚
    兜底（见 Part A"场景 D"/"pre-health mutating exception"）；
    Phase 2B 实现 drift-detection 基线机制之前，必须先加固 `apply()`
    让这两处异常也进入等价于场景 A/B 的处理路径，并补齐四类护栏
    测试（`install()` 异常、首次 `reload()` 异常、rollback
    `restore()` 异常、rollback 第二次 `reload()`/健康复核失败）；
    在这个加固完成之前，不能假设当前代码已经对所有 apply 失败模式
    提供了安全的回滚保证。

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
9. **（第三次修订新增）候选 A（`gateway_principal = request.
   username`，第二版曾经的 Selected）**：被本轮 exact-source 发现
   推翻——Xray 实际匹配的是 `f"{marzban_user_id}.{username}"`，不是
   纯 accounting username，候选 A 写入的值在真实 Marzban+Xray 组合下
   永远不会被路由规则命中，否决。
10. **（第三次修订新增）猜测 Marzban DB user id 按自增顺序推导**：
    被用户明确禁止，且无法应对历史删除导致的 id gap，脆弱不可验证，
    否决。
11. **（第三次修订新增）直接读取 Marzban 生产 SQLite 获取 DB user
    id**：被用户明确禁止，且这是未公开的实现细节，不是 Marzban
    承诺维持稳定的契约，否决。
12. **（第三次修订新增）复用 `TransportVersion`/`EgressVersion` 作为
    drift-detection 的 last-applied 基线存储**：grep 确认两者当前
    零引用，且按 `route_group_id` 组织，与"单一共享 Xray 配置文件"
    的 bounded context 不匹配，属于"因为字段形状像就复用"的错误
    硬映射，否决，改为新增独立的、scope 限定的持久化机制。
13. **（第三次修订新增）drift detection 静默假设"无基线 = 无漂移"**：
    会把"从未记录过基线"和"确认过没有被篡改"混为一谈，在首次引入
    基线机制时可能放行一个实际已经被篡改过的磁盘状态，否决，改为
    显式的、被记录的"采纳当前磁盘状态为初始基线"操作。

## 安全影响

- Part A 更正后的发现（Marzban 的 core-config 管理 API 会写共享
  文件）是一个此前未被识别的真实风险：如果运维习惯性通过 Marzban UI
  修改 core 配置，本仓库 renderer 下一次运行会静默覆盖运维的改动，
  或者运维的改动会覆盖 renderer 的期望态输出——**两个方向都可能导致
  配置意外偏离数据库期望态**，这正是需要 drift detection 的直接原因，
  优先级应视为 Phase 2B 的一部分，不是可以延后的次要项。
- **（第三次修订更正，原表述已被推翻）** 第二版这里描述的风险
  （"不做既有数据 reconciliation 会导致新旧订阅路由不一致"）建立在
  候选 A 是正确方案这个前提上，这个前提已被本轮 exact-source 发现
  推翻。**真实风险比第二版描述的更严重**：候选 A 写入的
  `gateway_principal = request.username` 从一开始就不是 Xray 实际
  匹配的值——如果 Phase 2B 在 Decision 3 BLOCKED 状态下仍然实现了
  候选 A，会导致**新开通的订阅路由规则从渲染出来那一刻起就永远不会
  命中**（不是"和旧订阅不一致"，而是"新旧订阅的路由可能都是错的"），
  且同样没有自动检测机制会发现它，直到某个客户报告连不上。这是本轮
  把 Decision 3 改为 `BLOCKED`、并在"约束"一节明确禁止在此之前实现
  候选 A 的直接动机。
- accounting health 决定（H1）修正后，`/admin/accounting/health`
  会真正检查 Marzban（而不是无关的 subscription transport）——在此
  之前，这个端点给运维的信号是误导性的（显示"健康"可能只是因为
  subscription transport 健康，Marzban 本身可能已经故障），这是一个
  被本轮修正之前一直存在、未被识别的运维可观测性缺口。

## 重新评估条件

如果 Phase 2B 实现 drift detection 时发现 Marzban 的 `PUT
/api/core/config` 在实际部署里从未被运维使用过（例如权限层面已经
天然不可达），可以相应降低 drift detection 的实现优先级，但仍然
应该保留检测能力，不应该假设"未来也永远不会被调用"。

**（第三次修订新增）Decision 3 的 `BLOCKED` 状态的解除条件**：以下
任一情况出现时，应重新打开 Decision 3 的讨论——(1) 未来版本的
Marzban 在公开 Admin API 里新增了暴露 DB user id 或复合 client
email 的字段/端点；(2) 项目决定接受"要求 Marzban 部署方额外暴露一个
只读 DB 连接供本应用查询 `User.id`"这个此前被列为"需要新 ADR 评估"
的选项，并为此写一份新的 ADR 正式评估其风险（依赖未公开 schema、
版本升级兼容性）；(3) 决定改用 Marzban 官方文档中记录的、本轮尚未
核实到的其它公开机制（例如未来核实到某个当前遗漏的端点确实返回
该值）。在这些条件被满足并有新的 exact-source 证据之前，不应该
重新尝试实现候选 A/B/C 中的任何一个。
