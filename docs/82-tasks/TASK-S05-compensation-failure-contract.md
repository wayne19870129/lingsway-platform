# TASK-S05-补偿动作失败的所有权契约

## 目标

把 ADR-018 已经为 `CREATE_ACCOUNTING_USER`(步骤 6) 确立的原则——"补偿动作
自身失败 → `PENDING_MANUAL`，不得报告为已完全补偿的 `FAILED`"——统一应用到
`backend/app/domain/provisioning.py` 的另外三处补偿路径，并补齐当前完全缺失
的测试覆盖。

架构依据：**ADR-025**（须先由 User 确认为「已接受」后再动
`backend/app/domain/`，见 `AGENTS.md` 铁律第 5 条）。

交付后必须成立的可验证结果：

1. 步骤 5(`APPLY_FORWARDER`)、步骤 7(`APPLY_GATEWAY`) 与
   `fail_apply_gateway_lock_acquisition()` 三处的补偿调用，任意一处自身抛出
   异常时：
   - 传播/记录的仍然是**原始**失败，不是补偿失败；
   - `state.rollback_database()` 与 run 终态写入**一定**执行到；
   - 结果是 `PENDING_MANUAL`，不是 `FAILED`。
2. 新增 `PendingManualReason.FORWARDER_COMPENSATION_FAILED` 与
   `.GATEWAY_COMPENSATION_FAILED`，且二者在
   `PENDING_MANUAL_BUSINESS_MESSAGES` 中各有一条固定、安全的业务文案。
3. `fail_apply_gateway_lock_acquisition()` 返回 `ProvisionOutcome`，
   `services.py` 的 `except GatewayRouteBindingLockError` 分支据此区分
   `PENDING_MANUAL` 与 `FAILED`，不再在补偿抛出时直接穿透整个函数。

## 约束

- **改动 `backend/app/domain/` 之前必须先有已接受的 ADR-025**（铁律 5）。
  ADR 仍是「提议中」时不得开始实现。
- 不得引入自动重试或完整开通状态机——`ARCHITECTURE.md` §13 明确列为不做项。
- 不得改变 ADR-017 的相位切分与 run 终态持久化顺序（调用方在自己的业务提交
  之后才写 run 终态）。
- 不得改变 ADR-018 的 `AccountingCreateEffect` 分类逻辑，也不得改变步骤 6
  现有的 `_compensate_created_accounting_user()` 行为。
- 不得改变 ADR-016 的 `routing_principal` 契约。
- 补偿失败的诊断信息**只允许携带异常类型名**，不得把 provider 异常原文当作
  业务数据持久化（沿用 `_compensate_created_accounting_user()` 现有做法）。
- 不得顺带做无关重构。本任务不碰 `_confirm_paid_purchase_durable()` 的
  ADR-022 durable 路径。

## 允许修改的文件

```
backend/app/domain/provisioning.py
backend/app/services.py
backend/tests/unit/test_domain.py
docs/80-decisions/ADR-025-compensation-failure-ownership.md   # 仅状态改为「已接受」
docs/83-project-continuity.md                                  # 仅更新 §8 第 1 条
```

未列出的文件不得修改。

## 验收标准

```sh
python -m ruff check backend
python -m mypy backend/app backend/tests
python -m pytest backend/tests/unit backend/tests/guards
```

新增测试必须覆盖 ADR-025「验证要求」一节列出的全部四种情形：

1. 步骤 5 失败 + 补偿 apply **成功** → `FAILED`，抛出原始异常，DB 已回滚。
2. 步骤 5 失败 + 补偿 apply **也失败** → `PENDING_MANUAL`
   (`FORWARDER_COMPENSATION_FAILED`)，DB 已回滚，run 终态已写，且原始异常
   类型出现在诊断中。
3. 步骤 7 失败 + `disable_user()` **成功** → `FAILED`（回归测试，行为不变）。
4. 步骤 7 失败 + `disable_user()` **也失败** → `PENDING_MANUAL`
   (`GATEWAY_COMPENSATION_FAILED`)，订单**未**被标记为普通 `FAILED`。

外加一条针对 `fail_apply_gateway_lock_acquisition()` 的用例：补偿抛出时
`confirm_payment_and_purchase` 路径仍然执行到 `fail_paid_purchase()` 或其
`PENDING_MANUAL` 等价物，不得整体穿透。

人工检查：PR 描述须说明这四种情形在**修复前**的实际表现（run 停留在
`RUNNING`、原始异常被掩盖），以便审查者确认修复真的改变了行为而不只是
加了测试。
