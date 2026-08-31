# Agent 协作规则

## 铁律(任何 Agent 不得违反,违反即回滚)
1. 配置渲染一律从数据库全量生成,禁止增量拼接
2. 未匹配用户流量一律 BLOCK,禁止 DIRECT 兜底
3. 外部系统写操作走白名单,不提供 force / override / bypass 开关
4. 账务用户开通失败时 disable,永不 DELETE(保留用量历史)
5. 修改 backend/app/domain/ 或 backend/app/providers/base.py 之前,
   必须先在 docs/80-decisions/ 提交 ADR
6. 任何会重载 Xray 的改动,必须经过九步安全重载,不得直连重启

## 模块写权限(同一时间一个模块只有一个 Agent 可写)
backend/app/providers/**   → Codex
backend/app/domain/**      → Codex
backend/app/api/**         → Codex
frontend/**                → Codex
ops/**                     → Codex
infrastructure/**          → Codex
deploy/**                  → Codex
docs/80-decisions/**       → Claude 主导,Codex 可补充
docs/81-reviews/**         → Claude 独占
docs/82-tasks/**           → Claude 独占
ARCHITECTURE.md            → Claude 独占
AGENTS.md                  → 需人工确认才可修改

## 流程
- Claude 不直接向 main 提交,只产出 TASK / REVIEW / ADR
- Codex 从 docs/82-tasks/TASK-xxx.md 领任务,建 branch,提 PR
- Codex 读 docs/81-reviews/REVIEW-xxx.md 后自行判断:
  合理的采纳,不合理的在 PR 里写明拒绝理由,不得盲从
- 冲突时以 ADR > AGENTS.md > REVIEW 为优先级

## 权限与破坏性操作

## 凭据持有范围

过渡期(T1~T5)Codex 可自主使用:
本机测试库凭据、VPS SSH key、Webshare API Key(轮换后的新 Key)、
R2 / Telegram / Resend 凭据

Codex 始终不得持有:
GPG 私钥、Vultr 账号、GitHub 主账号、域名注册商账号

T6 完成后:
PROD_* 全部迁入 GitHub Actions Secrets,本机只保留 STAGING_*;
Codex 通过触发 workflow 完成部署,不再持有生产凭据明文

## 禁止自主执行(必须人工确认)

- 删除或清空生产数据库表 / DROP / TRUNCATE
- 删除或覆盖 /var/backups/lingsway 下的备份
- 销毁、重装、快照回滚 VPS
- 轮换 SECRET_ENCRYPTION_KEY 或 JWT_SECRET
- 修改 sshd_config、authorized_keys、root 账户
- 修改 UFW 默认策略或删除放行规则
- Webshare 购买 / 续费 / 退订 / 修改套餐 / 非 dry_run replacement
- 删除 Cloudflare Zone 或修改根域 DNS
- 强制推送或删除 main 分支 / 删除仓库
- 修改 AGENTS.md 本身

## 允许自主执行(零审批)

- 读写代码、跑测试、建分支、提 PR
- 重建 backend-api / frontend 容器
- 执行 alembic upgrade(前提:先成功跑一次加密备份)
- 触发加密备份并上传 R2
- 只读查询 Webshare / Marzban / Mihomo / 数据库
- 渲染并热加载 Mihomo 配置
- 走九步保护的 Xray 重载

## 灰区

凡不在上述两表内的破坏性动作,一律停下询问,
不得自行归类为「允许」。

## 凭据处理

任何时候不得把凭据明文写入代码、日志、PR 描述或提交信息;
日志只允许输出前 4 位 + **** + 后 4 位。
