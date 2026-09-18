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

## 允许修改的文件

未列出的路径一律不得修改。笼统的 `backend/app/**` 不被接受。

```
backend/app/domain/ordering.py
backend/app/models/subscription.py
backend/app/api/admin.py
backend/app/api/public.py
backend/app/workers/accounting_sync.py
backend/app/core/config.py
backend/app/schemas/public.py
infrastructure/alembic/versions/0026_usage_period_queue.py    # 新增，编号见下
backend/tests/unit/test_domain.py
backend/tests/unit/test_order_subscription_api.py
backend/tests/unit/test_admin_api.py
backend/tests/unit/test_scheduler.py
backend/tests/integration/test_db_adapters.py
backend/tests/integration/test_models.py
frontend/app/(customer)/subscriptions/page.tsx
frontend/app/(customer)/subscriptions/[id]/page.tsx
frontend/app/(customer)/orders/new/page.tsx
frontend/app/(customer)/plans/page.tsx
frontend/app/(customer)/orders/page.tsx
frontend/app/(admin)/admin/orders/page.tsx
docs/82-tasks/TASK-S07-order-queue-billing.md
docs/83-project-continuity.md
```

迁移编号 `0026` 为预留。若届时 `main` 上的 head 已前移（S04-B2-B 会占用
`0024`/`0025`），使用当时的下一个连续编号，并在 PR 描述中说明。
**认领编号时要对着 `main` 加上所有 open PR 一起查**，不能只看 `main`
（2026-09-18 已因此撞过一次号）。

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
