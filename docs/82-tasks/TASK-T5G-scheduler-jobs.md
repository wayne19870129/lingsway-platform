# T5g — Scheduler periodic jobs

## 目标

迁入并验证 scheduler 的定时任务：`accounting.sync`、`transport.sync`、
用量同步和出口漂移检测。当前仅保留 scheduler 的 provider registry 入口，
这些任务尚未迁入。

## 约束

- T7 阶段二完成后单独执行，不与 T5e/T5f 或部署验收混合。
- 外部副作用必须经 registry/provider 抽象，不得直接实例化具体 provider。
- 用量同步与漂移检测必须保持只读或既有安全护栏，不得调用 Webshare 写接口。

## 允许修改的文件

- `backend/app/workers/**`
- `backend/app/providers/**`（仅为任务接线所需）
- `backend/tests/**`
- 本任务对应的文档与迁移文件（如确有需要）

## 验收标准

- 四类任务均有可调度入口和独立单元测试。
- 全 mock/noop 环境下 `make test-unit` 通过且无网络副作用。
- 真实 MySQL 集成测试覆盖用量同步、漂移检测的数据库状态变化。
- 全仓边界审计确认 workers 不直接 import 具体 provider 实现。

## 完成情况（2026-09-10）

`backend/app/workers/drift_check.py` 已落地：只读比对 `EgressEndpoint`
的 host/port 与 `EgressProvider.list_endpoints()` 返回的 DTO（按
`EgressEndpoint.code == EgressEndpointDTO.endpoint_id` 匹配），发现不一致
只写 `AuditLog`，不做任何自动纠正或调用 `replace_endpoint`。已接入
`scheduler.run_batch()`，`SchedulerBatchResult` 新增
`egress_endpoints_checked` / `egress_drift_findings` 两个字段。单测见
`backend/tests/unit/test_drift_check.py`（含 provider 返回空列表时只记一条
"无法核验" 而不是给每个端点各报一次假阳性的场景）。另外补了
`backend/tests/unit/test_scheduler_transport_failure.py`，覆盖
`accounting.sync`/`transport.sync` 之前缺失的失败转 DEGRADED 分支
（此前一版"现状复核"提议新建 `test_accounting_sync.py`/
`test_transport_sync.py` 两个文件,实际以这一个测试文件覆盖了同样的
缺口,不需要再拆两个文件)。

四类任务(accounting.sync / transport.sync / 用量同步 / 出口漂移检测)
现已全部有可调度入口和独立单元测试,验收标准第一条达成。

**接入时发现一个更大的前置问题**：`backend/app/providers/registry.py` 的
`build_registry()` 目前无论环境变量填什么，`EGRESS_PROVIDER` 等九个
provider 变量都只接受 mock/noop 取值，其余一律
`raise ProviderConfigurationError`。也就是说本任务接的
`registry.egress.list_endpoints()` 在任何部署环境下实际都还是
`MockEgressProvider`，`providers/egress/webshare.py` 的真实实现完全没有
被接入的路径，出口漂移检测暂时无法在真实环境里发挥作用。这个问题超出
T5G 范围，已经单独开了 `TASK-T16-real-provider-registry-wiring.md`。
