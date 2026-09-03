# ADR-011: Accounting and transport provider contracts

- 状态: Accepted
- 日期: 2026-09-03
- 决策范围: T5g PR 一

## 问题

T5g 需要迁入旧 worker 的账务同步和传输库存同步任务。旧实现依赖账务
provider 的配额/到期日更新、带状态的用量读取，以及传输 provider 的节点
同步、库存、容量和健康检查能力。现有 `providers/base.py` 只覆盖了开通时的
账务动作，registry 也没有传输 provider，无法在不绕过插件边界的情况下迁移
这些任务。

## 决定

扩展 `AccountingProvider`，增加 `set_quota()`、`set_expire()`，并将
`get_usage()` 的返回值统一为 `UsageDTO`，其中包含 `used_bytes` 和 `status`。

新增 `TransportProvider` Protocol，覆盖节点同步、端点读取、容量读取和
启停能力。由 `registry.py` 根据 `TRANSPORT_PROVIDER_MODE` 装配，当前提供
`mock` 实现。所有 mock provider 都支持构造参数注入失败，以便测试补偿和
失败路径。

## 约束

1. domain 与 workers 不得直接 import 具体 provider 实现。
2. 业务任务只能从 `ProviderRegistry` 获取 accounting/transport provider。
3. 本 ADR 不实现漂移检测，也不实现失败缓存与连续失败告警；前者记录为
   T10，后者记录为 T9。
4. 传输 provider 的网络请求和缓存文件写入属于 provider 实现边界，不能由
   domain 或 worker 承担。

## 考虑过的替代方案

1. 继续复用旧 accounting/transport Protocol：会保留旧模块路径和直接实现
   依赖，违反 ADR-009 的 registry 边界，否决。
2. 在 scheduler 中增加兼容分支直接适配旧 provider：会让任务绕过统一
   registry，且无法保证不同实现拥有一致契约，否决。
3. 把 transport 同步能力并入 EgressProvider：传输订阅库存与出口资源是
   不同生命周期和数据模型，强行合并会扩大 Egress 契约，否决。

## 后果

账务与传输同步任务可以在同一 registry 入口下使用 mock 运行；具体网络和
缓存实现将在 T5g PR 二中迁入。现有实现需要在真实 provider 接入时满足新的
Protocol，属于明确的适配工作而不是隐式兼容。

## 重新评估条件

当需要同时运行多家传输供应商、按节点健康动态调度，或 accounting provider
需要返回更多计费/状态维度时，重新评估是否拆分更细的 DTO、引入版本化契约
或使用更完整的依赖注入容器。
