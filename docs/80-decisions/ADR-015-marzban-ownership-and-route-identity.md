# ADR-015: 关闭 Phase 2B 前置决策——Marzban 运行时归属、provider 分类、
route identity 收敛方案

- 状态: 已接受
- 日期: 2026-09-11
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

A. Marzban（当前 Compose 锁定版本 `gozargah/marzban:v0.8.4`）在共享
   `xray_config.json` 拓扑下，是否/如何管理 `inbounds[].settings.clients`。
B. Marzban 在本仓库架构里最终应归为 `AccountingProvider`、
   `TransportProvider`，还是拆成两个 adapter；解决 `ADR-013` 与
   `config.py` 的现有矛盾。
C. `GatewayRouteBinding.gateway_principal`/`Subscription.
   accounting_user_id` 的收敛方案（ADR-014 的候选 A/B/C）。

本 ADR 只读研究这三个问题，产出明确结论，不实现任何代码/schema/
migration 改动——这些留给真正的 Phase 2B 实现 PR。

## Part A：Marzban v0.8.4 的 client lifecycle

**方法**：只读检索 `github.com/Gozargah/Marzban` 在 `v0.8.4` tag 下的
公开源码（`raw.githubusercontent.com/Gozargah/Marzban/v0.8.4/...`）和
公开文档/Issue，未连接任何真实 Marzban 实例，未使用任何真实凭据。

### 逐项结论

1. **Marzban 如何读取 `/code/xray_config.json`？**
   `CONFIRMED`——Marzban 用环境变量 `XRAY_JSON`（默认值
   `"xray_config.json"`，`.env.example` 里写着
   `# XRAY_JSON = "xray_config.json"`）指定这个文件路径，在 Marzban
   自己进程启动/重建 `XRayConfig` 对象时读取一次，作为初始骨架（含
   `inbounds`/`outbounds`/`routing`）。这正是本仓库
   `compose.transport.yml` 挂载到 `marzban` 容器 `/code/xray_config.json`
   的同一个文件。

2. **user/client 数据的 authoritative source 是什么？**
   `CONFIRMED`——是 **Marzban 自己的数据库**（本仓库部署拓扑里就是挂载的
   `db.sqlite3`），不是 `xray_config.json`，不是内存，也不是本应用的
   MySQL。证据：`app/xray/config.py::include_db_users()` 方法体内直接
   `with GetDB() as db: db.query(db_models.User.id, db_models.User.
   username, ..., db_models.Proxy.settings, ...)`——查询的是 Marzban 自己
   ORM 模型 `db_models.User`/`db_models.Proxy`，这套模型对应的正是
   Marzban 自己管理的 SQLite 库。

3. **创建/disable/update Marzban user 时会不会修改
   `xray_config.json`？会不会重新生成文件？只调用 Xray API？还是有
   内部 runtime config？**
   `CONFIRMED`——都不是"修改磁盘上的 `xray_config.json` 文件"。真实机制：
   Marzban 内部维护一个 **内存中** 的 `XRayConfig` 对象；用户变化后调用
   `include_db_users()` 重新从数据库查询并在内存里重建
   `clients`（`clients.append(client)` 是全代码库里唯一"往 inbound 加
   client"的地方，只操作内存对象，不写文件）；然后通过
   `xray/core.py` 的 `restart()`（内部先 `stop()` 再 `start()`）把这个
   **内存对象序列化后的 JSON 直接写进 Xray 子进程的 stdin**（
   `subprocess.Popen([executable_path, "run", "-config", "stdin:"],
   stdin=subprocess.PIPE, ...)`，随后
   `self.process.stdin.write(config.to_json())`）——Xray 进程本身也是
   Marzban 用 `-config stdin:` 参数启动的，从标准输入读配置，不是从
   `/code/xray_config.json` 这个磁盘文件读。原始的
   `generated_config-debug.json` 调试文件（仅 `DEBUG` 模式下才写）是
   唯一涉及"写文件"的代码路径，且写的是另一个文件，不是原始
   `xray_config.json`。

4. **Marzban 是否把用户映射为 `inbounds[].settings.clients`？**
   `CONFIRMED`——是，就是上面第 3 点里 `include_db_users()` 的
   `clients.append(client)` 逻辑，按协议类型把数据库里的 `Proxy` 记录
   转换成 Xray inbound 的 client 对象，追加进对应 inbound 的
   `settings.clients` 列表（只在内存对象里）。

5. **共享的、bind-mounted 的 `xray_config.json` 会不会被 Marzban
   写回？**
   `CONFIRMED`（否定回答）——不会。搜索 `app/xray/config.py` 全文，
   没有任何代码把 `XRayConfig` 对象写回它最初读取的文件路径；唯一的
   "写文件"分支是 `DEBUG` 模式下的调试文件，路径也不同
   （`generated_config-debug.json`，不是 `xray_config.json`）。

6. **如果本仓库的 renderer 再次全量替换这个文件，会不会覆盖 Marzban
   管理的 clients？**
   `CONFIRMED`（不会覆盖"已经写入磁盘的 clients"，因为从第 5 点已知
   磁盘文件从来没有 clients）——但需要精确说明影响范围：
   - **对 Marzban 当前正在运行的 Xray 子进程无影响**：那个进程的配置
     是启动/上次重载时一次性通过 stdin 灌入的，之后不会再读磁盘文件。
   - **对 Marzban 下一次自己触发 `restart()`（例如管理员改了某个用户）
     有影响**：`restart()` 只是重新执行 `stop()`+`start()`，`start()`
     会重新用**当前内存里已经持有的** `XRayConfig` 对象（不是重新读磁盘
     文件）走 `include_db_users()` 再拼一次；但 Marzban **进程自身重启
     /重建**（不是内部的 xray-core `restart()`，是 Marzban 应用层进程
     整个重启，例如容器重启）时，会重新按 `XRAY_JSON` 指向的路径读取
     磁盘文件作为新的初始骨架——这时如果本仓库的 renderer 已经用新的
     `outbounds`/`routing` 覆盖了这个文件，Marzban 会用新骨架重建
     `XRayConfig` 对象，再重新 `include_db_users()` 注入它自己数据库里
     的 clients，最终效果是"骨架被本仓库更新了、clients 仍然由 Marzban
     自己的数据库正确重新注入"——不会丢失 clients，因为 clients 从来
     不依赖这个文件的历史内容存在。
   - 结论：**renderer 全量替换这个文件是安全的**，不会破坏 Marzban 管理
     的 client 数据，前提是 renderer 产出的 `inbounds` 结构本身没有把
     Marzban 需要的 inbound `tag` 改没了（`include_db_users()` 按
     inbound `tag` 匹配协议类型去注入 client，如果 renderer 改变或
     删除了 Marzban 依赖的 inbound tag，会导致对应协议的 client 注入
     失败——这是 Phase 2B 设计新期望态时需要保留的一个真实约束，记入
     "重新评估条件"）。

7. **Marzban user UUID/password/email 等认证材料的 canonical source
   实际在哪里？**
   `CONFIRMED`——Marzban 自己的数据库（`db_models.User`/`db_models.
   Proxy`，本仓库部署拓扑里对应挂载的 `db.sqlite3`），不是本应用的
   MySQL，不是 `Secret` 表，不是 `xray_config.json`。

8. **本应用是否应该自己管理这些 client fields？**
   `INFERENCE`（基于上述 CONFIRMED 事实的推论，不是直接读到的一句
   声明）——不应该。Marzban 已经完整拥有 client 生命周期管理（创建、
   禁用、认证材料生成、注入 Xray 运行时），本应用重复实现同一件事
   会产生两个互相不知情的 client 数据源，这正是 ADR-014 铁律第 1 条
   想避免的"多数据源"问题的另一种形式。本应用需要做的是**通过
   `AccountingProvider.create_user()`（真实实现调用 Marzban 的
   admin API）来驱动 Marzban 创建/管理这些 client**，而不是本应用自己
   往数据库或 Xray 配置里塞 client 记录。

### Decision 1

**Marzban v0.8.4 是否管理 Xray inbound clients？**

**`YES`**——通过它自己的数据库（`db_models.User`/`db_models.Proxy`）+
`include_db_users()` 在每次内部配置重建时动态注入，经 stdin 直接喂给
它自己管理的 Xray 子进程；从不写回本仓库挂载的 `xray_config.json`
文件本身。

## Part B：Marzban 的 provider classification

### 逐项证据

**`AccountingProvider` Protocol**（`backend/app/providers/base.py` +
ADR-011 扩展）需要：`create_user(username, quota_bytes, expire_at)`、
`disable_user(username)`、`set_quota`、`set_expire`、
`get_connection_links(username)`、`get_usage(username)`。

**`TransportProvider` Protocol**（ADR-011）需要：节点同步
（`sync_nodes`）、端点读取、容量读取、启停能力、`health_check()`。

**`Settings`（`backend/app/core/config.py`）里所有 `marzban_*` 字段**：
`marzban_base_url`、`marzban_admin_username`、`marzban_admin_password`、
`marzban_default_protocol`、`marzban_default_inbounds_json`、
`marzban_verify_tls`——这六个字段**逐一对应"调用 Marzban admin API 创建/
管理一个用户"所需要的信息**（服务地址、管理员登录凭据、新用户默认用什么
协议、默认挂哪些 inbound）。**没有任何一个字段是"节点列表/节点健康/
节点容量"这类 `TransportProvider` 契约需要的信息**。

**Part A 的证据**同样支持这一点：Marzban v0.8.4 的核心职责就是"管理
Xray 用户"（创建、认证材料生成、配额、到期、连接链接），这精确对应
`AccountingProvider` 的方法签名，和 `TransportProvider`"同步节点库存/
读取端点/容量"这类完全不同的职责没有交集。

**`ADR-013` 的表述**："Marzban 在新架构中的定位是 transport 层，健康
检查归属该层一致"——**这个表述和上面两组证据矛盾**：`marzban_*`
配置字段和 Marzban 实际产品职责（用户/账务管理）都指向 Accounting，
不是 Transport。`ADR-013` 做这个决定时给出的**真正理由其实是另一条**
（"`providers/base.py` 受铁律第 5 条约束，为单个健康检查端点修改核心
契约不合比例"——这条理由本身仍然成立，和 Marzban 该归哪一类无关），
"Marzban 定位是 transport 层"只是这条理由之外附带的一句表述，且这句
表述本身站不住脚——**这是需要 supersede 的具体一句话，不是要推翻整条
ADR-013**。

**方案 3（SPLIT）不成立的理由**：SPLIT 的前提是 Marzban 确实同时提供
两类互不重叠的能力，需要两个 adapter 分别接。但 Part A/上面的证据显示
Marzban v0.8.4 **完全没有**任何"节点库存同步/多节点容量读取"能力
——`TransportProvider` 契约描述的那类职责在本仓库里由完全不同的东西
（`transport/subscription.py::SubscriptionTransportProvider`，管理订阅
链接/节点列表同步，和 Marzban 毫无关系）覆盖。给 Marzban 拆出一个
"MarzbanTransportProvider"会是一个没有任何真实能力可实现的空壳类，
没有证据支持这么做，否决。

### Decision 2

**Marzban 的最终 provider classification：`ACCOUNTING`。**

理由：`marzban_*` 配置字段形状、Marzban v0.8.4 的真实产品职责（用户/
认证材料/配额/到期管理）、`AccountingProvider` Protocol 方法签名，
三者完全对应；`TransportProvider` 契约描述的节点库存/容量能力在 Marzban
产品里没有对应物。

**Supersede ADR-013 的具体表述**：`ADR-013`"Marzban 在新架构中的定位是
transport 层，健康检查归属该层一致"这一句判断被本 ADR **推翻并纠正**为
"Marzban 的定位是 accounting 层；`/admin/accounting/health` 复用
`TransportProvider.health_check()` 这个既有方法只是一个成本考虑下的
实现捷径（避免为单个健康检查端点修改 `providers/base.py`），不代表
Marzban 本身属于 transport 类别"。`ADR-013` 其余决定（废弃自动开通
开关、九步编排统一执行、`/admin/accounting/health` 底层调用哪个具体
方法这个**代码接线本身**）**不受影响、不需要改代码**——这条 supersede
只纠正一句被证明不准确的架构表述，不要求 Phase 2B 或任何后续 PR 修改
`admin_accounting_health()` 现有的调用方式，因为改这行代码本身没有
实际收益（重新给 `AccountingProvider` 加 `health_check()` 仍然要碰
`providers/base.py`，仍然不合比例）。

已在本 ADR 末尾的"约束"一节里，同时在 `ADR-013` 文件里补一条指向本 ADR
的 supersede 说明（只加一段，不删除、不重写 `ADR-013` 原文其余内容）。

## Part C：Xray route identity 收敛方案

### 候选对比（评估维度按用户要求逐一比较）

| 维度 | 候选 A：恢复 `gateway_principal` = accounting principal | 候选 B：`accounting_user_id` 持久化时机提前 | 候选 C：`sub-{order.id}` 定为正式契约 |
|---|---|---|---|
| DB 单一 source of truth | 好——`gateway_principal` 本身就是单一字段，写入值改对即可 | 好，但依赖跨编排步骤读一个字段 | 好——纯函数推导，不依赖任何持久化字段 |
| 首次开通时序 | **不受影响**——`ensure_gateway_route_binding()` 就在 `APPLY_GATEWAY`（第 7 步）内部写入，不依赖后续步骤 | 需要把 `accounting_user_id` 的写入从 `admin_confirm_payment`（编排全部返回后）提前到第 6 步之后、第 7 步之前 | **不受影响**——`order_id` 在下单时就已持久化 |
| rollback/compensation | 不增加新失败模式（沿用 `ensure_gateway_route_binding()` 现有 upsert 逻辑） | **新增**：第 7 步失败时，第 6 步已提前写入的 `accounting_user_id` 要不要回滚，需要新设计 | 不增加新失败模式（纯函数无副作用） |
| idempotency | 好——`ensure_gateway_route_binding()` 已经按 `subscription_id` upsert | 需要新增"避免重复提前写入"的判断 | 好——纯函数天然幂等 |
| retry | 简单——重新计算/重新调用即可 | 需要处理"部分提前写入后重试"的状态判断 | 简单——纯函数 |
| accounting user disable 语义 | 不受影响 | 不受影响 | 不受影响 |
| 现有 schema 复用 | **最好**——复用 `gateway_principal` 现有列，只改写入的值 | 复用 `accounting_user_id` 现有列，但改写入时机涉及跨层代码移动 | 不新增列，靠推导，等于把"字段"换成了"函数" |
| migration 需求 | 无 | 无 | 无 |
| 未来改用户名规则的兼容性 | **好**——`gateway_principal` 一旦写入即固定，后续改命名规则不影响已渲染的历史路由 | 好——`accounting_user_id` 同理一旦写入即固定 | **差**——如果 `sub-{order.id}` 这个推导公式将来改变，所有已经渲染过的历史候选配置里嵌入的旧值和新一次推导出的值会不一致，除非额外保留历史值（这其实是在重新发明"持久化"） |
| renderer 重新渲染同一订阅时能否拿到一致的值 | **好**——直接读已持久化的 `gateway_principal` | 好——直接读已持久化的 `accounting_user_id`（提前之后可用） | 只要推导公式不变就一致，公式一变就不一致（同上一条的弱点） |
| 最小改动范围 | **最小**——只需要把 `desired_routing_state()` 里
`self.ensure_gateway_route_binding(tenant.tenant_id, endpoint)` 改成
`self.ensure_gateway_route_binding(request.username, endpoint)` 一行 | 较大——需要在 `domain/provisioning.py` 或 `admin.py` 之间挪动持久化时机，涉及事务边界 | 较小——需要新增一个共享的推导函数并在两处调用，但要接受上面提到的"命名规则变更风险"这个长期隐患 |
| 测试难度 | **最容易**——现有集成测试已经在用 `"marzban-user-1"` 这类值调用 `ensure_gateway_route_binding()`，改动后这类测试直接验证契约 | 较难——需要新增编排/补偿场景的测试 | 中等——需要测试推导函数在两个调用点结果一致 |

### Decision 3

**Selected: `A`**——恢复 `GatewayRouteBinding.gateway_principal` 的
accounting-principal 语义，`desired_routing_state()` 调用
`ensure_gateway_route_binding()` 时改传 `request.username`（而不是
`tenant.tenant_id`）。

**Rejected: B**——同样能解决问题，但引入了新的跨编排步骤事务/补偿复杂度
（"提前持久化的字段在后续步骤失败时要不要回滚"），而候选 A 不需要这类
新设计，用更小的改动达到同样效果，没有理由选复杂度更高的方案。

**Rejected: C**——短期内同样能工作，但把"路由匹配键"的正确性长期绑定在
"一个从未改变过的字符串推导公式"这个假设上，一旦这个假设未来被打破
（例如产品需要允许自定义用户名、或者需要把 `sub-{id}` 换成更贴近人类
可读的账务用户名），所有历史渲染出的路由都会和新推导值不一致，而
这个不一致目前没有任何检测机制——候选 A/B 都用一个明确持久化的字段
规避了这个长期风险，A 的改动成本还更低，没有理由为了"不新增字段"这一个
好处（C 的唯一优势）而接受这个长期风险。

**候选 A 生效后 `Subscription.accounting_user_id` 是否还需要**：需要，
不冲突——它服务的是完全不同的读取路径（`admin_sync_usage()` 读它去调用
`AccountingProvider.get_usage()`），和 `gateway_principal`（服务 Xray
路由匹配）是两个不同用途的字段。两者的值理应相同（都等于同一个
`ProvisionRequest.username`），因为它们都从同一个 `request` 对象在同一次
`confirm_payment_and_provision()` 调用里派生——只要 Phase 2B 实现候选 A
时不改变 `request.username` 在整个编排过程中的稳定性（现状如此，
`ProvisionRequest` 是 `frozen=True` 的 dataclass，同一次调用内不会变），
两个字段自然保持一致，不需要额外同步机制。

## Reality ownership：无 NEW EVIDENCE，维持 ADR-014 结论

本次研究过程中没有发现任何推翻 ADR-014 已 Accepted 的 Reality
ownership 结论（inbound protocol/listen/port → 静态模板；Reality
`dest`/`serverNames` → 环境变量；Reality `privateKey`/`shortIds` →
DB/Secret；routing/outbounds/route identity → DB；运行时 config →
只读校验/回滚/漂移来源）的证据，本 ADR 不重新打开这些问题。

## Decision 4：Phase 2B implementation matrix

| 改动类型 | 需要？ | 最小改动说明 |
|---|---|---|
| `DesiredRoutingState` DTO change | **YES** | 需要扩展以表达完整 outbound 连接细节（host/port/protocol/凭据引用，参照 `render_xray_routes.py` 已验证的字段形状）；route identity 部分**不需要新增字段**（候选 A 复用现有 `user_routes` 的 key，只是调用方传入的值从 `tenant.tenant_id` 改为 `request.username`） |
| Settings change | **YES** | 新增/确认 `XRAY_REALITY_DEST`/`XRAY_REALITY_SERVER_NAME`（ADR-014 已定方向）读取路径；不需要为 Marzban classification 改动新增字段（`marzban_*` 字段已存在，只是本 ADR 澄清了它们该被归进哪个 provider 分类，不改字段本身） |
| Secret persistence change | **YES** | Reality `privateKey`/`shortIds` 需要走 `Secret` 表（ADR-014 已定方向，本 ADR 不变） |
| DB schema/migration | **NO** | Part A/B/C 均未发现需要新增表或列——`gateway_principal`（候选 A）、`accounting_user_id`（已有列）都复用现状；Reality 持久化用 `Secret` 表（既有机制，不需要新表结构） |
| provisioning orchestration change | **YES，但范围很小** | 只需要修改 `SqlAlchemyProvisioningState.desired_routing_state()` 里 `ensure_gateway_route_binding()` 的调用参数（候选 A），不涉及编排步骤顺序、事务边界或补偿逻辑的改动 |
| query/adapter change | **YES** | 期望态查询需要参照 `render_xray_routes.py::_active_routes()` 的 JOIN 逻辑扩展到 provider 抽象层 |
| Xray renderer change | **YES** | `XrayFileProvider.render()` 需要产出完整 outbound；`ops/gateway/render_xray_routes.py` 需要把 Reality `dest`/`serverNames` 的读取路径从"模板优先、env 兜底"改为"只读 env"（ADR-014 已定方向） |
| preservation/validation change | **YES** | 按 ADR-014"合法删除语义"重写：校验候选与本次期望态一致 + 安全不变量，不是"候选是不是运行时配置超集"；且 Phase 2B 设计新期望态时必须保留 Part A 第 6 点发现的约束——渲染出的 `inbounds` tag 不能破坏 Marzban `include_db_users()` 依赖的协议匹配 |

## 约束

1. 本 ADR 不实现任何代码、schema、迁移改动——纯粹是研究结论和决策记录。
2. Marzban v0.8.4 相关的 8 项结论均标注 `CONFIRMED`/`INFERENCE`，附带
   具体源码文件/函数证据；没有任何一项凭产品常识猜测。
3. 本 ADR **supersede** `ADR-013` 里"Marzban 在新架构中的定位是
   transport 层，健康检查归属该层一致"这一句具体表述，理由见 Part B；
   `ADR-013` 其余决定（废弃自动开通开关、九步编排统一执行、
   `/admin/accounting/health` 具体调用哪个方法这个代码接线）不受影响，
   不需要改代码。`ADR-013` 文件本身补一段指向本 ADR 的 supersede 说明，
   不重写其原文。
4. Decision 3（候选 A）在 Phase 2B 实现时，改动范围严格限定为
   `SqlAlchemyProvisioningState.desired_routing_state()` 里
   `ensure_gateway_route_binding()` 的调用参数，不得借此顺便重构
   `GatewayRouteBinding`、`ensure_gateway_route_binding()` 的其它逻辑。
5. Part A 第 6 点发现的约束（renderer 产出的 inbound tag 不能破坏
   Marzban 的协议匹配）必须写进 Phase 2B 的验收标准，不能只停留在本
   ADR 里。

## 考虑过的替代方案

1. **继续把 Marzban 归为 transport（维持 ADR-013 原表述）**：与
   `marzban_*` 配置字段形状和 Marzban 真实产品职责证据矛盾，否决。
2. **Marzban SPLIT 成两个 adapter**：没有证据支持 Marzban 提供任何
   `TransportProvider` 契约描述的节点库存/容量能力，会产出一个没有
   真实实现内容的空壳 transport adapter，否决。
3. **候选 B（提前 `accounting_user_id` 持久化时机）**：可行但引入不
   必要的编排/事务复杂度，候选 A 用更小改动达到同样效果，否决。
4. **候选 C（`sub-{order.id}` 正式契约化）**：短期可行，但把路由正确性
   长期绑定在一个字符串推导公式永不改变的假设上，候选 A 没有这个长期
   风险且改动更小，否决。

## 安全影响

- Part A 的结论（Marzban 完整拥有 client 生命周期，本应用不应重复
  管理）降低了"两个数据源互相不知情导致 client 状态不一致"的风险——
  这是一个正面的安全影响，确认了 ADR-014 铁律第 1 条精神在 inbound/
  client 这部分的正确落地方式：不是"本应用自己管理 client 并写入
  DB"，而是"通过 `AccountingProvider.create_user()` 驱动 Marzban 自己
  管理，本应用不重复存储认证材料"。
- Decision 3 选定候选 A 之后，`GatewayRouteBinding.gateway_principal`
  会开始存储 accounting username（此前存储的是 Webshare 出口租户
  ID）——这是一个字段语义变化，Phase 2B 实现时需要确认没有任何现有
  代码路径依赖这个字段当前存储的是 tenant id（已核实：`render_xray_
  routes.py` 把它当 Xray `"user"` 匹配值使用，这正是它应该存的值，
  变更后这条路径的行为会变得*正确*，不是引入新风险）。

## 重新评估条件

当 Phase 2B 实际实现候选 A 时，如果发现 `GatewayRouteBinding.
gateway_principal` 在其它未被本次研究覆盖的代码路径里被依赖为
"Webshare 出口租户 ID"这个旧语义，需要重新评估候选 A 的改动范围，
可能需要新增一个独立字段而不是复用 `gateway_principal`——本 ADR
基于当前已读到的代码路径（`ensure_gateway_route_binding()`、
`render_xray_routes.py`、集成测试）做出候选 A 的选择，如果 Phase 2B
实现时发现新的依赖方，应回到本 ADR 补充记录，不要在实现 PR 里悄悄
改变已经 Accepted 的决策而不留痕迹。
