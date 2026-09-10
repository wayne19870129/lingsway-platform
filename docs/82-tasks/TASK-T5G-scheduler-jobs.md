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

## 现状复核（2026-09-10）——本任务只剩一项没做

`backend/app/workers/scheduler.py` 的 `run_batch()` 已经把三类任务接好并
跑通：

- `transport.sync` → `backend/app/workers/transport_sync.py`
  `refresh_provider_inventory()`，失败会把 `TransportProviderRecord`
  标记 `DEGRADED` 并写 `AuditLog`，符合“只读/既有护栏”约束。
- `accounting.sync`（对账）→ `backend/app/workers/accounting_sync.py`
  `run_pending_reconcile()` + `reconcile_usage_period_cache()`。
- 用量同步（含 ≥90% 用量自动切到 60s 快轮询）→ 同文件
  `run_next_usage_job()` + `scheduler.usage_requires_fast_poll()`，
  快慢轮询切换已有独立测试
  `backend/tests/unit/test_scheduler.py::test_usage_poll_interval_switches_between_five_and_one_minutes`。

**唯一没有落地的是“出口漂移检测”**：全仓库搜不到任何 `drift` 相关代码
（`backend/app/workers/drift_check.py` 从未创建）。注意不要跟
`ops/probe/egress_chain_probe.py` 混淆——那是一个独立的、面向单个探针
账户的低频 SOCKS 出口 IP 探测脚本（对比一个写死的 `PROBE_EXPECTED_EGRESS_IP`），
跑在 ops 容器里，不经过 scheduler，也不是按客户逐一核对。本任务要的
“漂移检测”是：定期把 `EgressEndpoint` 表里记录的出口信息（host / IP /
ISP 等）与 `EgressProvider.list_endpoints()`（见
`backend/app/providers/base.py` 的 `EgressProvider` Protocol）返回的
供应商侧真实数据做比对，发现 Webshare 未经 `replace_endpoint` 流程就
静默换掉某个 IP 的情况，写入 `AuditLog` / 告警，供人工介入——不做自动
更正、不自动触发 `replace_endpoint`。

实现前请先确认：`list_endpoints()` 现有 DTO 字段是否已经足够做比对
（host/IP、ISP 等）；如果确实需要扩展 `EgressEndpointDTO`，属于修改
`providers/base.py`，按 AGENTS.md 铁律第 5 条，需要先有对应 ADR（可在
`docs/80-decisions/` 新开一条，说明为什么现有字段不够），不要跳过这一步
直接改协议。

另外，`accounting.sync` / `transport.sync` 目前只有
`test_scheduler.py` 里那个全空 mock session 的冒烟测试（断言
`transport_records == 0` 等），`backend/app/workers/accounting_sync.py`
和 `transport_sync.py` 里的具体函数（`refresh_provider_inventory`、
`run_pending_reconcile`、`run_next_usage_job`、
`reconcile_usage_period_cache`）本身没有独立单元测试覆盖真实分支（比如
`refresh_provider_inventory` 失败时的 `DEGRADED` 状态转换、
`run_pending_reconcile` 的重试/跳过逻辑）。这不满足原验收标准里“四类
任务均有…独立单元测试”，建议这次一并补上，而不是只加漂移检测这一类的
测试。

## 允许修改的文件（补充）

- `backend/app/workers/drift_check.py`（新建）
- `backend/tests/unit/test_accounting_sync.py`、
  `backend/tests/unit/test_transport_sync.py`（新建，补齐现有两类任务的
  独立单元测试）
- 若确有必要扩展 DTO：`backend/app/providers/base.py` 及对应
  `docs/80-decisions/` 新 ADR
