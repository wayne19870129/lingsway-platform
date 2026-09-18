# 交给 Codex 的执行提示词 — Lingsway Platform 仓库搭建（历史归档，勿作为现行规则）

> ## ⚠️ 本文件是 **2026 年建库期（T0~T8）的一次性提示词**，不是现行流程
>
> 它记录的是"当初打算怎么建这个仓库"，保留是为了审计留痕。**任何新会话
> 都不应该按本文件行动。** 现行规则以这三处为准：
>
> | 要找什么 | 去哪里 |
> |---|---|
> | 现行 Agent 规则、铁律、写权限、凭据边界 | 根目录 [`AGENTS.md`](../AGENTS.md) |
> | 日常操作流程、PR / 审查流程 | 根目录 [`CLAUDE.md`](../CLAUDE.md) |
> | 当前进度、角色分工、未完成工作 | [`83-project-continuity.md`](83-project-continuity.md) |
>
> 已知本文件与现状不符之处（2026-09-18 审计确认）：
>
> - **A1 里那份 `AGENTS.md` 内容已被替换**（见下方 A1 一节）：现行
>   `AGENTS.md` 有 **8 条铁律**（本文件只列了 6 条，缺少「Alembic 历史
>   revision 不得改写」与「main 分支自我约束 / 不得自行 merge」），
>   且执行者早已从 Codex 统一为 **Claude Code**。
> - `REPO_ARCHITECTURE.md` 已收敛成指向根目录 `ARCHITECTURE.md` 的指针。
> - 任务编号已从 T 系列（T0~T25）切换到 **S 系列**（S02 / S03 / S04…），
>   本文件的 T0~T8 序列早已全部走完或被取代。
> - 本文件第 8 行写死的生产 VPS IP 与「本轮不改动生产」的时间性约束，
>   属于当时的一次性上下文，不构成现行授权或现行禁令——现行边界见
>   `AGENTS.md`「禁止自主执行 / 允许自主执行」两表。

---
0. 全局约束(任何任务都必须遵守)
禁止提交任何真实凭据。 仓库里只允许 `.env.example` 和 `*.example.conf`。第一个 commit 之前必须先有 `.gitignore` 和 `.gitleaks.toml`。
不改动生产 VPS。 本轮全部工作在本机仓库内完成。任何需要连接 `45.77.9.5` 的动作,先停下问我。
不要盲从我或 Claude 的建议。 如果某条要求与代码现状冲突、或你判断会引入风险,停下说明理由,不要一边照做一边留坑。
不要把测试失败改成通过。 断言与实现不一致时,先判断哪一边是错的,说明理由再改。
每个任务结束必须给出:改了哪些文件、跑了哪些检查、哪些没验证成功、为什么。 不要用"应该没问题"结案。
提交信息用英文,格式 `type(scope): summary`。
---
A. 相对 REPO_ARCHITECTURE.md 的增量(必须一并实现)
A1. 新增根文件 `AGENTS.md` —— **已完成，且内容已被后续演进取代**

此处原本逐字嵌入了一份 `AGENTS.md` 的初版全文。该副本已于 2026-09-18
的仓库审计中移除，因为它与现行 `AGENTS.md` **实质冲突**，而一个只读到
本文件的会话无从分辨哪一份才算数：

| 项目 | 本文件旧副本（已失效） | 现行 [`AGENTS.md`](../AGENTS.md) |
|---|---|---|
| 铁律条数 | 6 条 | **8 条**（新增 Alembic 历史不得改写、main 分支自我约束/不得自行 merge） |
| 铁律 5 措辞 | "必须先提交 ADR" | 补充了"首个实现视为已覆盖，引用对应 ADR 即可" |
| 代码模块写权限 | → Codex | → **Claude Code**（唯一执行者） |
| `docs/80-decisions/**` | "Claude 主导，Codex 可补充" | **Claude Code 独占** |
| 流程 | "Claude 不直接向 main 提交，只产出 TASK/REVIEW/ADR" | Claude Code 同时负责实现与文档产出，**不是两个角色分工** |
| 审查者 | 未定义 | **ChatGPT Work** 按 head SHA 做独立审查 |

**唯一正本是根目录 [`AGENTS.md`](../AGENTS.md)。** 该文件按其自身的
「禁止自主执行」清单，修改需要人工确认，不得由 Agent 自行改写——也正因如此，
任何地方都不应该再存在它的第二份可编辑副本。

A2. 新增 `.github/CODEOWNERS`
按 A1 的写权限表映射。
A3. CI/CD 改为风险分级(替换 v1 §11 的单一 `make deploy`)
`.github/workflows/` 最终包含:
文件	触发方式	覆盖路径	说明
`ci.yml`	所有 PR / push	全仓	ruff + mypy + pytest(MySQL 8.4 service) + eslint + tsc + next build
`security.yml`	所有 PR / 每周定时	全仓	gitleaks + pip-audit + npm audit
`risk-classify.yml`	所有 PR	全仓	按改动路径给 PR 打 `risk:low/medium/high` 标签,并在 PR 评论里说明需要哪种部署方式
`deploy-auto.yml`	push 到 main 且 仅命中低风险路径	见下	自动部署,失败自动回滚
`deploy-gateway.yml`	`workflow_dispatch` 手动	—	会重载 Xray,含九步保护
`deploy-migration.yml`	`workflow_dispatch` 手动	—	执行前强制先跑一次加密备份
风险分级路径表(写进 `risk-classify.yml` 和 README):
```
low (自动部署):
  frontend/**
  docs/**
  backend/app/api/**
  backend/app/domain/**
  backend/app/schemas/**
  backend/tests/**

medium (workflow_dispatch,走 deploy-gateway):
  backend/app/providers/gateway/**
  backend/app/providers/forwarder/**
  backend/app/providers/egress/**
  ops/gateway/**
  ops/forwarder/**
  infrastructure/marzban/**
  infrastructure/compose/**

migration (workflow_dispatch,走 deploy-migration,先备份):
  infrastructure/alembic/versions/**

high (禁止自动化,只能人工在 VPS 执行):
  删除数据 / 销毁机器 / 轮换 SECRET_ENCRYPTION_KEY /
  修改 sshd / 清空备份 / Webshare 购买或退订
```
`deploy-auto.yml` 的顺序固定:
```
CI 全绿 → 远端源码与 .env 备份 → 打包并校验 SHA-256 → 解包
→ 只重建 backend-api / frontend(不碰 marzban / mihomo / mysql)
→ 跑 deploy/lib/70_verify.sh → 失败则 deploy/rollback.sh
```
`deploy-gateway.yml` 与 `deploy-migration.yml` 除上述外,额外要求:
gateway:apply 前后各跑一次 `70_verify.sh` 的 Xray 相关子集
migration:执行前调用 `ops/backup/backup.sh` 并确认 R2 上传与 SHA-256 校验成功,否则中止
注意:私有仓库的 Environment required reviewers 需要 Enterprise 计划。因此不要依赖它,用 `workflow_dispatch` 手动触发达到同等效果。
A4. Secret 命名与注入
GitHub Actions Secrets 分两组,名字写进 `docs/50-secrets-layout.md`:
```
STAGING_*   → 给开发/测试 workflow 用
PROD_VPS_HOST / PROD_VPS_USER / PROD_VPS_SSH_KEY
PROD_DATABASE_URL
PROD_SECRET_ENCRYPTION_KEY
PROD_JWT_SECRET
PROD_WEBSHARE_API_KEY
PROD_R2_ACCOUNT_ID / PROD_R2_BUCKET / PROD_R2_ACCESS_KEY_ID / PROD_R2_SECRET_ACCESS_KEY
PROD_TELEGRAM_BOT_TOKEN / PROD_TELEGRAM_CHAT_ID
PROD_RESEND_API_KEY / PROD_RESEND_FROM_EMAIL
PROD_TURNSTILE_SITE_KEY / PROD_TURNSTILE_SECRET_KEY
PROD_BACKUP_GPG_RECIPIENT
```
规则:
`PROD_*` 只允许出现在 `deploy-*.yml` 中,`ci.yml` / `security.yml` 一律不得引用
仓库代码里只出现 `${{ secrets.XXX }}`,不得出现明文
本阶段 staging 用本机 Docker Compose,不开第二台 VPS
A5. VPS 部署用户改为 `deploy`(不再用 root)
在 `deploy/lib/10_system.sh` 中创建:
```
用户: deploy
允许: /opt/lingsway 下的 git pull、docker compose、运行 ops/ 脚本
sudoers 白名单: 仅 docker、docker compose、systemctl restart lingsway-*
禁止: 读 /root、改 sshd_config、改系统账户、访问 /etc/lingsway/* 之外的密钥
```
同时产出 `docs/60-runbooks/deploy-user-migration.md`,说明如何从当前 root 部署平滑切到 deploy 用户(先并存,验证通过再收紧 root 的 authorized_keys)。本阶段只写脚本和文档,不在生产执行。
A6. 新增文档目录
```
docs/81-reviews/    # Claude 的 Review 输出,命名 REVIEW-<编号>-<主题>.md
docs/82-tasks/      # 任务书,命名 TASK-<编号>-<主题>.md
```
`docs/82-tasks/TASK-TEMPLATE.md` 固定四段:目标 / 约束 / 允许修改的文件 / 验收标准。
A7. Webshare 客户端去重(v1 遗漏)
`ops/procurement/` 并入后,不允许存在第二份 Webshare HTTP 客户端。
`ops/procurement/cli.py` 必须复用 `backend/app/providers/egress/webshare.py`,
以保证硬拦截白名单只有一份实现。若两边行为有差异,以 backend 版本为准,差异写进 `docs/70-external-facts.md`。
---
B. 任务序列
T0 — 建库与前置检查
初始化 private 仓库 `lingsway-platform`。
先创建并提交:`.gitignore`、`.dockerignore`、`.gitleaks.toml`、`README.md`(占位)、`LICENSE`。
`.gitignore` 必须包含 `REPO_ARCHITECTURE.md §7` 列出的全部条目。
把 `REPO_ARCHITECTURE.md` 和本文件放进 `docs/`。
扫描我现有的本地工作区(`F:\Codex\network-subscription-platform` 与 `F:\Codex\webshare-procurement`),列出所有含疑似凭据的文件清单给我看,先不迁移。
验收: 仓库为 private;`git log` 只有上述文件;凭据清单已输出。
---
T1 — 骨架 + CI + AGENTS
按 `REPO_ARCHITECTURE.md §3` 创建完整目录树,空目录放 `.gitkeep`。
落地 `AGENTS.md`(§A1)、`.github/CODEOWNERS`(§A2)。
创建 `.github/workflows/ci.yml` 和 `security.yml`。
创建 `Makefile`、`pyproject.toml`、`.env.example`(含 §7 与 §A4 全部变量,值留空)。
`.github/pull_request_template.md` 必须含勾选项:
[ ] 是否影响存量客户连接
[ ] 是否会触发 Xray / Mihomo 重载
[ ] 回滚方案
[ ] 风险等级(low / medium / migration / high)
`docs/82-tasks/TASK-TEMPLATE.md`。
验收: `make lint` 通过;CI 在空仓库上绿;gitleaks 无告警。
---
T2 — Provider 抽象与 mock
按 `REPO_ARCHITECTURE.md §4` 写 `backend/app/providers/base.py`,含全部 Protocol 与 DTO(用 dataclass 或 pydantic)。
写 `providers/registry.py`,只从 `core/config.py` 的 Settings 读取选择,业务代码不得直接 import 具体实现。
为每一个 provider 写 `mock` 实现,mock 要能模拟失败(通过构造参数注入异常),供补偿逻辑测试使用。
写 `backend/tests/unit/test_registry.py`,断言全 mock 装配成功。
验收: 所有 provider 变量设为 `mock` / `noop` 时,`make test-unit` 在无网络、无 Docker 的环境下通过。这是"可插拔"成立的唯一检验,不通过不得进入 T3。
---
T3 — domain 层
实现 `ordering / capacity / quota / provisioning / subscription_render`,只依赖 Protocol。
`provisioning.py` 按 `REPO_ARCHITECTURE.md §5` 的九步顺序与补偿实现;`ProvisionRun` 表结构建好但不做完整状态机与自动重试。
`subscription_render.py` 的 UA 分支与规则顺序按当前生产约定:
`clash.meta` / `mihomo` → Clash YAML + 13 条分流规则
`v2ray` / `v2rayN` / `sing-box` → Base64 URI
其他/未知 UA → Clash YAML,仅 `MATCH,SUBSCRIPTION`
核心国外域名规则必须在 `GEOSITE,CN` 与 `GEOIP,CN` 之前
验收(单测,全 mock):
第 3 步(创建外部租户)失败时不回滚外部租户,标记 `PENDING_MANUAL`,记录外部 ID
第 6 步失败时调用 `disable_user`,断言从未调用 delete
容量不足时在标记付款之前拒绝
渲染结果不变式:首条 `geoip:private → BLOCK`,末条 `tcp,udp → BLOCK`,全文无 `DIRECT` 兜底
---
T4 — 安全护栏
按 `REPO_ARCHITECTURE.md §6` 实现并测试:
`providers/egress/webshare.py` 的底层硬拦截、白名单、双档限速、429 指数退避。
`providers/gateway/xray_file.py` 的九步安全重载。
`backend/Dockerfile` 必须自带 `xray` 二进制 + `geoip.dat` + `geosite.dat`(缺失会导致 `xray run -test` 误判失败,已踩过坑)。
补齐 `backend/tests/guards/` 全部五个测试文件。
验收: 护栏测试全绿;构造非法候选配置时 `apply()` 必须在 reload 之前中止;构造健康检查失败时必须触发回滚 + disable + 告警回调。
---
T5 — 迁移现有代码
把现有 backend / frontend / infrastructure / ops 内容搬入新结构,只改 import 与路径,不改业务逻辑。
`webshare-procurement` 整体并入 `ops/procurement/`,并按 §A7 去重。
整理 alembic 版本链。所有新迁移一律写成幂等形式(列/表存在则跳过)——`egress_endpoints.purpose` 曾因非幂等迁移导致线上部署中断。
迁移过程中发现的所有"代码与线上实际状态不一致"的地方,逐条记进 `docs/70-external-facts.md` 或 `docs/80-decisions/`,不要静默修正。
验收: `make test` 全绿(需 `TEST_DATABASE_URL` 指向本机 Docker MySQL 8.4);`alembic upgrade head` 在全新空库上从零成功。
---
T6 — 风险分级 CI/CD
按 §A3 实现四个 workflow + `risk-classify.yml`。
验收:
只改 `frontend/**` 的 PR 被打上 `risk:low`
改 `ops/gateway/**` 的 PR 被打上 `risk:medium` 并在评论中提示需手动触发 `deploy-gateway`
改 `infrastructure/alembic/versions/**` 被打上 `risk:migration`
`ci.yml` 中不出现任何 `PROD_*` secret 引用(用 grep 断言)
---
T7 — deploy 全自动
按 `REPO_ARCHITECTURE.md §3` 实现 `deploy/bootstrap.sh` + `lib/00~80` + `inventory.example.yml` + `rollback.sh`。
`70_verify.sh` 必须覆盖 §8 全部 15 项,任一失败返回非零。
`10_system.sh` 按 §A5 创建 `deploy` 用户。
产出 `docs/60-runbooks/deploy-user-migration.md`。
验收: 在一台全新 Vultr 机器上从零执行 `./deploy/bootstrap.sh -i inventory.yml`,`70_verify.sh` 全项通过。 这台机器用完即销毁。不允许用"脚本写完了"代替这条验收。
---
T8 — 文档与事实台账
补齐 `docs/` 全部条目。
`docs/70-external-facts.md` 按 §10 格式填入已有实测结论,至少包含:
Webshare 额度耗尽行为(未验证:0.001GB 上限下累计 6.61MB / 345 次请求仍全部 200)
`behavior=add` 是否保留现有代理(客服确认,未自行实测)
子用户能否限定单 IP(已实测:不能,`X-Subuser` 仍返回全部 5 个 IP)
`high_quality_ips_only` 计费(已实测:约基础价 30%,非固定 $0.50/月)
replacement 额度重置周期(文档倾向周期额度,Add More 行为未确认)
出口 IP 属性(外部数据源指向 Comcast/Verizon/RCN,Webshare API 未返回明确 residential 字段,标记"无法由公开元数据确认")
`docs/80-decisions/` 至少补齐这几条 ADR:
ADR-001 monorepo 而非多仓库
ADR-002 服务端不做地理分流,分流下沉到客户端
ADR-003 Xray 不启用运行时 gRPC 热添加
ADR-004 支付保持人工确认,不接 webhook
ADR-005 Webshare 子用户额度与售卖额度一比一,不留缓冲
ADR-006 订阅域名与站点域名三分离
ADR-007 GTID 关闭、`sync_binlog` 保持默认
ADR-008 CI/CD 按风险分级,不做无条件零审批部署
`docs/60-runbooks/` 至少四篇:xray-rollback / quota-exhausted / ip-drift / ssh-lockout。
验收: 按 `docs/10-deploy-new-server.md` 照做能独立完成一次部署,不需要口头补充。
---
C. 明确不做(写进 README,防止范围蔓延)
邀请返佣、工单系统、钱包余额、在线客服
支付 webhook(保持人工确认收款)
Xray gRPC 运行时热添加
完整开通状态机与自动重试
并发抢占压测
第二台 staging VPS(本阶段用本机 Docker)
无条件自动部署到生产(按 §A3 风险分级)
目标网站前端复刻(优先级低于 T7 部署验收和恢复演练)
---
D. 现在开始
先执行 T0,执行完停下汇报,尤其要把凭据文件清单列给我看,等我确认后再进 T1。
