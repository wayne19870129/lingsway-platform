# TASK-S05-补偿动作失败的所有权契约

> **状态：已实现**（2026-09-18，与 ADR-026 同一 PR）。ADR-026 已由 User 确认
> 为「已接受」，本任务据此实现。验收结果见文末「实际交付」。

## 目标

把 ADR-018 已经为 `CREATE_ACCOUNTING_USER`(步骤 6) 确立的原则——"补偿动作
自身失败 → `PENDING_MANUAL`，不得报告为已完全补偿的 `FAILED`"——统一应用到
`backend/app/domain/provisioning.py` 的另外三处补偿路径，并补齐当前完全缺失
的测试覆盖。

架构依据：**ADR-026**（须先由 User 确认为「已接受」后再动
`backend/app/domain/`，见 `AGENTS.md` 铁律第 5 条）。

交付后必须成立的可验证结果：

1. 步骤 5(`APPLY_FORWARDER`)、步骤 7(`APPLY_GATEWAY`) 与
   `fail_apply_gateway_lock_acquisition()` 三处的补偿调用，任意一处自身抛出
   异常时：
   - 传播/记录的仍然是**原始**失败，不是补偿失败；
   - 控制流**一定**走到一个明确终态，绝不因补偿异常穿透而把 run 留在
     `RUNNING`；
   - 结果是 `PENDING_MANUAL`，不是 `FAILED`（因此按既有约定**不**回滚 DB、
     **不**在本方法内写终态——见 ADR-026「决策」第 4 条）。
2. 新增 `PendingManualReason.FORWARDER_COMPENSATION_FAILED` 与
   `.GATEWAY_COMPENSATION_FAILED`，且二者在
   `PENDING_MANUAL_BUSINESS_MESSAGES` 中各有一条固定、安全的业务文案。
3. `fail_apply_gateway_lock_acquisition()` 返回 `ProvisionOutcome`，
   `services.py` 的 `except GatewayRouteBindingLockError` 分支据此区分
   `PENDING_MANUAL` 与 `FAILED`，不再在补偿抛出时直接穿透整个函数。

## 约束

- **改动 `backend/app/domain/` 之前必须先有已接受的 ADR-026**（铁律 5）。
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
docs/80-decisions/ADR-026-compensation-failure-ownership.md   # 仅状态改为「已接受」
docs/83-project-continuity.md                                  # 仅更新 §8 第 1 条
```

未列出的文件不得修改。

## 验收标准

```sh
make lint     # ruff check backend ops infrastructure scripts + mypy + 前端
python -m pytest backend/tests/unit backend/tests/guards
```

新增测试必须覆盖 ADR-026「验证要求」一节列出的全部四种情形：

1. 步骤 5 失败 + 补偿 apply **成功** → `FAILED`，抛出原始异常，DB 已回滚。
2. 步骤 5 失败 + 补偿 apply **也失败** → `PENDING_MANUAL`
   (`FORWARDER_COMPENSATION_FAILED`)，DB **未**回滚、run 仍为 `RUNNING`
   （终态归调用方，ADR-017），且原始与补偿两个异常**类型名**都出现在诊断中。
3. 步骤 7 失败 + `disable_user()` **成功** → `FAILED`（回归测试，行为不变）。
4. 步骤 7 失败 + `disable_user()` **也失败** → `PENDING_MANUAL`
   (`GATEWAY_COMPENSATION_FAILED`)，订单**未**被标记为普通 `FAILED`。

外加一条针对 `fail_apply_gateway_lock_acquisition()` 的用例：补偿抛出时
`confirm_payment_and_purchase` 路径仍然执行到 `fail_paid_purchase()` 或其
`PENDING_MANUAL` 等价物，不得整体穿透。

人工检查：PR 描述须说明这四种情形在**修复前**的实际表现（run 停留在
`RUNNING`、原始异常被掩盖），以便审查者确认修复真的改变了行为而不只是
加了测试。

## 实际交付（2026-09-18）

改动文件（与「允许修改的文件」一致，无越界。同一 PR 里还有 `AGENTS.md`、
`Makefile`、`pyproject.toml`、`README.md` 等改动，**不属于本任务**——它们来自
同一轮审计的另外两项决定：lint 范围扩到 `ops/`+`infrastructure/`，以及
`AGENTS.md` 的三处规则修订）：

- `backend/app/domain/provisioning.py` —— 新增两个 `PendingManualReason`
  与对应固定文案；步骤 5 / 步骤 7 / `fail_apply_gateway_lock_acquisition()`
  三处补偿全部包进自己的 `try`；`fail_apply_gateway_lock_acquisition()`
  改为返回 `ProvisionOutcome | None`；`provision()` 组合方法对 phase-B 的
  `PENDING_MANUAL` 分支。
- `backend/app/services.py` —— 新增统一的 `_mark_pending_manual()` 帮助
  函数（四条 `PENDING_MANUAL` 路径共用同一持久化顺序）；
  `provision_apply_gateway()` 返回 `PENDING_MANUAL` 时**不再**走到
  `activate_paid_purchase()`；锁获取失败路径同样分支。
- `backend/tests/unit/test_domain.py` —— 新增 8 条用例。

验收结果：

```
ruff check backend ops infrastructure scripts   All checks passed!
mypy backend/app backend/tests                  Success: no issues found in 126 source files
pytest backend/tests/unit backend/tests/guards  588 passed (580 before, +8)
```

**回归证据**（关键，不是事后补测）：将 `provisioning.py` 与 `services.py`
暂存回修复前状态后重跑，以下 4 条用例确认失败，修复后全部通过：

```
FAILED test_step_5_failure_with_failing_restore_is_pending_manual
FAILED test_step_7_failure_with_failing_disable_is_pending_manual
FAILED test_provision_never_marks_a_pending_manual_phase_b_run_succeeded
FAILED test_lock_acquisition_compensation_failure_is_pending_manual
```

与 ADR-026 原稿的**一处修正**：原稿「验证要求」写的是补偿失败时"DB 已回滚、
run 终态已写"。实现时核对既有三条 `PENDING_MANUAL` 路径
（`CREATE_TENANT`、`ACCOUNTING_CREATE_AMBIGUOUS`、
`ACCOUNTING_COMPENSATION_FAILED`）后确认约定恰好相反——`PENDING_MANUAL`
**刻意保留**部分 DB 状态供人工核查，终态由调用方在自己的业务提交之后才写
（ADR-017）。ADR-026 已按实现更正，理由见其「决策」第 4 条。
