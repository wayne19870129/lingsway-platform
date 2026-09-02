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
