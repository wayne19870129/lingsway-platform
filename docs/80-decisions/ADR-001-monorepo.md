# ADR-001: 单一 monorepo 而非多仓库

- 状态: 已接受
- 日期: 2026-09-10（补记，实际生效于 T1 建库）

## 问题

平台包含 backend、frontend、infrastructure、ops、deploy 五个可独立发布的模块，
是否应该拆成多个仓库分别管理版本和 CI。

## 决定

全部放进一个 private 仓库 `lingsway-platform`，用目录边界代替仓库边界。
`domain/providers/api` 的分层规则、CI 的路径级风险分级（见 ADR-008）都建立
在 monorepo 之上：一次 PR 可以同时改动 backend 契约和它的 frontend 消费方，
避免多仓库之间出现版本漂移。

## 约束

1. 目录结构必须严格遵守 `REPO_ARCHITECTURE.md §3` 的边界，模块间不得反向依赖
   （例如 frontend 不得直接读 backend 内部模块）。
2. CI 的路径触发规则（`risk-classify.yml`）替代多仓库场景下的“各自 CI”，
   必须覆盖全部子目录。

## 考虑过的替代方案

1. backend / frontend / ops 各自独立仓库：版本对齐成本高，且平台早期由个人
   维护，多仓库的协作收益小于协调成本，否决。
2. monorepo + 子模块（git submodule）：deploy/ 依赖 backend 与 infrastructure
   的产物强绑定，子模块只会增加一层引用管理复杂度，否决。

## 后果

单仓库可以用一次 CI 跑全量校验，但也意味着无关模块的改动会共享同一条
`ci.yml` 流水线；已通过 ADR-008 的风险分级 workflow 缓解“一处改动全仓
重新部署”的问题。

## 重新评估条件

当团队规模扩大到需要按模块设置独立的访问权限（例如 backend 与 deploy 由
不同的人独占访问）时，重新评估是否拆分仓库。
