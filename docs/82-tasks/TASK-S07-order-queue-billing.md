# TASK-S07-订单队列计费（续费/加购落地）

> 触发 `AGENTS.md` 的 TASK 文件门槛：触及 `backend/app/domain/`、
> `infrastructure/alembic/versions/`，且涉及支付与计费。
>
> 架构依据：**ADR-027**（订单队列计费模型），**状态已是「已接受」**，
> 因此本任务**可以开工**。
>
> 复用：**ADR-026**（补偿动作失败所有权）、**ADR-018**
> （`AccountingCreateEffect`）、**ADR-017**（相位切分与终态持久化顺序）。
> 本任务**不得**重新发明这三者的语义。

## 目标

把"再买一单 → 排队 → 当前单结束后顶上"这条链路从零实现出来。当前
`apply_renewal` / `apply_upgrade` / `apply_addon` 三个全是 stub
（`backend/app/api/admin.py:159-169`），周期滚动代码零行
（`apply_expiry_policy()` 的 docstring 明确写着不碰周期边界）。

交付后必须成立的可验证结果：

1. **入队**：付款确认后，在已有订阅上创建一个 `QUEUED` 周期，并且
   **当前周期的 `quota_bytes` / `period_end` / token / 账务 provider
   全部不变**。
2. **出队（额度耗尽）**：`used_bytes >= quota_bytes` 时，当前周期关闭，
   队首出队成为 `ACTIVE`，`period_start = 激活时刻`、
   `period_end = 激活时刻 + 30 天`。
3. **出队（时间到期）**：`now >= period_end` 且队列非空时，同上；
   **剩余额度不结转**。
4. **队列为空时才进 `GRACE`**：队列非空 + 时间到期 → 直接激活下一单，
   **不经过 `GRACE`**。
5. **token 不变**：滚动前后 `Subscription.token_hash` 完全相同。
6. **滚动是原子的**：关旧 + 开新在同一事务；并发下不违反
   `UsagePeriod.active_subscription_id` 唯一约束。
7. **账务写失败 ⇒ `PENDING_MANUAL`**（ADR-026），账务用户**永不 DELETE**。
8. `apply_paid_billing_change()` 对 `ADDON` / `UPGRADE` **fail closed**。
9. **修掉已可达的缺陷（ADR-027 §7.3）**：`admin_confirm_payment`
   （`api/admin.py:687-689`）当前在入口就拒绝一切非 `PURCHASE` 订单，而
   `/subscriptions/{id}/renew` 却能正常创建 `RENEWAL` 订单——**客户今天就能
   点出一个永远无法被确认收款的续费订单**。必须把 `RENEWAL` 路由到
   `apply_paid_billing_change()`，而不是一刀切拒绝。
10. **解除档位锁（ADR-027 §7.2）**：`/subscriptions/{id}/renew`
    （`api/subscription.py:55-84`）当前把档位硬锁成
    `subscription.plan_id`，调用方无法选档。改为从四档中由调用方选择。
    **这是对已上线端点的行为变更，PR 描述必须写明。**

## 约束

- **不得给 `RENEWAL` 加回档位锁。** ADR-027 §7.1 已写明 `RENEWAL` 现在的
  语义是"已有订阅上的后续订单、档位自选"，**不是**"按原档位续费"。名字
  与语义有偏差是刻意接受的债务，不要按字面理解去"修正"它。
- **不得为区分"续费/加购"新增分支。** 若要记录客户动机，只能是**纯记录性
  标签**（ADR-027 §7.4），不得成为任何条件判断——否则等于把合并掉的路径
  又长回来。
- **铁律 4**：账务用户只 disable / 改额度，**永不 DELETE**。滚动只调
  `set_quota()` + `set_expire()`，不重建用户。
- **铁律 7**：新增迁移必须**幂等**，不得改写任何历史 revision。
- **不得删除 `Order.addon_bytes` 列**（删列是破坏性迁移）。标记 deprecated，
  停止让 `BillingCommand.addon_bytes` 参与业务判断即可。
- **不得引入流量结转（rollover）**、按比例退款、中途改档。
- **不得新增独立的加购 SKU 或加购价格**。四档价目表的唯一事实源是
  `backend/app/catalog.py`。
- **不得为了缩短额度感知延迟而在请求路径上直接查账务 provider**。
  `used_bytes` 只由 `accounting_sync` 轮询更新（ADR-027 §9）。
- **不得改变 `grace_period_hours` 的语义或默认值**。
- 不得触碰 Mihomo / S04 相关路径——那是 `TASK-S04-mihomo-activation.md`
  的范围，两条工作线不得互相夹带。
- 不得触碰 `deploy/`、生产凭据、或任何真实外部写入的开关。
- **`backend/app/services.py` 只允许改非 PURCHASE 那一个分支**
  （`confirm_payment_and_provision()` 第 350–355 行的 `else:`）。
  **PURCHASE 的九步 saga、ADR-017 的相位切分与终态持久化顺序、
  `_mark_pending_manual()` 的落库顺序，一行都不许动**——它是本仓库
  "每条 `PENDING_MANUAL` 路径必须遵循的唯一排序"（见该函数 docstring）。
  这个文件是本次新加入允许清单的，加它是为了解一个具体调用点，
  **不是把它开放成可自由重构的范围**。
- **不得把 `OrderWorkflowState.activate_subscription()` 改成"其实是入队"。**
  目标 1（入队，当前周期不变）与目标 2/3（之后才真正激活）是两个不同动作，
  用同一个方法名表达两种语义，等于把缺陷藏进命名里。需要新动作就在
  `domain/ordering.py` 的 Protocol 上新增方法，并同步四个落点
  （见「允许修改的文件」下方那张表）。

## 允许修改的文件

未列出的路径一律不得修改。笼统的 `backend/app/**` 不被接受。

```
backend/app/domain/ordering.py
backend/app/models/subscription.py
backend/app/api/admin.py
backend/app/api/public.py
backend/app/api/subscription.py
backend/app/services.py
backend/app/workers/accounting_sync.py
backend/app/core/config.py
backend/app/schemas/public.py
infrastructure/alembic/versions/0024_usage_period_queue.py    # 新增，编号见下
backend/tests/unit/test_domain.py
backend/tests/unit/test_order_subscription_api.py
backend/tests/unit/test_admin_api.py
backend/tests/unit/test_scheduler.py
backend/tests/unit/test_services_wiring.py
backend/tests/integration/test_db_adapters.py
backend/tests/integration/test_models.py
backend/tests/integration/test_provisioning_phase_boundary.py
backend/tests/integration/test_gateway_route_binding_lock.py
frontend/app/(customer)/subscriptions/page.tsx
frontend/app/(customer)/subscriptions/[id]/page.tsx
frontend/app/(customer)/orders/new/page.tsx
frontend/app/(customer)/plans/page.tsx
frontend/app/(customer)/orders/page.tsx
frontend/app/(admin)/admin/orders/page.tsx
docs/83-project-continuity.md
```

### 2026-09-19 范围闭包修正（本 TASK 此前自相矛盾，无法派发）

Codex 桌面版在开工前按规则停住并上报：**目标第 10 条点名要改
`/subscriptions/{subscription_id}/renew`，而它所在的文件不在上面这张清单里。**
执行者没有越界，是规格错了。借这次修正对十条目标做了一次逐条闭包检查，
共补入 **4 个文件**、移出 **1 个**。**每一条都给出代码依据，没有"顺手也加上"。**

**补入（逐条可复核）：**

| 文件 | 为什么必需 |
|---|---|
| `backend/app/api/subscription.py` | ①目标 10 点名的 `_create_renewal_order()`（第 50–81 行）把档位硬锁成 `plan_id=current_plan.id`，就在本文件；②`/subscriptions/{id}/details`（第 98 行）是前端拿订阅数据的**唯一**端点，「前端展示队列」这条要求绕不开它 |
| `backend/app/services.py` | `confirm_payment_and_provision()` 的**非 PURCHASE 分支已经存在**（第 350–355 行），当前写的是 `apply_paid_billing_change(...)` + `order_state.activate_subscription(...)`——**它立即激活**。而目标 1 要求付款确认后只入队、当前周期一个字段都不许变。这一行调用点在本文件，改不到就做不出「入队不激活」 |
| `backend/tests/unit/test_services_wiring.py` | 内含一份 `OrderWorkflowState` 的假实现（`prepare_purchase` / `activate_subscription` / `apply_renewal`），并在第 544 行直接断言 `(BillingOrderType.RENEWAL, "billing:renewal")`。**给 Protocol 加方法必然打破它** |
| `backend/tests/integration/test_provisioning_phase_boundary.py` | 同样内含一份 `OrderWorkflowState` 假实现（第 210、219 行），同上 |

> **`OrderWorkflowState` 全仓库只有四个落点**，实测：
> `domain/ordering.py:56`（Protocol）、`api/admin.py:171`（真实实现）、
> 上面两个测试里的假实现。**前两个本来就在清单里，后两个是这次补的。**
> 动 Protocol 而漏掉任何一个，`mypy` 或测试必挂。

**移出：**

| 文件 | 为什么移出 |
|---|---|
| ~~`docs/82-tasks/TASK-S07-order-queue-billing.md`~~ | `AGENTS.md` 写权限表：`docs/82-tasks/` **由 Claude Code 独占**，ADR-032 §3a 更把这条列为"派活交回 ChatGPT 后审查独立性成立的唯一依据"。**让执行者改 TASK 等于让被约束的人改约束。** 没有任何权威依据要求 Codex 改本文件，故移出 |

**确认过、但明确不加的（防止范围蔓延）：**

- `frontend/lib/api.ts` —— 实测**全文只有一行**（`export const api = ...` 基址常量）。
  前端调用 `/details` 用的类型是页面文件里的内联 `type Details`，而那两个页面已在清单里。
  **且全前端没有任何一处调用 `/renew`**（`grep -rn "renew" frontend/` 只命中
  `node_modules` 里的无关串），所以目标 10 不产生前端改动。
- `backend/app/domain/provisioning.py` —— `PENDING_MANUAL` 的枚举与文案在这里，
  但目标 7 的要求是**复用** ADR-026 的语义、**不得重新发明**，只读不改。
- `backend/app/catalog.py` —— 约束已写明四档价目表是唯一事实源且不新增 SKU，不改。
- ~~`backend/tests/integration/test_gateway_route_binding_lock.py` —— 虽然引用了
  `confirm_payment_and_provision`，但**没有** `OrderWorkflowState` 的假实现，
  不受 Protocol 变更影响。~~
  **⚠️ 这条判断是错的，2026-09-19 已被 MySQL 8.4 CI 推翻。见下一节。**

**如果开工后发现还有清单外的必需文件：按 `docs/86` §7 停下来上报，不要自行扩大范围。**
这次补漏的方式（先用代码证明必需，再改 TASK）就是标准动作。

### 2026-09-19 第二次范围闭包补充：`UsagePeriod.order_id NOT NULL`

PR #152（head `8e63680dd19b434664475703225f2a33d7ba85bb`）在 MySQL 8.4 上跑出
**`1 failed, 802 passed`**，唯一失败：

```
backend/tests/integration/test_gateway_route_binding_lock.py
  ::test_sync_subscription_usage_expiry_persists_when_no_active_egress_binding
Column 'order_id' cannot be null
```

**补入：`backend/tests/integration/test_gateway_route_binding_lock.py`。**

#### 为什么第一次闭包检查漏了它

**上面那张"查过、明确不加"的表里，点名不加的就是这个文件。** 当时给的理由
（"它没有 `OrderWorkflowState` 的假实现"）**本身没错，但只覆盖了一条轴**：

| 影响轴 | 第一次检查 | 该文件的实际情况 |
|---|---|---|
| `OrderWorkflowState` Protocol 新增方法 | ✅ 查了，四个落点全找齐 | 确实不受影响 |
| **`UsagePeriod` schema 新增必填列** | ❌ **根本没查这条轴** | 第 1005 行直接构造 `UsagePeriod(...)` |

**一个文件可以从多个方向被同一个 TASK 影响。只沿一条轴做闭包，得到的是
"这条轴上闭合"，不是"闭合"。** 这条教训比"漏了一个文件"重要——它会在任何
一次"顺着某个符号找引用"的检查里重演。

**做闭包检查时，至少要分别沿这几条轴各走一遍**：被改的**函数/协议**、被改的
**数据库列**、被改的**API 契约**、被改的**渲染字段**。

#### 本次沿 schema 轴做的闭包检查（逐条可复核）

S07 按 ADR-027 把 `UsagePeriod.order_id` 实现为
`Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True)`
——**NOT NULL + FK + UNIQUE**（`models/subscription.py:132`，PR #152 head 实测）。
凡是直接构造 `UsagePeriod(...)` 却不传 `order_id` 的地方都必挂。

全仓库构造点（`git grep "UsagePeriod(" 8e63680`，共四处）：

| 位置 | 是否在允许清单 |
|---|---|
| `backend/app/api/admin.py:135` | ✅ 已在 |
| `backend/app/api/admin.py:180` | ✅ 已在 |
| `backend/tests/unit/test_order_subscription_api.py:336` | ✅ 已在 |
| `backend/tests/integration/test_gateway_route_binding_lock.py:1005` | ❌ **本次补入** |

又排查了间接路径（工厂函数、fixture、原始 SQL）：
`git grep -l "usage_period\|UsagePeriod" -- backend/tests/` **只命中上面那两个
测试文件**；`backend/tests/*/conftest.py` **完全不碰** `UsagePeriod`。

**未发现其他因 `order_id NOT NULL` 必须新增的清单外文件。**

这个结论有两重支撑：上面的静态搜索，**以及一次真实的全量 MySQL 8.4 运行**
——803 条里恰好只有这一条失败。

#### 修法：改测试的 fixture，不是松生产 schema

**正确修复**：该测试创建 `UsagePeriod` 时关联一个已存在的 `Order.id`
（同文件内已有创建 `Order` 的代码可直接复用）。

**绝对不许**：把 `order_id` 改回 nullable 来让 CI 变绿。
NOT NULL + UNIQUE 是 **ADR-027 的业务决定**——一个周期对应一张订单，
这是整个队列模型的核心约束。**为了绿一条测试去松生产约束，等于用 schema
掩盖测试没跟上**，属于 `docs/86` §3「不得把失败的测试改成通过」的同一类。

**本次只补范围，不动那条测试的代码**——修它是 Codex 在 PR #152 里的活。

> **2026-09-19 更正：编号从 `0026` 改为 `0024`。**
> 原文预留 0026，理由是"S04-B2-B 会占用 0024/0025"——那是按 S04 先做写的。
> 后来排序规则反了过来（`docs/85` §5.1：**S07 优先于 S04-B2-B**），
> S04-B2-B 一行都还没写。**实测 2026-09-19：`main` 上最大编号是 `0023`。**
> 继续用 0026 会平白留下两个空号。
>
> 这是**和 S11 同一类的规格缺陷**：TASK 里一个当时成立、后来被别的决定推翻
> 的前提。派活前逐条核实前提，不要假定 TASK 写下的那一刻还成立。

**开工时仍然要自己确认一次编号**：对着当前 `main` **加上所有 open PR** 一起
查，取下一个连续编号；与本文件写的不一致时以实际为准，并在 PR 描述里说明。
只看 `main` 会撞号（2026-09-18 已因此撞过一次）。

## 验收标准

```sh
make lint
python -m pytest backend/tests/unit backend/tests/guards
python -m pytest backend/tests                # 需 TEST_DATABASE_URL（MySQL 8.4）
cd frontend && npx tsc --noEmit && npx eslint . --max-warnings=0 && npm run build
```

必须覆盖的用例（对应 ADR-027「验证要求」八条）：

1. **入队无副作用**：付款确认后当前周期的 `quota_bytes`、`period_end`、
   `token_hash` 逐项断言未变，且未调用账务 provider 的任何写方法。
2. **额度耗尽触发滚动**：时间未到，`used_bytes` 达到 `quota_bytes` →
   下一单 `ACTIVE`，`period_start` 为激活时刻。
3. **时间到期触发滚动**：额度未用完 → 下一单 `ACTIVE`，且断言
   **剩余额度未结转**（新周期 `quota_bytes` 等于其套餐额度，不含余量）。
4. **队列为空 + 到期 → `GRACE`**（既有行为回归）。
5. **队列非空 + 到期 → 不进 `GRACE`**，直接 `ACTIVE`。
6. **原子性**：一条集成测试在 MySQL 上验证关旧+开新同事务，且并发滚动
   不违反 `active_subscription_id` 唯一约束。
7. **账务写失败 ⇒ `PENDING_MANUAL`**：断言不是 `FAILED`，断言
   `delete_user` 从未被调用，断言原始异常未被补偿异常掩盖（ADR-026）。
8. **token 跨滚动不变**。
9. **`ADDON` / `UPGRADE` fail closed**：`apply_paid_billing_change()`
   收到这两个类型时抛错，不静默当作 `RENEWAL` 处理。
10. **`QUEUED` 不占唯一键**：一条 **MySQL 8.4 集成测试**验证一个订阅可以
    同时有 1 个 `ACTIVE` + N 个 `QUEUED` 周期而不违反约束。
    **不得只靠阅读 computed 表达式推断。**

### 前端

- 订阅详情页展示队列：当前周期 + "已购待生效"列表（档位、入队时间）。
- 下单页明确写出两条客户条款：
  - **额度用完或到期，先到者结束本单**；
  - **未用完的额度不结转**。
- **金额显示改成 `¥30` 而不是 `30 CNY`**（ADR-028）。当前四处渲染的都是
  `{price} {currency}`：`(customer)/plans/page.tsx`、
  `(customer)/orders/new/page.tsx`、`(customer)/orders/page.tsx`、
  `(admin)/admin/orders/page.tsx`。前三处本任务本来就要动；第四处
  （admin）一并改，已加入允许路径。
  系统只有人民币一种币种，**不要为此引入 i18n 或货币格式化库**。

## 人工检查

- PR 描述必须写明本次**不做**什么（不结转、不退款、不改档、不碰 Mihomo）。
- PR 描述必须说明迁移的幂等性如何验证。
- 「未用完不结转」是面向客户的条款，上线前需要 User 确认文案。
