# Provisioning flow

> **状态：未撰写。** 本文件此前只有一句"The nine-step saga and compensations
> are implemented and documented in T3/T7"——T 系列编号已被 S 系列取代
> （见 `83-project-continuity.md` §4b），这句承诺不会由任何已排期的任务兑现。
> 在本文件补齐之前，**九步开通编排的权威描述只存在于代码与 ADR 里**。

## 在本文件补齐之前去哪里读

| 内容 | 位置 |
|---|---|
| 九步顺序与各步补偿动作（原始设计表） | [`../ARCHITECTURE.md`](../ARCHITECTURE.md) §5 |
| 实际实现（含相位切分） | `backend/app/domain/provisioning.py` |
| 相位切分与 run 终态持久化顺序 | ADR-017 |
| `routing_principal` 路由身份契约 | ADR-016 |
| `create_user()` 失败的副作用确定性分类 | ADR-018 |
| 凭据解析边界 | ADR-019 |
| 网关运行时协调（ADR-022 Direction B 路径） | ADR-022、`backend/app/infra/gateway_reconciliation.py` |
| 调用方事务/锁边界 | `backend/app/services.py` |
| 行为级测试 | `backend/tests/unit/test_domain.py` |

## 已知与 `ARCHITECTURE.md` §5 的偏差

`ARCHITECTURE.md` §5 的九步表是**原始设计**，实现已经在两处显著演进，撰写本
文件时必须以后者为准：

1. **步骤 7 有两条路径。** `GATEWAY_PROVIDER=xray_file` 且调用方持有真实
   SQLAlchemy Session 时，走 ADR-022 的 durable 协调路径
   （`_confirm_paid_purchase_durable()`：先提交 pending intent，再由
   reconciler 执行运行时变更），而不是原始表里的"render + validate + apply"
   同步调用。后者现在只在 mock/测试替身下走到。
2. **`PENDING_MANUAL` 有多个来源。** 原始表只在步骤 3 标注了
   `PENDING_MANUAL`；ADR-018 之后步骤 6 新增了两个来源（ambiguous create、
   disable 补偿失败），ADR-026 提议再新增两个（步骤 5、步骤 7 的补偿失败）。

撰写本文件时应同时核对 `docs/83-project-continuity.md` §8 的未决缺陷清单，
避免把当前行为写成"设计如此"。
