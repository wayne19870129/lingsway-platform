# ADR-026: 补偿动作自身失败时的所有权契约

- 状态: 已接受
- 日期: 2026-09-18（同日实现并合并，见 `TASK-S05-compensation-failure-contract.md`）
- 决策范围: `backend/app/domain/provisioning.py` 的 `APPLY_FORWARDER`(步骤 5)、
  `APPLY_GATEWAY`(步骤 7) 以及 `fail_apply_gateway_lock_acquisition()` 三处
  补偿路径。不改变 ADR-017 的相位切分、ADR-018 的
  `AccountingCreateEffect` 分类、ADR-016 的 `routing_principal` 契约，
  也不改变 `services.py` 的事务边界。
- 触发来源: 2026-09-18 仓库全量架构审计（非 PR 审查）。缺陷已在当前
  工作树上复现，不是推测。

## 背景

ADR-018 已经为 `CREATE_ACCOUNTING_USER`(步骤 6) 建立了一条明确原则：

> 补偿动作（`disable_user()`）**自身失败**时，外部账户可能仍然是启用状态，
> 这**永远不能**被报告成一次"已完全补偿"的普通 `FAILED`，必须降级为
> `PENDING_MANUAL`。

`_compensate_created_accounting_user()` 严格实现了这一点。**问题是这条
原则没有被应用到其它同样带补偿动作的步骤上。**

## 已复现的缺陷

三处补偿动作都是**裸调用**，没有被自己的 `try` 包住：

| 位置 | 补偿动作 | 失败后果 |
|---|---|---|
| `provision_prepare()` 步骤 5 | `forwarder.apply(render(previous_forwarder))` | 见下 |
| `provision_apply_gateway()` 步骤 7 | `accounting.disable_user(...)` | 见下 |
| `fail_apply_gateway_lock_acquisition()` | `accounting.disable_user(...)` | 见下 |

以步骤 5 为例，现有代码是：

```python
except Exception as exc:
    self.forwarder.apply(self.forwarder.render(previous_forwarder))  # ← 可能抛
    self.state.rollback_database()
    self._failed(run_id, ProvisionStep.APPLY_FORWARDER, exc)
    raise
```

当补偿调用自身抛出时，实测结果（两例均已复现）：

1. **原始失败被掩盖** —— 传播出去的是补偿异常，`exc` 丢失。调用方
   `services.confirm_payment_and_provision()` 记录的是
   `type(补偿异常).__name__`，人工排障看到的是错误的根因。
2. **`state.rollback_database()` 被跳过** —— 步骤 4 已 flush 的凭据/绑定
   改动不会被本方法回滚。
3. **`self._failed()` 被跳过** —— `ProvisionRun` **永久停留在 `RUNNING`**，
   既不是 `FAILED` 也不是 `PENDING_MANUAL`。
4. **最关键：外部副作用无人认领。** 步骤 5 失败时 Mihomo 配置处于
   未知/半应用状态却没有任何 `PENDING_MANUAL` 标记；步骤 7 失败时
   Marzban 用户仍然**启用**，而订单被写成普通 `FAILED`——业务状态在说
   "已回滚干净"，事实并非如此。这与 ADR-018 的核心结论正面冲突。

调用方是否兜底，取决于路径，因而更不可依赖：

- `provision_prepare()` 的调用方确实会 `rollback_database()`，但**不会**
  写 run 的终态，也不会把结果降级为 `PENDING_MANUAL`。
- `provision_apply_gateway()` 的调用方会写 `mark_run_failed()`，但同样
  把它当作普通 `FAILED`。
- `fail_apply_gateway_lock_acquisition()` 抛出时会直接穿透整个
  `confirm_payment_and_provision()`，连 `fail_paid_purchase()` 都不会执行。
- `ProvisioningService.provision()`（组合方法）完全没有兜底。

## 决策

**把 ADR-018 已经为步骤 6 确立的原则，统一为整个 saga 的通用契约：**

1. **补偿动作必须包在自己的 `try/except` 里，永远不得替换原始异常。**
   原始失败（`exc`）是根因，必须是最终传播或记录的那一个；补偿失败只以
   **异常类型名**的形式作为附加诊断（与
   `_compensate_created_accounting_user()` 现有做法一致，不把 provider
   原文当业务数据持久化）。

2. **补偿成功 → 维持现状**：回滚 DB、记 `FAILED`、抛出原始异常。行为不变。

3. **补偿失败 → `PENDING_MANUAL`，不是 `FAILED`。** 新增两个
   `PendingManualReason`：

   - `FORWARDER_COMPENSATION_FAILED` —— 转发层配置恢复失败，Mihomo 运行时
     状态未知。
   - `GATEWAY_COMPENSATION_FAILED` —— 网关应用失败后 `disable_user()` 也
     失败，账务用户可能仍处于启用状态。

   并在 `PENDING_MANUAL_BUSINESS_MESSAGES` 中为两者各配一条固定、安全、
   可持久化的业务文案。

4. **补偿异常不得让控制流"掉出去"。** 补偿失败时必须走到一个明确的终态
   （`PENDING_MANUAL`），不得让补偿异常替代原始异常向上穿透、把
   `rollback_database()` / `_failed()` 整段跳过，从而把 run 永久留在
   `RUNNING`。

   **注意：`PENDING_MANUAL` 分支刻意不调用 `rollback_database()`，也不写
   run 终态**——这是沿用 `CREATE_TENANT`、`ACCOUNTING_CREATE_AMBIGUOUS`、
   `ACCOUNTING_COMPENSATION_FAILED` 三条既有路径已经确立的约定：

   - `FAILED` = 已完全补偿 → 回滚 DB，本方法内写终态；
   - `PENDING_MANUAL` = 外部副作用未被撤销 → **保留**部分 DB 状态供人工
     核查（否则会丢掉 `EgressBinding.credential_secret_ref` 这类记录，而
     外部租户/账户仍然存在，变成无法追溯的孤儿），终态由调用方在自己的
     业务提交之后才写（ADR-017）。

5. **`fail_apply_gateway_lock_acquisition()` 返回 `ProvisionOutcome` 而非
   `None`**，使锁获取失败路径能表达 `PENDING_MANUAL`，与步骤 7 的失败
   路径语义保持一致——ADR-017 明确要求这两条路径"不得在语义上漂移"。

## 不做的事

- 不引入自动重试。`ARCHITECTURE.md` §13 明确把"完整开通状态机与自动重试"
  列为不做项，本 ADR 不改变这一点。
- 不改变步骤 6 现有的 `AccountingCreateEffect` 分类逻辑。
- 不改变 `services.py` 的事务/锁边界；`PENDING_MANUAL` 的持久化仍然遵守
  ADR-017 的"调用方在自己的业务提交之后才写 run 终态"顺序。
- 不为 `NOTIFY`(步骤 9) 增加补偿——通知失败按设计不影响业务状态。

## 重新评估条件

- 如果将来引入完整开通状态机与自动重试（当前明确不做），补偿失败的处置
  应改为进入状态机的重试/挂起状态，届时本 ADR 需要重写而不是修补。
- 如果某个 forwarder provider 能够提供"补偿已确定生效/确定未生效"的
  certainty 证据（类似 ADR-018 的 `AccountingCreateEffect`），则步骤 5 的
  `PENDING_MANUAL` 可以进一步细分为"确定已恢复→FAILED"与"不确定→
  PENDING_MANUAL"，届时重新评估。

## 验证要求（已全部落地）

`backend/tests/unit/test_domain.py` 新增 8 条用例；其中 4 条在修复前的
代码上确认失败、修复后通过（回归证据，非事后补测）：

| 用例 | 断言 |
|---|---|
| `test_step_5_failure_with_successful_restore_stays_failed` | 回归守卫：补偿成功时行为不变——抛**原始**异常、DB 已回滚、run `FAILED` |
| `test_step_5_failure_with_failing_restore_is_pending_manual` ⚠️ | `PENDING_MANUAL` / `FORWARDER_COMPENSATION_FAILED`；诊断里同时出现原始与补偿的异常**类型名**、且不含 provider 原文；DB **未**回滚；run 仍 `RUNNING`（终态归调用方） |
| `test_step_7_failure_with_successful_disable_stays_failed` | 回归守卫；并断言 `delete_calls == []`（铁律 4） |
| `test_step_7_failure_with_failing_disable_is_pending_manual` ⚠️ | `PENDING_MANUAL` / `GATEWAY_COMPENSATION_FAILED` |
| `test_provision_never_marks_a_pending_manual_phase_b_run_succeeded` ⚠️ | `provision()` 组合方法不得把 phase-B 的 `PENDING_MANUAL` 标成 `SUCCEEDED` |
| `test_lock_acquisition_compensation_failure_is_pending_manual` ⚠️ | 锁获取失败路径与 `APPLY_GATEWAY` 语义一致 |
| `test_lock_acquisition_compensation_success_returns_none_and_marks_failed` | 补偿成功时返回 `None` 并写 `FAILED` |
| `test_every_pending_manual_reason_has_a_business_message` | 每个 `PendingManualReason` 都有固定业务文案——缺一条就会在最需要人工介入的时刻 `KeyError` |

⚠️ = 已验证在修复前的代码上失败。

调用方侧的关键断言（`services.py`）：`provision_apply_gateway()` 返回
`PENDING_MANUAL` 时**绝不能**走到 `activate_paid_purchase()`——否则会激活
一个路由从未下发的订阅。该分支现在复用统一的 `_mark_pending_manual()`
帮助函数，与其余三条 `PENDING_MANUAL` 路径共用同一套持久化顺序。
