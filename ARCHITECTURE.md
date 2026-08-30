# Lingsway Platform — 仓库架构与搭建规范

> 本文件是交给 Codex 的**唯一架构依据**。搭建顺序、目录边界、插件接口、验收标准都以本文件为准。
> 建议仓库名:`lingsway-platform`,**Private**。

---

## 0. 建库前的强制前置动作

在第一次 `git init` 之前完成,否则不要开始:

1. **轮换 Webshare API Key** — 旧 Key 曾在聊天/终端日志中以明文出现,视为已泄漏。
2. **轮换订阅 token** — 已有客户订阅链接在外部出现过,用 `token/reset` 重新生成。
3. **确认 `.gitignore` 与 `.dockerignore` 先于任何代码提交存在**(见 §7)。
4. **在 CI 里启用 gitleaks**,第一个 PR 就要跑。
5. 明确:仓库里**永远只有 `.env.example`,不存在 `.env`**;所有真实凭据只存在于目标机的 `/etc/lingsway/*.conf`(`0600 root:root`)。

---

## 1. 设计目标

| 目标 | 说明 |
|---|---|
| 全自动 | 一台空白 Debian 12 → 一条命令 → 可售卖的完整平台 |
| 可插拔 | 出口供应商、账务内核、支付、通知、邮件、验证码、对象存储全部是可替换插件 |
| 可迁移 | 换机器、换域名、换出口供应商都不需要改业务代码 |
| 可回滚 | 任何会影响存量客户的操作(尤其 Xray 重载)必须先备份、先校验、失败自动回滚 |
| 可验证 | 每一步部署都有机器可执行的验收脚本,不靠人眼确认 |
| 事实留痕 | 外部系统(Webshare)的行为分「已实测/文档声明/未验证」三类记录,不把猜测当结论 |

### 三条不可违反的铁律

1. **配置渲染一律从数据库全量生成,禁止增量拼接。**
2. **未匹配的用户流量一律 `BLOCK`,禁止回落 `DIRECT`。**
3. **对外部系统的写操作走白名单;购买、续费、支付、账单类端点在客户端最底层硬拦截,不提供 `force`/`override` 开关。**

---

## 2. 分层模型

```
┌────────────────────────────────────────────┐
│ interface   API(public/admin/subscription) │  FastAPI 路由 + Next.js 页面
├────────────────────────────────────────────┤
│ domain      纯业务逻辑,零外部 SDK 依赖      │  下单/开通编排/容量/配额/计费
├────────────────────────────────────────────┤
│ providers   ★ 可插拔层,唯一允许副作用的地方 │  egress/accounting/gateway/...
├────────────────────────────────────────────┤
│ infra       DB / Secrets / Config / Log     │
└────────────────────────────────────────────┘
```

规则:

- `domain/` 里**不允许** `import httpx`、`import docker`、不允许读文件、不允许调 shell。
- 所有外部副作用必须经过 `providers/` 的抽象接口。
- 每个 provider 必须有 `mock` 实现,单元测试默认用 mock,集成测试才用真实实现。
- provider 的选择只由环境变量决定,由 `providers/registry.py` 装配,业务代码不得直接 import 具体实现。

---

## 3. 仓库目录结构

```
lingsway-platform/
├── README.md                        # 5 分钟看懂 + 快速开始
├── ARCHITECTURE.md                  # 本文件
├── CHANGELOG.md
├── Makefile                         # 统一入口
├── pyproject.toml
├── alembic.ini
├── .env.example
├── .gitignore
├── .dockerignore
├── .gitleaks.toml
│
├── .github/
│   ├── workflows/
│   │   ├── ci.yml                   # ruff + mypy + pytest(MySQL service) + next build
│   │   ├── security.yml             # gitleaks + pip-audit + npm audit
│   │   └── release.yml              # tag → 构建镜像 → 生成部署包
│   ├── ISSUE_TEMPLATE/
│   │   ├── incident.md              # 事故复盘模板
│   │   └── external-fact.md         # 外部系统事实核验模板
│   └── pull_request_template.md     # 含「是否影响存量客户 / 回滚方案」勾选项
│
├── backend/
│   ├── Dockerfile                   # 含 xray 二进制 + geoip.dat/geosite.dat(用于配置校验)
│   ├── app/
│   │   ├── main.py
│   │   ├── core/
│   │   │   ├── config.py            # Settings,所有开关集中于此
│   │   │   ├── database.py
│   │   │   ├── secrets.py           # 对称加密的 Secret 表访问
│   │   │   ├── logging.py           # 结构化日志 + 凭据脱敏过滤器
│   │   │   └── rate_limit.py
│   │   ├── models/
│   │   │   ├── identity.py          # Customer / JwtSession
│   │   │   ├── billing.py           # Plan / Order / Payment
│   │   │   ├── subscription.py      # Subscription / UsagePeriod / SubscriptionVersion
│   │   │   ├── egress.py            # EgressEndpoint / EgressBinding / ExternalTenant
│   │   │   ├── gateway.py           # GatewayRouteBinding / ConfigVersion
│   │   │   └── ops.py               # Secret / AuditLog / ProvisionRun / Alert
│   │   ├── schemas/
│   │   ├── api/
│   │   │   ├── public.py            # 注册/登录/套餐/订单/订阅详情
│   │   │   ├── admin.py             # 订单确认/客户/IP池/容量
│   │   │   ├── subscription.py      # /s/<token>,UA 自适应渲染
│   │   │   └── health.py
│   │   ├── domain/
│   │   │   ├── ordering.py          # 下单、付款通知、人工确认
│   │   │   ├── provisioning.py      # ★ 开通编排(saga)
│   │   │   ├── capacity.py          # 容量硬校验
│   │   │   ├── quota.py             # 额度换算、一比一规则
│   │   │   └── subscription_render.py  # Clash/Base64 渲染 + 分流规则
│   │   ├── providers/               # ★★ 可插拔层
│   │   │   ├── base.py              # 全部 Protocol 定义
│   │   │   ├── registry.py          # 按 env 装配
│   │   │   ├── egress/{webshare,manual,mock}.py
│   │   │   ├── accounting/{marzban,mock}.py
│   │   │   ├── gateway/{xray_file,mock}.py
│   │   │   ├── forwarder/{mihomo,mock}.py
│   │   │   ├── payment/{manual,mock}.py
│   │   │   ├── notify/{telegram,noop}.py
│   │   │   ├── email/{resend,smtp,noop}.py
│   │   │   ├── captcha/{turnstile,noop}.py
│   │   │   └── storage/{r2,s3,local}.py
│   │   └── workers/
│   │       ├── scheduler.py
│   │       ├── usage_sync.py        # Webshare 用量同步(5min / ≥90% 时 1min)
│   │       ├── drift_check.py       # 出口 IP 漂移检测
│   │       └── baseline.py          # 用量基线采样
│   └── tests/
│       ├── conftest.py              # 需要 TEST_DATABASE_URL,真实 MySQL 8.4
│       ├── unit/                    # 全 mock,无 DB
│       ├── integration/             # 有 DB
│       └── guards/                  # ★ 安全护栏专项测试(见 §6)
│
├── frontend/
│   ├── app/
│   │   ├── (customer)/{register,login,portal,plans,orders,subscriptions}/
│   │   ├── (admin)/admin/{orders,customers,egress,capacity}/
│   │   └── (docs)/guides/{windows,macos,ios,android}/
│   ├── components/
│   │   ├── layout/SideNav.tsx       # 订阅 / 财务 / 用户 分组
│   │   ├── subscription/            # 卡片、进度条、出口IP区块、重置区块
│   │   └── admin/DataTable.tsx
│   ├── lib/api.ts
│   └── styles/
│
├── infrastructure/
│   ├── compose/
│   │   ├── compose.base.yml         # mysql / backend-api / scheduler / frontend / caddy
│   │   ├── compose.transport.yml    # marzban / mihomo
│   │   ├── compose.monitoring.yml   # uptime-kuma(profile: monitoring)
│   │   ├── compose.probe.yml        # gateway-probe(profile: probe)
│   │   └── compose.dev.yml
│   ├── caddy/Caddyfile.tmpl         # 由 SITE/API/SUBSCRIPTION 三个域名变量渲染
│   ├── mysql/conf.d/99-binlog.cnf
│   ├── marzban/xray_config.base.json  # 只含 inbound/reality 骨架,路由由渲染器生成
│   └── alembic/
│       ├── env.py
│       └── versions/
│
├── deploy/                          # ★ 全自动可插拔部署
│   ├── bootstrap.sh                 # 唯一入口
│   ├── inventory.example.yml        # 目标机 + 启用哪些插件 + 域名
│   ├── rollback.sh
│   └── lib/
│       ├── 00_preflight.sh          # 系统版本/磁盘/内存/网络/端口占用
│       ├── 10_system.sh             # docker / 时区 / ufw / fail2ban / ssh 加固
│       ├── 20_secrets.sh            # /etc/lingsway/*.conf 生成与校验(缺失即中止)
│       ├── 30_dns_verify.sh         # 三个域名解析核验,不通过不签证书
│       ├── 40_stack_up.sh           # 按 profile 拉起
│       ├── 50_migrate.sh            # alembic upgrade head
│       ├── 60_seed.sh               # 管理员 / 四档套餐 / 出口池导入
│       ├── 70_verify.sh             # ★ 验收清单(见 §8)
│       └── 80_schedule.sh           # 备份 cron + binlog guard timer
│
├── ops/
│   ├── backup/{backup.sh,restore.sh,install-cron.sh}
│   ├── gateway/{render_xray_routes.py,safe_reload.py}
│   ├── forwarder/render_mihomo_config.py
│   ├── probe/egress_chain_probe.py
│   ├── procurement/                 # 原 webshare-procurement 整体并入
│   │   ├── cli.py
│   │   └── config/procurement.yaml
│   └── status.py                    # 运维总览 + OVER_PROXY_LIMIT 告警
│
└── docs/
    ├── 00-overview.md
    ├── 10-deploy-new-server.md
    ├── 20-provisioning-flow.md
    ├── 30-backup-restore.md         # 含 Windows PowerShell gpg --output 说明
    ├── 40-domain-migration.md       # 订阅域名切换手册
    ├── 50-secrets-layout.md
    ├── 60-runbooks/
    │   ├── xray-rollback.md
    │   ├── quota-exhausted.md
    │   ├── ip-drift.md
    │   └── ssh-lockout.md
    ├── 70-external-facts.md         # ★ 外部系统事实台账
    └── 80-decisions/                # ADR
```

---

## 4. 插件接口定义(`backend/app/providers/base.py`)

Codex 必须**先写这个文件**,再写任何具体实现。

```python
from typing import Protocol
from decimal import Decimal

# ── 出口供应商(Webshare / 未来其他住宅代理商) ──────────────
class EgressProvider(Protocol):
    name: str
    def list_endpoints(self) -> list[EgressEndpointDTO]: ...
    def capacity(self) -> CapacityDTO: ...                  # 总额度/已分配/预留
    def create_tenant(self, label: str, quota_gb: Decimal,
                      thread_limit: int) -> TenantDTO: ...   # Webshare 子用户
    def update_tenant_quota(self, tenant_id: str,
                            quota_gb: Decimal) -> TenantDTO: ...
    def get_tenant_usage(self, tenant_id: str) -> UsageDTO: ...
    def get_credentials(self, tenant_id: str,
                        endpoint_id: str) -> CredentialDTO: ...
    def replace_endpoint(self, endpoint_id: str,
                         dry_run: bool = True) -> ReplacementDTO: ...

# ── 账务内核(Marzban / 未来自研) ────────────────────────
class AccountingProvider(Protocol):
    def create_user(self, username: str, quota_bytes: int,
                    expire_at) -> AccountUserDTO: ...
    def disable_user(self, username: str) -> None: ...       # 只 disable,永不 DELETE
    def get_connection_links(self, username: str) -> list[str]: ...
    def get_usage(self, username: str) -> int: ...

# ── 网关(Xray 路由) ────────────────────────────────────
class GatewayProvider(Protocol):
    def render(self, desired: DesiredRoutingState) -> CandidateConfig: ...
    def validate(self, candidate: CandidateConfig) -> ValidationResult: ...
    def apply(self, candidate: CandidateConfig) -> ApplyResult: ...  # 内含九步保护
    def health(self) -> HealthReport: ...

# ── 转发层(Mihomo listener) ────────────────────────────
class ForwarderProvider(Protocol):
    def render(self, desired: DesiredForwarderState) -> CandidateConfig: ...
    def apply(self, candidate: CandidateConfig) -> ApplyResult: ...
    def health(self) -> HealthReport: ...

# ── 其余 ────────────────────────────────────────────────
class PaymentProvider(Protocol):
    def create_intent(self, order) -> PaymentIntentDTO: ...
    def confirm_manual(self, order, reference: str) -> None: ...
    def verify_webhook(self, headers, body) -> WebhookResult: ...   # manual 实现直接 raise NotSupported

class NotifyProvider(Protocol):
    def send(self, event: NotifyEvent) -> DeliveryResult: ...       # 失败不得吞掉业务状态

class EmailProvider(Protocol):
    def send(self, to: str, template: str, ctx: dict) -> DeliveryResult: ...

class CaptchaProvider(Protocol):
    def verify(self, token: str, remote_ip: str | None) -> bool: ...

class BlobStorage(Protocol):
    def put(self, key: str, path: str) -> None: ...
    def list(self, prefix: str) -> list[BlobDTO]: ...
    def delete(self, key: str) -> None: ...
    def checksum(self, key: str) -> str: ...
```

### 装配方式

```python
# providers/registry.py
EGRESS_PROVIDER=webshare|manual|mock
ACCOUNTING_PROVIDER=marzban|mock
GATEWAY_PROVIDER=xray_file|mock
FORWARDER_PROVIDER=mihomo|mock
PAYMENT_PROVIDER=manual|mock
NOTIFY_PROVIDER=telegram|noop
EMAIL_PROVIDER=resend|smtp|noop
CAPTCHA_PROVIDER=turnstile|noop
STORAGE_PROVIDER=r2|s3|local
```

**验收标准:把全部变量设成 `mock`/`noop`,`pytest backend/tests/unit` 必须能在无网络、无 Docker 的机器上跑通。** 这是「可插拔」是否真正成立的唯一检验。

---

## 5. 开通编排(`domain/provisioning.py`)

顺序固定,每步都有补偿动作:

| # | 步骤 | 失败补偿 |
|---|---|---|
| 1 | 容量硬校验:`Σ租户额度 + 本单额度 + 运维预留 ≤ 出口总带宽` | 拒绝开通,**不标记已付款**,告警 |
| 2 | 出口 IP 分配(`SELECT … FOR UPDATE` + 唯一约束) | 释放绑定 |
| 3 | `EgressProvider.create_tenant`(额度 = 售卖额度,一比一) | **不回滚**,标记 `PENDING_MANUAL`,记录外部 ID 防重复创建 |
| 4 | 凭据加密写入 Secret,`EgressBinding.credential_secret_ref` | 数据库事务回滚 |
| 5 | `ForwarderProvider.render + apply`(全量渲染) | 恢复配置备份并重载 |
| 6 | `AccountingProvider.create_user` | `disable_user`,**永不 DELETE**(保留用量历史) |
| 7 | `GatewayProvider.render + validate + apply`(九步保护) | 自动回滚 + disable 新用户 + 告警 |
| 8 | 生成订阅 token,加密存储,返回一次性 `subscription_url` | 不返回 token |
| 9 | 通知 | 通知失败不影响业务状态,后台标记「通知失败」 |

`ProvisionRun` 表现在就建(记录 run_id / step / status / error / external_ids),但**实现先用最简顺序执行 + 补偿**,不做完整状态机与自动重试 —— 客户量上来再补,接口位已留好。

---

## 6. 安全护栏(必须有独立测试,位于 `backend/tests/guards/`)

### 6.1 外部写操作硬拦截

在 `providers/egress/webshare.py` 最底层的 `request()` 中,**发出前**匹配 URL 路径即抛异常:

```
/subscription/purchase   /subscription/renew   /subscription/auto_renewal
/payment/                /billing/
/proxy/replace/ 且 dry_run != true
```

白名单(其余写操作一律拒绝):

```
所有 GET
POST   /api/v2/subuser/
PATCH  /api/v2/subuser/<id>/
PUT    /api/v2/subuser/<id>/
POST   /api/v3/proxy/replace/…  且 dry_run=true
```

不提供任何绕过参数。限速:General 240/min,Proxy List 60/min,429 指数退避最多 3 次。

### 6.2 Xray 安全重载九步(`ops/gateway/safe_reload.py`)

```
1. 备份当前 xray_config.json(带时间戳)
2. 从数据库全量渲染候选配置
3. JSON 解析校验
4. xray run -test 校验 —— 不通过立即中止,绝不重载
5. 保全校验:候选中现有用户路由 / 现有 outbound / BLOCK 兜底规则全部在场
6. 重载
7. 健康检查:Marzban healthy + 8443 监听 + 旧用户路由在 + 新用户路由在
8. 任一失败 → 恢复备份 → 再次重载 → 复验 → disable 新建用户
9. 全程审计日志 + 失败发 Telegram
```

`ops/gateway/` 的 `xray run -test` 依赖 `geoip.dat` / `geosite.dat`,**backend 镜像必须自带这两个文件**,否则校验会误判失败(已踩过)。

### 6.3 护栏测试清单

- `test_webshare_guard.py` — 每条禁止路径都必须抛异常,白名单必须放行
- `test_safe_reload.py` — 非法候选在 reload 前被拒;reload 后健康检查失败必须触发回滚 + disable + 告警回调
- `test_capacity_guard.py` — 超容量必须在标记付款之前拒绝
- `test_routing_invariants.py` — 渲染结果永远以 `geoip:private → BLOCK` 开头、以 `tcp,udp → BLOCK` 结尾,且不含 `DIRECT` 兜底
- `test_secret_leak.py` — 日志格式化器对已知凭据字段必须输出脱敏值

---

## 7. 配置与密钥

### 域名三分离(现在都填 lingsway.com,但必须是三个独立配置项)

```
SITE_DOMAIN=lingsway.com
API_DOMAIN=api.lingsway.com
SUBSCRIPTION_DOMAIN=lingsway.com     # ★ 独立,换域名只改这一项
```

订阅链接一律由 `SUBSCRIPTION_DOMAIN` 拼接,禁止硬编码。客户订阅详情页读的是当前配置,换域名后客户回到页面自动看到新链接。

### 密钥文件布局(目标机,均 `0600 root:root`)

| 文件 | 内容 |
|---|---|
| `/opt/lingsway/.env` | 应用运行配置 + `SECRET_ENCRYPTION_KEY` + `JWT_SECRET` + `WEBSHARE_API_KEY` |
| `/etc/lingsway/backup.conf` | `BACKUP_GPG_RECIPIENT` / R2 / Telegram |
| `/etc/lingsway/app-secrets.conf` | `RESEND_API_KEY` / `RESEND_FROM_EMAIL` / `TURNSTILE_*` |

规则:不进代码、不进数据库明文、不进 Git、日志只允许 `前4位****后4位`。

### `.gitignore` 必含

```
.env
.env.*
!.env.example
*.conf
!*.example.conf
data/
backups/
*.tar.gz
*.gpg
*.asc
!docs/**/*.asc.example
.venv/
node_modules/
.next/
__pycache__/
.pytest_cache/
```

---

## 8. 部署验收脚本(`deploy/lib/70_verify.sh`)

必须机器可执行,输出非零退出码即失败:

```
[ ] docker compose ps 全部 running / healthy
[ ] backend /health 200
[ ] alembic current == head
[ ] 三个域名 DNS 解析到本机
[ ] 证书存在且有效期 > 14 天
[ ] 80/443/8443 监听;1080 仅在显式启用时监听
[ ] UFW active,默认拒绝入站
[ ] SSH 禁止 root 密码登录
[ ] xray run -test 通过
[ ] Xray 路由不变式成立(private BLOCK 在首、tcp/udp BLOCK 在尾、无 DIRECT 兜底)
[ ] Mihomo listener 数 == 数据库出口数
[ ] 每个 listener 出口 IP == 数据库记录 IP
[ ] 备份 cron 存在,且能成功跑一次加密备份 + R2 上传 + SHA-256 校验
[ ] Telegram 测试消息送达
[ ] /admin/capacity 数字与数据库计算一致
```

---

## 9. 迁移路线(从现有 VPS 搬进仓库)

分 6 期,每期一个 PR,每期结束线上可用:

| 期 | 内容 | 风险 |
|---|---|---|
| P0 | 骨架 + CI + gitleaks + `.env.example` + docs 目录 + provider Protocol + mock 实现 + 单测跑通 | 零(不碰线上) |
| P1 | 把现有 backend/frontend/infrastructure 原样搬入新目录结构,只改 import 路径,不改逻辑;`domain` 与 `providers` 拆分 | 低,需一次重建 backend/frontend |
| P2 | `ops/` 归位:backup / render_xray / render_mihomo / probe / status;`webshare-procurement` 整体并入 `ops/procurement` | 低 |
| P3 | 护栏测试补齐(§6.3),`xray_file` provider 落地九步保护 | 中,改 Xray 需维护窗口 |
| P4 | `deploy/bootstrap.sh` + inventory + 验收脚本;**在一台临时 Vultr 机器上从零跑通一次** | 零(独立机器) |
| P5 | 恢复演练:用加密备份在临时机器完整还原并验证订阅可用;产出《迁移手册》 | 零 |

**P4/P5 优先级高于任何新功能。** 现在的恢复能力是「理论上可以」,不是「验证过可以」。

---

## 10. 外部事实台账(`docs/70-external-facts.md`)

这个文件是这个项目最容易被忽略、但价值最高的部分。格式:

```markdown
## Webshare 额度耗尽行为
- 状态: 未验证
- 客服说法: 立即返回 HTTP 402
- 实测(2026-08-28): proxy_limit 临时设为 0.001GB,累计 6.61MB / 345 次请求,
  等待 60s 后仍全部 200,未出现 402 / SOCKS 拒绝 / 超时
- 结论: 额度不是硬性即时限制,或对极小值有特殊处理
- 后续验证方式: 等真实客户跑满 50GB 时观察
- 影响: 加购流量功能不能假设「额度用尽必然阻断」

## behavior=add 是否保留现有代理
- 状态: 客服确认,未自行实测
- 后续验证: 真实扩容前先完整备份 proxy list 并逐条比对 ID/IP/ASN
```

已有条目至少应包括:额度耗尽行为、`behavior` 语义、多套餐额度是否合并、replacement 额度重置周期、子用户能否限定单 IP(**已实测:不能**)、`high_quality_ips_only` 计费规则(**已实测:约基础价 30%,非固定 $0.50**)。

---

## 11. Makefile 统一入口

```make
make dev-up            # 本地 compose.dev 起栈
make test              # 需 TEST_DATABASE_URL,跑 unit+integration+guards
make test-unit         # 全 mock,无需 DB/Docker
make lint              # ruff + mypy + eslint + tsc
make build             # 构建镜像
make deploy HOST=...   # 打包 → scp → 远端备份 → 迁移 → 重建 → 验收
make verify HOST=...   # 只跑验收脚本
make backup HOST=...
make restore ARCHIVE=...
```

`make deploy` 内部顺序固定:**本地测试通过 → 远端源码与 .env 备份 → 校验包 SHA-256 → 解包 → 迁移 → 只重建 backend-api/frontend → 验收 → 失败则 rollback.sh**。默认**不重启** marzban / mihomo / mysql,除非显式加 `RESTART_TRANSPORT=1`。

---

## 12. 交给 Codex 的任务序列

按顺序执行,每个任务一个 PR,前一个合并后再开下一个。

**T1 — 仓库骨架**
创建上述完整目录树(空目录放 `.gitkeep`)、`README.md`、`.gitignore`、`.dockerignore`、`.gitleaks.toml`、`Makefile`、`pyproject.toml`、`.env.example`(含 §7 全部变量,值留空或占位)、`.github/workflows/ci.yml` 与 `security.yml`。
验收:`make lint` 通过;CI 在空仓库上绿。

**T2 — Provider 抽象与 mock**
按 §4 写 `providers/base.py` 全部 Protocol 与 DTO(dataclass),并为每个 provider 写 `mock` 实现。
验收:`EGRESS_PROVIDER=mock` 等全设为 mock 时,`make test-unit` 在无网络、无 Docker 环境通过。

**T3 — domain 层**
实现 `ordering / capacity / quota / provisioning / subscription_render`,只依赖 Protocol。
验收:开通编排的九步顺序与补偿逻辑有完整单测(全 mock),含「第 3 步失败不回滚外部租户」「第 7 步失败必须 disable 而非 delete」两个专项用例。

**T4 — 护栏**
按 §6 实现 `webshare` provider 的硬拦截与限速、`xray_file` provider 的九步安全重载,并补齐 §6.3 全部测试。
验收:护栏测试全绿;故意构造非法候选配置时 `apply()` 必须在 reload 前中止。

**T5 — 迁移现有代码**
把当前 VPS 上的 backend/frontend/infrastructure/ops 内容搬入新结构,只调整 import 与目录,不改业务逻辑。补齐 alembic 版本链(注意 `egress_endpoints.purpose` 曾漏迁移,新迁移一律写成幂等形式:列存在则跳过)。
验收:`make test` 全绿;`alembic upgrade head` 在全新空库上从零成功。

**T6 — deploy 全自动**
实现 `deploy/bootstrap.sh` 与 `lib/00~80`,`inventory.example.yml`,`rollback.sh`,`70_verify.sh`。
验收:**在一台全新 Vultr 机器上从零执行 `./deploy/bootstrap.sh -i inventory.yml`,`70_verify.sh` 全项通过**。这台机器用完即销毁。

**T7 — 文档**
写齐 `docs/` 全部条目,`70-external-facts.md` 按 §10 格式填入已有实测结论,`60-runbooks/` 至少四篇。
验收:按 `10-deploy-new-server.md` 照做能独立完成一次部署,不需要口头补充。

---

## 13. 明确不做的事(写进 README,防止范围蔓延)

- 邀请返佣
- 工单系统(客户少时 Telegram 更快)
- 钱包余额 / 礼品卡
- 在线客服插件
- 支付 webhook(保持人工确认收款)
- Xray gRPC 运行时热添加(等「每开通一个客户都要打招呼」变麻烦时再评估)
- 完整开通状态机与自动重试(接口位已留,量小时手动重跑)
- 并发抢占测试(靠数据库唯一约束即可)
