# TASK-S06-可售套餐目录单一事实源

> 触发 `AGENTS.md` 的 TASK 文件门槛：涉及**定价**（支付相关）。
>
> **状态：已实现**（2026-09-18）。交付说明见文末。

## 背景：User 已拍板的档位

2026-09-18 User 确定四档，固定 30 天周期（`Plan` 表有
`CheckConstraint duration_days = 30`）：

| plan_code | 流量 | 价格 |
|---|---|---|
| `PLAN_50GB` | 50 GB | **30 元/月** |
| `PLAN_100GB` | 100 GB | **50 元/月** |
| `PLAN_200GB` | 200 GB | **80 元/月** |
| `PLAN_500GB` | 500 GB | **120 元/月** |

不卖 300GB，不卖 1000GB。

## 审计发现的真实问题（比"档位不一致"严重）

1. **`Settings` 里 8 个 `*_price` 字段全是死配置** —— `plan_50gb_price`、
   `plan_100gb_price`、`plan_200gb_price`、`plan_500gb_price`、
   `plan_1000gb_price`、`addon_20gb_price`、`addon_50gb_price`、
   `addon_100gb_price`，**没有任何代码读取它们**。真实价格只存在于 `plans`
   数据表的 `Plan.price` 列。
2. **`plan_500gb_price` 当前是 `150.00`**，与 User 拍板的 120 元不符。
3. **`Plan.currency` 的模型默认值是 `"USD"`**，而报价是**人民币**。
   前端展示的是 `{price} {currency}`，会把 ¥30 显示成 "30 USD"。
   `Order.currency` 直接取自 `plan.currency`，所以错误会一路带到订单。
4. **可售 plan_code 清单硬编码在 3 个地方**：`api/admin.py` 的
   `ADMIN_CAPACITY_PLAN_CODES`、`(customer)/plans/page.tsx`、
   `(customer)/orders/new/page.tsx`。
5. **`GET /plans` 返回所有 ACTIVE 套餐**，由前端各自过滤——过滤逻辑重复且
   容易漂移。

## 目标

1. 新增 `backend/app/catalog.py`，作为可售套餐的**唯一事实源**：四档的
   code / 名称 / 流量 / 价格 / 币种 / 周期集中在一处；价格仍从 `Settings`
   读取，使部署可以改价而不必改代码。
2. `GET /plans` 只返回目录内的可售套餐，前端不再自带过滤清单。
3. `ADMIN_CAPACITY_PLAN_CODES` 改为引用目录，不再自己写一份。
4. `Settings`：`plan_500gb_price` 改为 `120.00`；删除
   `plan_1000gb_price`；新增 `plan_currency`（默认 `CNY`）。
5. `.env.example`：删除死变量 `PLAN_300GB_PRICE`、`PLAN_1000GB_PRICE`；
   补上 `PLAN_CURRENCY`。

## 约束

- **不碰 `backend/app/domain/`**（无需新 ADR）。
- **不实现 seed。** `deploy/lib/60_seed.sh` 目前显式拒绝隐式种子数据
  （`SEED_COMMAND must be explicitly configured; no implicit seed data is
  accepted`），这看起来是刻意的安全决定，不是遗漏。把目录接进 seed 是
  独立工作，需要 User 确认后另开任务。**本任务结束后，价格仍然不会自动
  进入数据库** —— 必须在交付说明里写明这一点，不得让它看起来"已经生效"。
- **不改 `Plan.currency` 的模型默认值**，也不新增迁移。迁移 0001 里该列
  没有 server_default，改模型默认值会影响任何未显式指定币种的写入路径；
  目录始终显式指定币种，因此没有必要冒这个险。
- `addon_*_price` 三个死配置**本次保留不动**：ADDON 订单走 `addon_bytes`
  而不是 `Plan` 行，是否要做加购定价是另一个产品决定。
- API 契约改动必须同时检查两侧（`CLAUDE.md`「Implementation」）：后端
  `/plans` 收窄返回集，前端两个页面必须在同一 PR 内同步。

## 允许修改的文件

```
backend/app/catalog.py                        # 新增
backend/app/core/config.py
backend/app/api/public.py
backend/app/api/admin.py
backend/tests/unit/test_catalog.py            # 新增
backend/tests/unit/test_public_api.py
frontend/app/(customer)/plans/page.tsx
frontend/app/(customer)/orders/new/page.tsx
.env.example
docs/83-project-continuity.md                 # 仅更新 §8 第 4 条
```

未列出的文件不得修改。

## 验收标准

```sh
make lint
python -m pytest backend/tests/unit backend/tests/guards
```

必须成立的断言：

1. 目录恰好包含四档，code 与流量与上表逐项一致。
2. 四档价格分别为 `30.00 / 50.00 / 80.00 / 120.00`，币种为 `CNY`。
3. 目录价格确实来自 `Settings`——改 `Settings` 的价格，目录随之改变
   （证明不是第二处硬编码）。
4. `PLAN_1000GB` / `PLAN_300GB` 在整个仓库中不再作为可售档位出现。
5. `GET /plans` 不返回目录外的 ACTIVE 套餐。
6. `ADMIN_CAPACITY_PLAN_CODES` 与目录一致（用测试锁死，防止再次漂移）。
7. 前端两个页面不再包含硬编码的 plan_code 清单。

人工检查：交付说明必须明确写出"价格仍未进入数据库，seed 未实现"，
否则这个改动会造成"已经改好了"的错觉。

## 实际交付（2026-09-18）

改动文件与「允许修改的文件」一致，**有一处偏差**：`/plans` 端点的两条
测试写在了新增的 `backend/tests/unit/test_catalog.py` 里，没有动
`test_public_api.py`（该文件没有现成的 DB 夹具，自带夹具更内聚）。

验收结果：

```
ruff check backend ops infrastructure scripts   All checks passed!
mypy backend/app backend/tests                  Success: 128 source files
pytest backend/tests/unit backend/tests/guards  596 passed (588 before, +8)
frontend: tsc --noEmit / eslint --max-warnings=0 / next build   全部通过
```

八条断言全部落实，其中两条锁死了最容易再次漂移的地方：
`ADMIN_CAPACITY_PLAN_CODES is SELLABLE_PLAN_CODES`（同一个对象，不是同值
的两份拷贝），以及 `/plans` 在库里同时存在 `PLAN_1000GB` 与 `LEGACY_TIER`
时只返回目录内的档位。另有一条防止"收窄 plan_code 时顺手把 ACTIVE 判断
弄丢"的回归守卫。

### ⚠️ 必读：价格**尚未生效**

本任务交付的是**目录声明**，不是**生效的定价**。`plans` 表由仓库之外的
手段填充（`60_seed.sh` 显式拒绝隐式种子数据），仓库里没有任何代码创建
`Plan` 行。因此：

- `backend/app/catalog.py` 说"我们卖这四档、价格是这些"；
- 但客户实际看到的价格，取决于**数据库里已有的 `plans` 行**，与本目录
  可能完全不一致；
- 尤其是 **`currency`**：如果库里现存的行是 `USD`，前端会把 ¥30 显示成
  "30 USD"，`Order.currency` 直接取自 `plan.currency`，错误会带到订单。

**上线前必须人工核对 `plans` 表的现有行**（四个 plan_code、price、
currency、duration_days=30），或者先做「把目录接进 seed」这个独立任务，
而那需要 User 先同意"部署过程可以写业务数据"。
