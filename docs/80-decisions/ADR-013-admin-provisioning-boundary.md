# ADR-013：废弃自动开通开关，九步编排统一强制执行

- 状态：已接受
- 日期：2026-09-04
- **部分表述已被 `ADR-015-marzban-ownership-and-route-identity.md`
  supersede（2026-09-11，2026-09-11 第二次修订同步更新本注记）**：
  下方"决定"一节里"Marzban 在新架构中的定位是 transport 层，健康检查
  归属该层一致"这一句判断，经 TASK-T16 Phase 2B0 只读研究（Marzban
  v0.8.4 公开源码 + `config.py` `marzban_*` 字段形状 +
  `AccountingProvider`/`TransportProvider` Protocol 对比 + Marzban
  节点管理能力与 `TransportProvider` 契约的 bounded-context 差异分析）
  核实为不准确，已在 ADR-015 里纠正为"Marzban 的定位是 accounting
  层"。**`/admin/accounting/health` 的具体代码接线这次确实需要修改**
  ——第一版这里曾写"不受影响、不需要修改"，ADR-015 第二次修订核实
  这个说法站不住脚：当前接线在 `ACCOUNTING_PROVIDER=marzban` 且
  `TRANSPORT_PROVIDER_MODE=subscription` 时，实际检查的是和 Marzban
  毫无关系的 `SubscriptionTransportProvider`，是一个真实的语义错误，
  不是可以无限期保留的"实现捷径"。ADR-015 选定的修复方向（Option
  H1）是给 `AccountingProvider` 新增 `health_check()`，
  `admin_accounting_health()` 改为调用
  `build_registry(...).accounting.health_check()`——现在有 ADR-015
  本身作为满足 `AGENTS.md` 铁律第 5 条"先有 ADR 再改
  `providers/base.py`"的前提，此前"为单个健康检查端点修改核心契约
  不合比例"这条阻止修改的理由不再阻塞。这项修改属于 Phase 2B 的
  实现范围，本 ADR-013 文件和 ADR-015 均不在这里实现代码。本段以下
  原文保持不动，仅在此加注更正指向。

## 背景

旧实现 `services.py:71-73` 通过 `_automatic_egress_provisioning_enabled()`，依据
`app_env == "production"` 且 `accounting_provider == "marzban"` 决定是否执行出口
分配、Mihomo 与 Xray 步骤。开关关闭时，旧流程会先把订单标记为 `PAID`，再尝试
分配 IP；失败后订单保持 `PAID`、订阅变为 `PROVISION_FAILED`，形成客户已付款但
没有可用订阅的状态。该行为已记录在 `TASK-T15`。

## 决定

新架构通过 provider registry 装配（例如 `EGRESS_PROVIDER=mock`）实现开发期隔离，
不需要在业务逻辑中保留自动开通条件分支。因此本次迁移废弃
`_automatic_egress_provisioning_enabled`，九步资源开通编排对所有环境统一执行。
这是有意的行为变更，不是迁移疏漏。容量检查、出口分配、外部租户、凭据、转发器、
账务用户、网关、订阅 token 和通知均由 `domain/provisioning.py` 的九步编排负责。

管理端 `/admin/accounting/health` 路径保持不变，但底层改调 registry 提供的
`TransportProvider.health_check()`。不为 `AccountingProvider` 新增 `health_check`：
`providers/base.py` 受 AGENTS.md 铁律第 5 条约束，为单个健康检查端点修改核心契约
不合比例；Marzban 在新架构中的定位是 transport 层，健康检查归属该层一致。

## 后果

统一编排消除了“已付款但未执行外部开通”的环境分支，但也要求每个环境配置可用的
provider 实现或 mock。失败仍通过现有补偿逻辑和 `PROVISION_FAILED` 状态留痕。

## 重新评估条件

当 provider registry 需要在同一进程内同时运行多套开通策略，或需要按租户动态选择
不同编排策略时，重新评估是否需要独立的策略配置或更完整的依赖注入机制。
