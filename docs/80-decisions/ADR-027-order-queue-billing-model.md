# ADR-027: 订单队列计费模型（单一价目表 + 先到先算）

- 状态: **已接受（Accepted）**
- 日期: 2026-09-18（同日接受。User 就套餐档位与加购语义拍板后，将第 7 条
  `RENEWAL`/`ADDON` 是否合并的判断全权授权给 Claude Code；核对代码后按
  下方 7.1–7.4 作出决定，并附带发现了 7.3 那条已可达的缺陷。）
- 决策范围: `backend/app/domain/ordering.py` 的
  `BillingOrderType` / `apply_renewal` / `apply_upgrade` / `apply_addon`、
  `UsagePeriod` 的生命周期与滚动、以及每次滚动对账务 provider 的写入。
  不改变 ADR-017 的相位切分、ADR-018 的 `AccountingCreateEffect` 分类、
  ADR-026 的补偿失败所有权契约——本 ADR **复用**它们。
- 触发来源: 2026-09-18 User 拍板套餐档位与加购语义。

## 背景：现状是零实现

核对当前 `main`，不是从文档推断：

- `apply_renewal` / `apply_upgrade` / `apply_addon` 在
  `backend/app/api/admin.py:159-169` **三个全部是 stub**，一律抛
  `ValueError("Non-purchase billing is not handled by provisioning")`。
  只有 `PURCHASE` 走得通。
- **周期滚动代码零行。** 开通时在 `admin.py:134-145` 创建第一个
  `UsagePeriod`，此后没有任何代码关闭它或开启下一个。
  `apply_expiry_policy()` 的 docstring 明确写着
  "without touching usage-period boundaries"。
- `Order.addon_bytes` 与 `Settings.addon_*_price` 编码的是**另一种模型**
  （把字节加进当前周期、不动到期日）。`addon_*_price` 三个字段没有任何代码
  读取，已在本 PR 删除。

## 决策

### 1. 只有一张价目表

四档，固定 30 天周期，CNY：

| plan_code | 流量 | 价格 |
|---|---|---|
| `PLAN_50GB` | 50 GB | 30 元 |
| `PLAN_100GB` | 100 GB | 50 元 |
| `PLAN_200GB` | 200 GB | 80 元 |
| `PLAN_500GB` | 500 GB | 120 元 |

**不存在独立的加购 SKU，也不存在加购专属价格。** 客户想要更多流量，就是
再买一单，从同一张表里选。`backend/app/catalog.py` 是这张表的唯一事实源
（ADR 无关的部署改价仍走 `Settings`）。

### 2. 订单排队，一个订阅串起全部订单

一个 `Subscription` 对应一条**有序的订单队列**。每个已付款订单对应一个
`UsagePeriod`。任一时刻只有一个周期是 `ACTIVE`——这一点**已经由数据库
强制**：`UsagePeriod.active_subscription_id` 是 computed unique 列
（`CASE WHEN status = 'ACTIVE' THEN subscription_id ELSE NULL END`）。
排队中的周期不是 `ACTIVE`，因此不占用该唯一键。

#### 入队时机与队列语义（实现者最容易猜错的一段）

- **何时入队**：在**付款确认**时（人工确认收款之后），立即创建一个
  `QUEUED` 状态的 `UsagePeriod` 并挂到该订阅。不是下单时，不是激活时。
- **入队对当前周期零影响**：排队不改变当前周期的 `quota_bytes`、
  `period_end`，不写账务 provider，不碰 token，不触发任何运行时变更。
  客户买完之后在当前周期内**看不到任何变化**，这是预期行为，前端应当
  明确展示"已购待生效"，否则会被当成没到账。
- **可以排多单**：客户可以连续买多单，全部按**付款确认时间 FIFO** 排队。
  不做数量上限。
- **可以混档**：队列中各单的档位互相独立，允许 50GB 后面排一个 500GB。
- **激活是唯一的出队时机**：只有当前周期因额度耗尽或时间到期而结束时，
  队首才出队变成 `ACTIVE`。没有任何其他路径可以提前激活排队周期。

这要求 `UsagePeriodStatus` 从当前的 `ACTIVE`/`CLOSED` 扩展出一个
`QUEUED`（见「计划 schema」）。

### 3. 先到先算：两个结束条件等价

当前周期结束的触发条件有两个，**先到者生效**：

1. `used_bytes >= quota_bytes`（额度耗尽），或
2. `now >= period_end`（时间到期）。

两者在语义上**完全等价**，都记为"本单结束"。结束后：

- 队列里有下一单 → 立即激活它；
- 队列为空 → 按既有 `apply_expiry_policy()` 进入 `GRACE`，再到 `EXPIRED`。

**`GRACE` 只在队列为空时才进入。** 有排队订单时直接激活下一单，不经过
宽限期——宽限期的目的是"给还没续费的客户一点缓冲"，客户已经付过钱时
让他先等 24 小时是错的。

### 4. 每个排队订单激活时起算自己的 30 天

新周期的 `period_start = 激活时刻`，`period_end = 激活时刻 + 30 天`
（`Plan.duration_days`，由 `ck_plan_fixed_30_day_duration` 强制为 30）。

**不继承上一单的剩余时间。** 这是"再买一单"而不是"给当前单补额度"的必然
结果，也是 User 的选择。

定价上这不构成套利：四档的每 GB 单价分别是 ¥0.60 / ¥0.50 / ¥0.40 / ¥0.24，
**档位越大越便宜**，所以"用小档反复刷新 30 天"在每 GB 成本上始终劣于直接
买大档。若将来新增档位，必须保持这个单调性，否则本结论失效。

### 5. 未用完的额度在周期结束时作废

周期因时间到期而结束时，剩余 `quota_bytes - used_bytes` **不结转**到下一
周期。这是 User 明确选择的策略（"直接把这个月的订单清空"）。

这是面向客户的条款，不是实现细节：**必须在下单页与订阅详情页写明**，否则
客户会认为流量应当累积。

### 6. 订阅链接跨订单保持不变

`Subscription.token_hash` 挂在订阅上，不在 `Order` 也不在 `UsagePeriod` 上
（`models/subscription.py:70`）。因此订单滚动**不改变订阅链接**，客户端
自行刷新即可拿到新周期。这一点现有数据模型已经成立，本 ADR 只是确认它是
刻意的契约，后续实现不得把 token 下放到订单或周期层。

### 7. `RENEWAL` / `ADDON` / `UPGRADE` 收敛为同一个操作

在"单一价目表 + 订单排队 + 先到先算"之下，"续费""加购""升级"是**字面意义上
同一个操作**：从同一张表买一档，排进队列，等当前单结束后顶上。三者的差别
只在客户的动机（快到期 / 快用完 / 想换档），而动机不产生任何技术或计费差异。
保留三条行为相同的代码路径，唯一确定的结果是它们迟早会漂移——其中一条拿到
bug 修复，另外两条没有。

因此 `BillingOrderType` 收敛为两个**有效**类型：

| 类型 | 含义 |
|---|---|
| `PURCHASE` | 首单。创建 `Subscription` + 首个 `UsagePeriod` + 账务用户 + token |
| `RENEWAL` | 后续单。在**已有**订阅上追加一个排队周期，**档位可以是四档中任意一档**，不动 token、不新建账务用户 |

`ADDON` 与 `UPGRADE` 不再使用。枚举成员**保留不删**（历史订单行可能已经
写入这些值），但 `apply_paid_billing_change()` 对它们 fail closed。

#### 7.1 `RENEWAL` 这个名字保留，但语义被重新定义

`orders.order_type` 是 `String(32)`，直接存枚举的字符串值。把它改名成更中性的
名字（例如 `ADDITIONAL`）需要对存量行做数据迁移，而收益纯粹是命名上的，
因此**不改**。

代价是名字与语义有偏差：`RENEWAL` 现在的含义是**"已有订阅上的后续订单"**，
而**不是**"按原档位续费"。这是刻意接受的债务，在此写明以免后来的实现者
按字面理解加回档位锁。

#### 7.2 必须解除现有 `renew` 端点的档位锁

核对当前 `main`：`POST /subscriptions/{id}/renew`
（`backend/app/api/subscription.py:55-84`）**已经存在**并创建 `RENEWAL` 订单，
但它把档位硬锁成订阅当前的 `plan_id`（`current_plan = db.get(Plan, subscription.plan_id)`），
调用方无法选档。该端点的 docstring 自陈是
"Create only the renewal order branch from the legacy billing behavior"——
这是**从旧系统原样搬来的行为，不是本项目设计的决定**。

档位锁与本 ADR 冲突：单一价目表之下，客户用完 50GB 后想直接上 200GB，
应该就是买一单 200GB、排队、下一周期生效。这同时也是"升级"的全部含义，
`UPGRADE` 因此不再需要独立存在。

**S04-B2-B 以外的实现任务（TASK-S07）必须解除这个锁**，让调用方从四档里选。
这是对一个已上线端点的行为变更，PR 描述里必须明确写出。

#### 7.3 已发现的真实缺陷：续费订单当前无法被收款确认

`admin_confirm_payment`（`backend/app/api/admin.py:687-689`）在进入任何计费
逻辑之前就拒绝一切非 `PURCHASE` 订单：

```python
order_type = BillingOrderType(str(order.order_type))
if order_type is not BillingOrderType.PURCHASE:
    raise ValueError("Only purchase orders are handled by provisioning")
```

而 `/subscriptions/{id}/renew` 却可以正常创建 `RENEWAL` 订单。两者结合的结果是：
**客户今天就能点出一个永远无法被确认收款的续费订单。** 这不是"功能没做完"，
是一条已经可达的死路——订单会停在 `PENDING`/`UNPAID`，管理员在后台确认时
收到 `ValueError`。

TASK-S07 必须同时修掉这个缺口：`admin_confirm_payment` 要把 `RENEWAL` 路由到
`apply_paid_billing_change()`（它本来就支持非 `PURCHASE` 分支），而不是在入口
一刀切。

#### 7.4 被放弃的信息，以及补回来的成本

合并之后，系统不再记录客户**为什么**再买一单（快到期 vs 快用完）。这个信号
是有业务价值的：如果绝大多数后续订单发生在额度耗尽时，说明档位偏小。

本 ADR **不**为此新增字段，因为它与计费行为无关，属于分析需求。若将来要补，
成本很低且不破坏本模型：在入队时，根据当前周期的 `used_bytes / quota_bytes`
与剩余天数计算一个**纯记录性**的标签存进订单行即可——它**不得**成为任何
分支条件，否则就等于把合并掉的两条路径又长回来。

### 8. 每次滚动都是一次账务外部写，复用 ADR-026 的补偿契约

激活下一个周期必须把新的额度与到期时间写进账务 provider
（`AccountingProvider.set_quota()` + `set_expire()`）。这是**真实外部副作用**，
因此：

- DB 侧的"关旧周期 + 开新周期"必须在**同一个事务**内完成，否则会撞
  `active_subscription_id` 唯一约束，或出现"没有任何 ACTIVE 周期"的空窗；
- 外部写失败时**不得**报成干净的 `FAILED`。按 **ADR-026** 的原则：补偿
  动作自身失败 ⇒ `PENDING_MANUAL`，保留部分状态供人工核查，原始异常不得
  被补偿异常掩盖；
- 账务用户**永不 DELETE**（铁律 4）。滚动只改额度与到期，不重建用户。

### 9. 额度耗尽的感知是最终一致的，这是可接受的

`used_bytes` 由 `accounting_sync` 轮询更新（正常 300s，用量 ≥90% 时 60s）。
因此 DB 感知"额度用完"存在分钟级延迟。

**这不影响客户体验**：账务内核（Marzban）自身即时阻断超额流量，客户是被
它切断的，不是被本系统切断的。本系统只是稍后才记账并滚动周期。实现**不得**
为了缩短这个延迟而绕过轮询直接在请求路径上查账务 provider。

## 计划 schema（实现阶段执行，本 ADR 不添加）

一个**新增（additive）迁移**，幂等，不改写任何历史 revision（铁律 7）：

1. **`UsagePeriodStatus` 增加 `QUEUED`。** 当前只有 `ACTIVE` / `CLOSED`。
   排队周期既不是"生效中"也不是"已结束"，必须有自己的状态，否则无法用
   一个查询区分"待生效"和"已用完"。

   `active_subscription_id` 的 computed 表达式**不需要改动**——它只在
   `status = 'ACTIVE'` 时取值，`QUEUED` 自然不占唯一键。**实现时必须
   验证这一点在 MySQL 8.4 上确实成立，不能只靠阅读表达式推断。**

2. **`usage_periods` 增加 `order_id`**：非空外键指向 `orders.id`，并加
   **唯一约束**——一个订单最多对应一个周期，防止重复入队产生两份额度。

3. **`usage_periods` 增加 `queue_position` 或依赖 `id` 排序**：二选一，
   实现时择其简。若依赖 `id`，必须在 TASK 验收里写明"FIFO 由 `id` 单调性
   保证"，不得默认读者知道。

**不新增**：任何存放完整快照的列、任何机密列、任何冗余的额度缓存列。
额度真值始终是 `quota_bytes` / `used_bytes` 两列本身。

`Order.addon_bytes` **保留不动**（见「不做的事」）。

## 不做的事

- 不引入流量结转（rollover）。
- 不引入按比例退款或中途改档（`UPGRADE` 不再使用）。
- 不引入独立的加购 SKU 或加购价格。
- 不改变 `GRACE` 的时长语义（`grace_period_hours`，默认 24）。
- **不删除 `Order.addon_bytes` 列。** 它在本模型下不再使用，但删列需要
  破坏性迁移；按铁律 7 只新增、不改写历史。标记为 deprecated，
  `BillingCommand.addon_bytes` 停止参与业务判断。

## 重新评估条件

- 若将来新增档位破坏了"档位越大每 GB 越便宜"的单调性，第 4 条的反套利
  结论失效，需重新评估是否给排队订单独立计时。
- 若客户反馈强烈要求流量结转，第 5 条需要重新评估——那会改变周期的额度
  语义，不是一个可以顺手加的开关。
- 若将来确实需要让"续费"与"加购"产生**不同的计费行为**（不只是不同的
  统计标签），第 7 条的合并需要撤销，并重新引入独立的 order type。
  仅仅想知道"客户为什么买"不构成撤销理由——那用 7.4 的记录性标签解决。

## 验证要求

实现本 ADR 的 PR 必须覆盖：

1. 额度耗尽触发滚动（时间未到）→ 下一单激活，`period_start` 为激活时刻。
2. 时间到期触发滚动（额度未用完）→ 下一单激活，**剩余额度不结转**。
3. 队列为空 + 时间到期 → 进入 `GRACE`，而不是滚动。
4. 队列非空 + 时间到期 → **直接激活下一单，不进入 `GRACE`**。
5. 滚动的 DB 变更是原子的：关旧 + 开新同事务；并发下不违反
   `active_subscription_id` 唯一约束。
6. 滚动时账务外部写失败 ⇒ `PENDING_MANUAL`（ADR-026），且账务用户
   未被 DELETE。
7. 滚动前后 `Subscription.token_hash` **未改变**。
8. `apply_paid_billing_change()` 对 `ADDON` / `UPGRADE` fail closed。
