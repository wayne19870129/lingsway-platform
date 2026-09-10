# ADR-008: CI/CD 按风险分级部署，不做无条件零审批部署

- 状态: 已接受
- 日期: 2026-09-10（补记，实际生效于 T6，见 `.github/workflows/risk-classify.yml`）

## 问题

`main` 分支的机械保护当前不可用（私有个人仓库计划限制，见 `AGENTS.md`
铁律第 8 条），如果所有改动合并后都自动部署到生产，任何一次误判或者
Agent 的失误都可能直接影响存量客户。是否要区分改动类型给予不同的部署
自动化程度。

## 决定

按改动路径把每个 PR 分成四级（`.github/workflows/risk-classify.yml`）：

- `low`：`frontend/**`、`docs/**`、`backend/app/api/**`、
  `backend/app/domain/**`、`backend/app/schemas/**`、`backend/tests/**`
  → 合并后 `deploy-auto.yml` 自动部署，失败自动回滚。
- `medium`：`providers/gateway|forwarder|egress/**`、`ops/gateway|forwarder/**`、
  `infrastructure/marzban|compose/**` → 只能 `workflow_dispatch` 手动触发
  `deploy-gateway.yml`，因为会重载 Xray。
- `migration`：`infrastructure/alembic/versions/**` → 只能手动触发
  `deploy-migration.yml`，执行前强制先跑一次加密备份。
- `high`：删数据、销毁机器、轮换密钥等 → 禁止任何自动化，只能人工在
  VPS 上执行。

## 约束

1. `ci.yml` / `security.yml` 不得引用任何 `PROD_*` secret（`security.yml`
   的 `policy` job 用 grep 强制检查）。
2. `deploy-auto.yml` 顺序固定：CI 全绿 → 远端备份 → 打包校验 SHA-256 →
   解包 → 只重建 backend-api/frontend（不碰 marzban/mihomo/mysql）→
   `70_verify.sh` → 失败则 `rollback.sh`。
3. 私有仓库没有 Environment required reviewers（需要 Enterprise 计划），
   `medium`/`migration` 级别用 `workflow_dispatch` 手动触发替代人工审批
   卡点，不假装能有机械强制审批。

## 考虑过的替代方案

1. 全部改动合并后无条件自动部署：实现最简单，但 `medium`/`migration`
   级别的改动一旦出错影响面是“重载 Xray 断连”或“数据库结构损坏”，
   风险远高于前端改动，用同一条自动化路径处理不合理，否决。
2. 全部改动都要求手动触发部署：会让纯前端/文档改动也背上人工触发成本，
   与“低风险改动应该低摩擦”的目标冲突，否决。

## 后果

风险分级需要持续维护路径表（新增目录时要判断该归到哪一级），换来的是
高风险操作永远不会因为一次自动合并就意外触发。

## 重新评估条件

当仓库获得 Enterprise 计划、可以启用 Environment required reviewers
等机械审批机制时，重新评估是否用机械审批替代部分 `workflow_dispatch`
手动触发。
