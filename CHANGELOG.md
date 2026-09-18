# Changelog

All notable changes to this private project are documented here.

> **维护说明（2026-09-18 审计补充）：** 本文件在 T5a 之后停止更新，其间
> 又合并了 100+ 次提交、PR #110–#127。下面的 T5a 之后条目是按合并记录补录的
> **粗粒度追溯**，不是当时逐条记录的，因此**不是交付事实的权威来源**——
> 权威来源是 PR 的 commits + comments + checks 时间线（`AGENTS.md`
> 「协作角色与职责」）。当前进度请读
> [`docs/83-project-continuity.md`](docs/83-project-continuity.md)。
>
> 若不打算继续逐版本维护本文件，应该明确把它标记为归档，而不是让它继续
> 以"看起来是最新的"形式存在——这正是它此前造成的误导。

## Unreleased

### S 系列（当前产品工作线）

- **S04-B1**（PR #127）Mihomo 持久化协调基础：Job 支撑的意图记录、全局命名
  写锁、重试与 stale-running 恢复、提交结果确定性、未决意图闸门、
  finalization 与 lock-release 的持久化 blocker。fail-closed，不自动修复。
- **S04-A**（PR #126）Mihomo 投影基础：ADR-023 的不可变/带版本期望快照、
  确定性全量文档合成器、transport 引用与物化身份及新鲜度证明。
- **S03-B**（PR #125）订阅 transport 注册表接线：descriptor 解析器、
  secret revision/快照边界、registry 惰性所有权、缓存身份隔离。
- **S03-A**（PR #124）多 provider transport 选择架构（ADR-024）。
- **S02-A/B/C/D**（PR #120–#123）Mihomo 架构、Webshare 就绪度、订阅就绪度、
  provider 连续性。

### T16 Phase 2C（真实 provider 接线）

- **2C3D**（PR #116）生产 Xray 选择放行。
- **2C3C**（PR #115）持久化网关协调（ADR-022）。
- **2C3B**（PR #114）Xray 运行时 provider 接线。
- **2C3A**（PR #113）Xray 运行时控制边界（ADR-021）。
- **2C2**（PR #112）打补丁的 Marzban 镜像与身份校验。
- **2C1**（PR #111）Marzban 注册表接线。
- **2B**（PR #110）存量数据协调。

### T17–T25（自动化与工具，非产品功能）

- Claude Code GitHub Action 的并发/actor 作用域、工具权限、PR 创建与派发
  可靠性、App token 身份设计，以及最终的简化回退（T23）。
- 有条件自动合并（T18）曾短暂存在，已由 User 撤销——见 `AGENTS.md` 铁律 8。

### T1–T5（建库）

- Add the T1 repository skeleton, security policy, and CI contract.
- Add ADR-009, provider Protocols and DTOs, Settings-based assembly, and injectable mocks.
- Add the pure T3 domain services, nine-step provisioning saga, renderers, and boundary tests.
- Add T4 Webshare/Xray safety guards, offline guard tests, and pinned Xray image assets.
- Migrate the T5a database foundation, split legacy models, and preserve existing settings guards.
