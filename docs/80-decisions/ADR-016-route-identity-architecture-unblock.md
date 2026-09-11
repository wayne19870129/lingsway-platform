# ADR-016: Decision 3 / Xray route identity architecture unblock

- 状态: 已接受
- 日期: 2026-09-11
- 决策范围: TASK-T16 route-identity architecture research (docs/ADR-only,
  不实现代码)。本 ADR 是 `ADR-015-marzban-ownership-and-route-identity.md`
  Decision 3（`BLOCKED`）的专项后续研究，**supersede** ADR-015 的
  Decision 3 及"既有数据收敛"两节的最终结论（ADR-015 其余部分——Part A
  drift detection、Part B provider classification/H1——不受影响，继续
  有效，本 ADR 不重新讨论）。
- 前置: `ADR-014-xray-desired-state-ownership.md`、
  `ADR-015-marzban-ownership-and-route-identity.md`（Decision 3 =
  `BLOCKED`，本 ADR 的起点）

## Context

`ADR-015` 通过 exact-source 核实确认：Marzban v0.8.4（commit
`7f396db3e703d71a28060bc9ce4a532ec64cb1f4`）传给 Xray、Xray 实际用于
`routing.rules[].user` 匹配的 client email 是
`f"{marzban_db_user_id}.{username}"`，不是纯 accounting username；
Marzban v0.8.4 公开、受支持的 Admin API（`UserResponse` 家族）不暴露
这个 DB id，导致 Decision 3（`GatewayRouteBinding.gateway_principal`
该存什么、如何获取）在 ADR-015 里被诚实标记为 `BLOCKED`，而不是强行
选一个错误方案。

本 ADR 的任务：在不猜测、不读生产 DB、不使用真实凭据的前提下，找出
一个受支持、可长期维护、fail-closed 的方式获得并持久化 Xray 实际
routing principal，把 Decision 3 从 `BLOCKED` 推进到一个可以落地的
architecture 结论——或者，如果确实找不到，精确缩小 blocker 范围。

**方法**：只读检索 `github.com/Gozargah/Marzban` 公开源码（`v0.8.4`
tag 及 `master` 分支当前 HEAD，用于确认结论是否随时间变化）、公开
release notes、公开 webhook 文档；未连接任何真实 Marzban 实例，未
使用任何真实凭据，未读取生产 SQLite/MySQL，未调用真实 provider。

## Part A：仓库当前实际数据流（重新核实，非抄 ADR-015 总结）

逐一核实以下代码触点在最新 `main`（`cbbe618`，已包含 PR #56）上的
真实状态：

1. **`backend/app/providers/base.py::AccountUserDTO`**：字段仍是
   `username, quota_bytes, expire_at, enabled` 四个，**没有**任何
   携带 routing principal 的字段。`AccountingProvider.create_user()`
   的返回类型就是这个 DTO。
2. **`backend/app/domain/provisioning.py::ProvisioningService.
   provision()`**：`CREATE_ACCOUNTING_USER` 步骤（第 193-202 行）
   调用 `self.accounting.create_user(...)`，**返回值未被赋值给任何
   变量，直接丢弃**。`APPLY_GATEWAY` 步骤（第 204-218 行）独立调用
   `self.state.desired_routing_state(request, endpoint, tenant)`，
   和上一步没有任何数据传递关系。
3. **`backend/app/infra/provisioning_state.py::
   SqlAlchemyProvisioningState`**：
   - `ensure_gateway_route_binding(gateway_principal, endpoint)`
     （第 172-198 行）：按 `subscription_id` upsert
     `GatewayRouteBinding`，`gateway_principal` 参数直接写入同名列。
   - `desired_routing_state(request, endpoint, tenant)`（第
     200-207 行）：**当前调用是
     `self.ensure_gateway_route_binding(tenant.tenant_id, endpoint)`**
     ——写入的是 Webshare 出口租户 id（`EgressEndpointDTO`/
     `TenantDTO` 语境下的 `tenant.tenant_id`），既不是 accounting
     username，也不是 Xray 实际匹配的复合 email；`DesiredRoutingState.
     user_routes` 则用 `request.username` 作为 key。
4. **`backend/app/models/gateway.py::GatewayRouteBinding`**：
   `gateway_principal: Mapped[str]`（必填字符串列），另有计算列
   `active_gateway_principal`（`released_at IS NULL AND enabled=1`
   时等于 `gateway_principal`，否则 `NULL`），带唯一约束
   `uq_gateway_route_active_principal`。
5. **`backend/app/models/subscription.py::Subscription.
   accounting_user_id`**：`String(128)`，唯一约束，可空，独立于
   `GatewayRouteBinding.gateway_principal`；`accounting_user_id` 由
   `backend/app/api/admin.py`（第 625-626 行）在
   `admin_confirm_payment` 流程里、订阅确认之后才写入，晚于
   `APPLY_GATEWAY` 执行的时间点（ADR-014 已记录的时序缺口，本轮
   重新核实仍然成立）。
6. **`ops/gateway/render_xray_routes.py`**（本仓库唯一的、周期性
   全量重渲染共享 `xray_config.json` 的脚本，ADR-014/015 已确认的
   sole legitimate writer）：`_active_routes()`（第 94-119 行）
   JOIN 所有 `enabled=True AND released_at IS NULL` 的
   `GatewayRouteBinding` 行；`render_config()`（第 122-156 行）
   **直接使用 `route.gateway_principal`（不是
   `Subscription.accounting_user_id`，也不是 `request.username`）
   作为 `{"type": "field", "user": [principal], ...}` 路由规则的
   `user` 值**——这是真正决定生产环境 Xray 路由匹配结果的唯一代码
   路径，本 ADR 后续所有方案的目标都是让这一列的值稳定等于 Xray
   实际匹配的 client email。
7. **`backend/app/providers/accounting/`**：目录下只有 `mock.py`
   （`MockAccountingProvider`），**没有任何真实 Marzban-backed
   `AccountingProvider` 实现**，`create_user()` 只是内存字典操作，
   不涉及任何真实网络调用——本 ADR 的方案设计因此完全是"应该怎么
   设计未来的真实实现"，不涉及改动任何已有真实代码路径。

**结论**：当前数据流的问题不只是"没有 routing_principal 字段"，还有
两处独立的语义/时序缺口：`ensure_gateway_route_binding` 的第一个
参数当前传的是 Webshare 租户 id（比 ADR-015 讨论的"accounting
username 错误目标值"更早一层的历史遗留值）；`Subscription.
accounting_user_id` 的持久化时机晚于 `APPLY_GATEWAY`。任何 Decision 3
方案都必须同时修正这三处（目标值来源、写入参数、编排时序），而不是
只换一个字符串公式。

## Part B：Marzban 版本调研（exact-source，非假设版本号）

### 当前实际可用版本

通过公开 GitHub releases 页核实（`github.com/Gozargah/Marzban/
releases`）：稳定 tag 序列为 `v0.5.0` → `v0.8.4`（当前 pinned，2026
年之前发布的最新稳定版）；此外存在 `v1.0.0-alpha-1` 到
`v1.0.0-beta-3`（2025 年 7-8 月）一系列**预发布**版本，尚未有
`v1.0.0` 正式稳定 tag。按仓库既有安全边界（不得假设/依赖未稳定发布的
版本），**当前唯一可以作为"最新稳定版"评估对象的仍然是 `v0.8.4`**，
`v1.0.0-beta-*` 只作为"未来可能改变结论的方向"参考，不构成本轮
Candidate A 的可选基线。

### `master` 分支（当前开发中 HEAD，用于验证结论是否已过时）

逐项核实 `raw.githubusercontent.com/Gozargah/Marzban/master/...`：

- **`app/models/user.py`**：`User`/`UserCreate`/`UserModify`/
  `UserResponse`/`SubscriptionUserResponse`/`UsersResponse`/
  `UserUsageResponse`/`UserUsagesResponse` 等全部模型类逐一核对字段
  列表——**没有任何类声明整数 `id` 字段**，和 v0.8.4 结论一致，
  `master` 没有为这个问题引入修复。
- **`app/xray/operations.py`**：`add_user`/`update_user`/
  `remove_user` 构造 email 的代码仍然是
  `email = f"{dbuser.id}.{dbuser.username}"`——**和 v0.8.4 完全
  一致，复合 email 的构造方式没有变化**。
- **`app/routers/user.py`**：全部端点（`POST /api/user`、
  `GET/PUT/DELETE /api/user/{username}`、`GET /api/users` 等）的
  `response_model` 仍然是 `UserResponse`/`UsersResponse` 家族——
  由于这些模型本身不含 `id`，这些端点同样不暴露它。
- **官方 webhook 功能**（`WEBHOOK_ADDRESS` 环境变量，`app/utils/
  notification.py`）：**这是一个此前两轮 ADR-015 研究都没有覆盖的
  真实存在的官方推送通道**，v0.8.4 起就有文档记录，逐一核实
  `notification.py` 里 `UserCreated`/`UserUpdated`/`UserDeleted` 等
  通知类的字段：`username`、**`user`（类型是 `UserResponse` 对象）**、
  `by`（Admin 对象）、`action`、`enqueued_at`/`send_at`/`tries` 等
  时间/重试元数据、以及各 action 专属字段（`used_percent`、
  `days_left`、`reason` 等）。**`user` 字段虽然是完整对象，但它的
  类型就是 `UserResponse`——同一个已经确认不含 `id` 的 schema，
  webhook 复用了它，没有额外暴露 DB id**。

**综合结论（`CONFIRMED`，master 和 v0.8.4 交叉验证）**：Marzban 官方
当前所有公开、受支持的用户数据出口——`create`/`get`/`list` 响应、
webhook 推送——**没有任何一个暴露 DB 内部 `id`，也没有任何一个直接
暴露 `f"{id}.{username}"` 这个 Xray 实际使用的复合 email**。这个
结论从 v0.8.4 到当前 `master` 保持不变，**纯粹的版本升级不能解决
这个问题**——升级本身既不会新增缺失的字段，也没有计划中的相关变更
迹象（release notes 里 v1.0.0-beta 系列的改动集中在 node 同步/
schema bug fix/非 sudo 管理员 API，未提及 user id 暴露）。

## Part C：Candidate 评估

### Candidate A — 升级到"支持公开 routing identity"的 Marzban 版本

**结论：`REJECTED`（不成立，不是"暂不推荐"，是证据证明它不解决问题）**。
Part B 已经证明：从 `v0.8.4` 到 `master` 当前 HEAD，`UserResponse`
家族和 webhook payload 都没有暴露 DB id 或复合 email，且没有已知
计划中的变更方向。升级本身不改变这个事实——**没有一个"更新的稳定
版本"存在，可以让这个方案成立**。如果未来 Marzban 真的发布一个
新增该字段的版本，属于 ADR-015 已经记录的"重新评估条件"之一，
不是现在就能选择的方案。upgrade cost/API compatibility/DB
compatibility 等分析因此不适用——方案在事实层面不成立，不需要展开
代价分析来否决它。

### Candidate B — 受控 Marzban 补丁/companion 机制

**结论：`SELECTED`**——具体形态：**在本仓库已经维护的、pinned 到
精确 commit 的 Marzban Docker 镜像构建过程中，对 `app/models/
user.py::UserResponse` 做一处最小化、单字段的源码补丁**，让响应
里新增一个只读字段：

```python
# 在 UserResponse 的序列化路径上追加（伪代码，仅描述改动范围和位置，
# 不是最终实现，Phase 2B 落地时可能用 field_validator/model
# 装饰器等更贴合 Pydantic 版本的写法）：
routing_principal: str  # = f"{dbuser.id}.{dbuser.username}"，与
                         # app/xray/operations.py 计算复合 email 时
                         # 使用完全相同的公式，逐字节一致
```

逐条核对独立审查要求的约束条件：

- **不暴露任意 DB 查询能力**：这个补丁只是给已有的、按用户名/
  管理员权限鉴权过的 `UserResponse` 序列化路径多加一个已经从
  `dbuser` 对象上能拿到的衍生字段，不新增任何端点、不新增任何
  查询能力、不允许按 id 反查其它用户。
- **只暴露当前用户所需的 stable routing identity**：`routing_
  principal` 只是 `f"{dbuser.id}.{dbuser.username}"` 的字符串
  拼接结果，和 `app/xray/operations.py` 已经在用的公式完全一致——
  这不是新增一个"秘密"值，只是把 Marzban 内部已经在计算、只是没有
  对外暴露的同一个值，多序列化一次。
- **有鉴权**：完全继承现有 `GET/POST/PUT /api/user*` 端点已有的
  管理员 Bearer token 鉴权，不新增鉴权路径，不降低现有安全边界。
- **不泄露其它用户数据**：`routing_principal` 是每个用户各自的
  衍生字段，和现有 `UserResponse` 其它字段（`username`、
  `used_traffic` 等）的隐私范围完全一致。
- **可以固定在明确 Marzban commit/version**：补丁基于当前 pinned
  的精确 commit `7f396db3e703d71a28060bc9ce4a532ec64cb1f4`（`v0.8.4`）
  制作，作为本仓库 `infrastructure/marzban/` 下维护的一个小型
  source patch（例如 `patches/0001-expose-routing-principal.patch`），
  在 Docker build 阶段应用到官方镜像源码上，不是长期维护一个完整
  fork。
- **升级维护成本可接受**：补丁只涉及一个模型类的一个字段，且这个
  字段的计算公式（`f"{id}.{username}"`）已经在 `operations.py`
  里被独立验证是 Marzban 自己内部长期使用的既有约定，不是一个
  容易被下一次升级破坏的实现细节；Phase 2B 必须把"升级前重新核对
  这个公式是否仍然成立、patch 是否还能干净应用"列为强制 CI 步骤
  （见下方"重新评估条件"）。
- **必须明确谁拥有这个 extension**：本仓库（Lingsway 运维/开发团队）
  拥有并维护这个 patch 文件，不依赖上游 Marzban 项目接受这个改动
  （不提交上游 PR 作为前提条件，虽然这个字段本身足够通用，未来可以
  尝试上游贡献，但本 ADR 不假设它会被接受）。
- **必须说明升级时如何验证不会失效**：Phase 2B 必须新增一个 CI/
  部署前检查步骤——在任何 bump `MARZBAN_IMAGE_TAG`/pinned commit 的
  改动里，强制要求这个 patch 文件针对新版本 `app/models/user.py`/
  `app/xray/operations.py` 的 diff 干净应用（`git apply --check`
  或等价机制），如果不能干净应用，CI 必须 fail closed，阻止合并，
  而不是静默跳过打补丁步骤后仍然启动服务。

**为什么不是"在 Marzban 之外维护一个完全独立的 fork"**：一个字段的
补丁不构成"重写/长期分叉整个项目"的理由，用 patch-on-build 的方式
可以持续基于官方镜像增量更新，維护成本远低于维护一份完整分支。

### Candidate C — 只读 Marzban DB 集成

**结论：`REJECTED`（本轮有 Candidate B 更优方案，不需要接受 Candidate
C 的额外风险）**。逐项分析：

- **schema 是否 public/stable**：Marzban 的 SQLAlchemy DB models
  （`app/db/models.py`）是内部实现细节，从未作为公开 API 契约
  发布/文档化，随时可能在任何版本升级时改变列名/表结构，没有任何
  官方稳定性承诺。
- **version coupling**：比 Candidate B 更紧密——Candidate B 只依赖
  一个已经被多处代码验证过的字符串公式（`{id}.{username}`），
  Candidate C 依赖整张 `users` 表的完整列结构。
- **locking/concurrency/consistency after create user**：需要
  处理"本仓库创建用户后，Marzban 事务是否已经 commit、本仓库能否
  立即读到这一行"的竞态，Candidate B 因为直接用 Marzban 自己的
  HTTP API 响应（该响应本身就代表事务已完成），没有这个问题。
- **DB path/credential exposure**：需要额外管理一个数据库连接
  凭据（无论 SQLite 文件路径还是 MySQL 连接串），这本身是一个新增
  的、独立于现有 `AccountingProvider` HTTP 凭据模型的攻击面。
- **security blast radius**：一旦这个只读连接的凭据泄漏或权限
  配置错误，暴露的是整张用户表（包括其它客户的数据），而不是
  Candidate B 那样只多暴露一个衍生字段。
- **部署复杂度**：需要保证本仓库的服务和 Marzban 的数据库在网络
  拓扑上互通，且要应对 Marzban 未来可能更换数据库后端（例如从
  SQLite 迁移到 MySQL）导致连接方式必须跟着改变。

**结论**：Candidate C 在每一个维度上风险都比 Candidate B 更高，且
两者解决的是同一个问题——本 ADR 不接受"两个方案都试"的组合，选择
风险更低的 Candidate B。

## Part D：能否完全消除 per-user email routing 依赖（架构 sanity check）

调研 Xray 是否存在不依赖 Marzban 内部 `{id}.{username}` 复合 email
的路由拓扑，例如给每个客户一个独立 inbound/端口，通过监听端口而不是
`user` 字段区分客户。**结论：`REJECTED`，不满足既有约束**：

- ADR-014 已经把"inbound protocol/listen/port 归静态模板"确立为
  Accepted 结论——所有客户当前共享同一个 Reality inbound（同一个
  `listen`/`port`），这是刻意的、已接受的设计，不是尚待决定的空白。
  给每个客户一个独立 inbound/端口意味着推翻这条已接受的结论，需要
  重新打开 ADR-014，而不是本 ADR 的研究范围。
- **inbound/端口数量膨胀**：客户数量增长意味着 inbound 数量同比
  增长，Xray 单进程管理数千个 inbound 的运维/资源开销和现有"共享
  一个 Reality inbound，靠 `user` 字段路由"的设计目标（用一个 TLS
  伪装端口服务所有客户）直接冲突。
- **Marzban 用户生命周期仍然必须可维护**：Marzban 自己的用户/proxy
  模型是围绕"一个 inbound 多个 client"设计的（`inbounds[].settings.
  clients`），强行改成"一个客户一个 inbound"需要同时改变 Marzban
  自己的运行假设，这已经超出"本仓库如何路由"的范围，变成"如何
  重新设计 Marzban 的部署拓扑"，不是本仓库能够/应该单方面决定的。
- **客户端可伪造风险**：如果改用连接来源（如源 IP/端口）而不是
  Xray 认证后的 client identity 做路由依据，源 IP 在多数部署场景
  下不能唯一稳定标识一个客户（NAT、动态 IP、客户端切换网络），且
  比 `user` 字段更容易被伪造/无法认证，风险更高，不是更优替代。

**记录为已否决的替代方案**，不进一步展开设计。

## Decision 3: SELECTED — Candidate B

**最终方案**：为 pinned Marzban 镜像维护一个最小化 source patch，
让 `UserResponse`（进而 `create_user`/`get_user`/`list_users` 等
所有复用这个 schema 的响应）新增一个 `routing_principal: str` 字段，
值等于 `f"{marzban_db_user_id}.{username}"`（与 `operations.py`
现有内部计算公式逐字节一致）。真实的 Marzban-backed
`AccountingProvider` 实现读取这个字段，本仓库的编排/持久化层把它
当作 `GatewayRouteBinding.gateway_principal` 唯一合法的写入来源。

### 精确定义

- **`gateway_principal`**：等于 Marzban 打了补丁的
  `UserResponse.routing_principal` 字段值，形如 `"42.sub-123"`——
  这就是 Xray `routing.rules[].user` 实际匹配的 client email，
  语义从"accounting username"或"Webshare 出口租户 id"（历史遗留
  错误值）正式改为"Xray routing principal"。
- **`accounting_user_id`**（`Subscription.accounting_user_id`）：
  维持现状，继续存 accounting username（`request.username`，形如
  `"sub-123"`）——**这是一个独立的、故意保留的语义**，用于
  accounting 侧的用户识别/usage 查询（`get_usage`/`set_quota` 等
  `AccountingProvider` 方法目前的签名都是按 `username` 调用，不需要
  改动），和 `gateway_principal`（Xray 侧路由匹配键）是两个不同
  bounded context 的标识符，不应该合并成一个字段。
- **谁生成 routing principal**：Marzban 自己（打补丁后的
  `UserResponse` 序列化逻辑），不是本仓库计算/猜测。
- **谁返回它**：真实的 Marzban-backed `AccountingProvider.
  create_user()` 实现——调用 Marzban `POST /api/user` 之后，从
  响应体读取 `routing_principal` 字段。
- **谁持久化它**：`SqlAlchemyProvisioningState.
  ensure_gateway_route_binding()`，通过下方的编排改动获得这个值
  作为参数。

### DTO / 编排数据流改动（方向，不在本 ADR 实现）

1. `AccountUserDTO` 新增字段：`routing_principal: str | None`（真实
   Marzban 实现填充；`MockAccountingProvider` 可以填充一个确定性的
   mock 值，例如 `f"mock-{len(self.users)}.{username}"`，用于测试
   编排数据通道本身是否被正确传递，不代表真实 Marzban 语义）。
2. `ProvisioningService.provision()` 的 `CREATE_ACCOUNTING_USER`
   步骤（当前第 195-197 行）**必须保留返回值**：
   `account = self.accounting.create_user(...)`，不再丢弃。
3. `APPLY_GATEWAY` 步骤调用 `self.state.desired_routing_state(...)`
   时，需要能够访问上一步拿到的 `account.routing_principal`——
   具体是把它加进 `desired_routing_state()` 的参数列表，还是通过
   `ProvisionRequest`/新的中间结构传递，留给 Phase 2B 详细设计，
   本 ADR 只确定"这个值必须能从第 6 步流到第 7 步"这个数据流方向。
4. `SqlAlchemyProvisioningState.ensure_gateway_route_binding()`
   的调用点（当前 `desired_routing_state()` 内的
   `self.ensure_gateway_route_binding(tenant.tenant_id, endpoint)`）
   改为传入 `account.routing_principal`（如果账户创建阶段没有拿到
   这个字段——例如 mock provider 未实现、或真实 provider 因为
   Marzban 一侧补丁失效而拿不到——按下方"provider/API 不返回
   principal 时怎么办"处理，不允许静默回退到 `tenant.tenant_id`
   或 `request.username`）。

### provider/API 不返回 principal 时怎么办

**fail closed，不静默回退**：如果真实 `AccountingProvider.
create_user()` 返回的 `AccountUserDTO.routing_principal` 是
`None`/空字符串（无论因为补丁在某次镜像升级后失效、还是任何其它
原因），`APPLY_GATEWAY` 步骤必须直接失败（等价于现有
`RuntimeError`/`gateway candidate validation failed` 路径），
触发已有的 `self.accounting.disable_user(request.username)` +
`_alert("GATEWAY_APPLY_FAILED", ...)` 补偿逻辑，**不允许**用
`request.username`/`tenant.tenant_id` 等错误值当 fallback 继续
往下走——这正是 Candidate B 需要 Phase 2B 加一个"补丁是否仍然生效"
CI 检查的直接原因：让这类失败尽量在部署前被发现，而不是在每次
provisioning 请求时才 fail closed。

### 既有数据 reconciliation（本轮相比 ADR-015 有实质性变化）

**`CONFIRMED` 可行，不再是 `BLOCKED`**——这是本 ADR 相比 ADR-015 的
关键推进：因为 `routing_principal` 是 Marzban 自己数据库里已经
存在的 `id`+`username` 的纯衍生值（不需要任何新数据、不需要客户
重新走一次开通流程），只要 Marzban 镜像打上 Candidate B 的补丁，
**对任何历史 active binding，只需要用它的 accounting username
调用一次打了补丁的 `GET /api/user/{username}`，就能拿到正确的
`routing_principal`**，不存在"本地 DB 算不出目标值"这种结构性
障碍（这一点和 ADR-015 时"目标值依赖 Marzban 内部 id，而本仓库
从未持久化过它"的判断不同——本方案不需要本仓库持久化过 id，只需要
在 reconciliation 时临时查询一次）。

**Reconciliation 设计方向**（沿用 ADR-015 已确定的通用 fail-closed
要求，具体目标值来源本轮补全）：

1. 全量枚举 `GatewayRouteBinding.enabled = True AND released_at
   IS NULL` 的行，通过 `subscription_id` JOIN `Subscription` 拿到
   `accounting_user_id`（accounting username）。
2. 对每一行调用一次打了补丁的 Marzban `GET /api/user/{username}`
   （只读，admin token 鉴权，不修改任何 Marzban 状态），取
   `routing_principal`。
3. **全量 preflight**：先对所有行完成第 2 步查询，任何一行查询
   失败（网络错误、用户在 Marzban 侧不存在、`routing_principal`
   字段缺失）都记录下来但不中止查询循环本身（查询是只读操作，
   不涉及写入顺序问题）；查询阶段全部完成后，如果**存在任何**
   失败记录，整个 migration **中止，不做任何 UPDATE**——不允许
   "跳过失败的行，更新其它行"。
4. 全部行查询成功后，检测目标 `routing_principal` 值之间、以及和
   `active_gateway_principal` 唯一约束的潜在冲突，冲突同样导致
   整体中止、不做任何 UPDATE。
5. 全部校验通过后，一次性批量 UPDATE 所有行的 `gateway_principal`
   为查询到的 `routing_principal`。
6. migration 必须幂等：重复执行时，已经等于目标值的行不产生
   无意义写入；如果 Marzban 一侧数据在两次执行之间发生变化（正常
   业务变化，不是异常），第二次执行应该按新查询结果再次收敛，这
   仍然符合"相同输入产生相同结果"的幂等定义。

**这依赖 Candidate B 的补丁已经部署并对存量用户生效**——补丁本身
不需要 Marzban 重新创建用户（`routing_principal` 是从已有
`dbuser.id`/`dbuser.username` 现算的，不是创建时才写入的新列），
所以对存量用户同样立即可用，不需要额外的"存量用户迁移"步骤。

### DB schema 影响

- **`GatewayRouteBinding.gateway_principal`**：**不需要新列**，
  只是写入的值语义变化（从"Webshare 租户 id"/错误的 accounting
  username 变成正确的 Xray routing principal），沿用现有列。
- **`AccountUserDTO`**：这是一个 dataclass DTO，不是 DB 表，新增
  `routing_principal` 字段不涉及 migration。
- **不需要新表存储 Marzban DB user id**——因为方案不要求本仓库
  持久化这个 id，只在 reconciliation 时临时查询、且真实 provider
  每次 `create_user()` 时都能直接拿到，不需要缓存。

### Failure / fail-closed 语义汇总

- 补丁失效/字段缺失 → `APPLY_GATEWAY` fail closed（见上）。
- Reconciliation 查询任一行失败 → 整体 migration 中止，不做任何
  UPDATE（同 ADR-015 已确定的通用规则）。
- Reconciliation 目标值冲突（唯一约束）→ 整体 migration 中止。
- Marzban 镜像升级导致补丁无法干净应用 → Phase 2B 要求的 CI 检查
  必须在这种情况下 fail closed，阻止发布新镜像，而不是静默跳过
  打补丁、让 `routing_principal` 字段从响应里消失。

### Phase 2B gate 后果

**Decision 3 从 `BLOCKED` 变为 `SELECTED`——`ADR-014` 第 5 条
（"Phase 2B 开始前必须先选定 route identity 收敛方向"）的前提条件
现在已经满足**。这意味着：

- `ADR-014` 第 5 条的门槛本身**已经解除**——不是本 ADR 去 supersede
  它，而是它设定的前提条件（选定收敛方向）现在被满足了。
- **但这不等于"可以立即开始 Phase 2B 实现"**——Phase 2B 实现 PR
  仍然需要单独走完整的实现/测试/审查流程，包括：真正编写并验证
  Candidate B 的 source patch、真实 Marzban-backed
  `AccountingProvider` 实现、`AccountUserDTO`/编排改动、
  reconciliation migration、`XrayFileProvider.apply()` 的异常
  兜底加固（ADR-015 已记录的独立缺口，不受本 ADR 影响，仍然是
  Phase 2B 的前提）、以及 ADR-015 Decision 4 矩阵里其余 `YES`
  条目。本 ADR 只解除"能不能开始规划 Phase 2B 实现 PR"这个闸门，
  不代表这些实现工作已经完成或可以跳过验证直接上线。

## 安全影响

- Candidate B 的补丁把 Marzban 内部已经在用、只是没有对外暴露的
  衍生值多序列化一次，不引入新的敏感数据面；补丁维护责任明确归属
  本仓库团队，需要在 `infrastructure/marzban/` 下有清晰的文档和
  CI 校验，防止补丁静默失效后系统退化为使用错误的 fallback 值
  （本 ADR 已经在"provider/API 不返回 principal 时怎么办"一节明确
  禁止这种 fallback）。
- Reconciliation migration 需要一次性对所有历史 active binding 发起
  只读 Marzban API 调用，这些调用使用现有 admin 凭据、走现有鉴权
  路径，不引入新的凭据类型，但需要在 Phase 2B 实现时评估调用频率/
  限流，避免对 Marzban 造成过大瞬时负载（具体节流策略留 Phase 2B
  设计，本 ADR 只记录这个考虑点）。
- 否决 Candidate C 直接消除了"额外维护一个数据库连接凭据、扩大
  凭据泄漏后的影响半径"这个风险，是本 ADR 相比继续尝试其它方案的
  一个安全收益。

## 考虑过的替代方案

1. **Candidate A（升级 Marzban 版本）**：exact-source 证明从
   v0.8.4 到当前 `master` 都没有解决 DB id 暴露问题，纯升级不能
   成立，否决。
2. **Candidate C（只读 Marzban DB 集成）**：逐项风险分析（schema
   稳定性、version coupling、并发/一致性、凭据暴露面、部署复杂度）
   均劣于 Candidate B，否决。
3. **消除 per-user email 依赖的替代 Xray 拓扑（per-customer
   inbound/端口）**：与 ADR-014 已接受的"共享单一 Reality inbound"
   结论冲突，且引入 inbound/端口数量膨胀和 Marzban 自身运行假设的
   连锁改动，否决。
4. **向 Marzban 上游贡献这个字段、等待官方合并后再采用**：作为
   Candidate B 的一个可能后续动作记录（如果上游接受，未来可以
   移除自维护的 patch），但不能作为当前方案的前提条件——上游是否/
   何时接受不可控，本仓库不能把 Decision 3 的解锁时间绑定在一个
   无法控制的外部决定上。
5. **继续保持 `BLOCKED`，等待 Marzban 官方某天暴露这个字段**：
   被否决，理由是 Candidate B 提供了一个当前就能落地、风险可控、
   维护成本低的路径，没有必要无限期等待一个没有已知计划的上游变更。

## 重新评估条件

- 如果 Marzban 上游某个未来稳定版本正式在 `UserResponse`（或等价
  受支持的公开 API）里原生暴露 DB id 或 `routing_principal` 等价
  字段，应重新评估是否可以移除本仓库自维护的 Candidate B 补丁，
  改用官方原生字段（迁移路径：先并行验证官方字段和补丁字段在同一
  批用户上产出一致的值，再切换，不应该在没有交叉验证的情况下
  直接信任新字段）。
- 如果 Phase 2B 实施 Candidate B 的过程中发现某次 Marzban 镜像
  升级导致补丁无法干净应用，且短期内无法修复补丁本身，应该按
  "provider/API 不返回 principal 时怎么办"一节 fail closed，同时
  这个事件本身构成重新评估 Candidate B 长期可持续性的触发条件——
  如果这类补丁失效频繁发生，可能需要重新打开本 ADR，评估是否要
  转而尝试向上游贡献这个字段或重新评估 Candidate C。
- 如果未来 Marzban 完全改变 Xray client identity 的构造方式（不再
  是 `{id}.{username}`），Candidate B 的补丁公式必须同步更新，
  且需要验证 `operations.py` 里增量 CRUD 路径和补丁计算路径是否
  仍然给出一致的值——这个校验应该作为 Phase 2B 的护栏测试之一
  （对同一个测试用户，比较 mock/真实 Handler API 增量路径产生的
  client email 和补丁字段返回值是否一致）。
