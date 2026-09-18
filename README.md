# Lingsway Platform

Private monorepo for the Lingsway provider-pluggable network subscription platform
on Debian 12. Interfaces, pure domain logic, side-effecting providers, and
infrastructure are kept behind explicit boundaries.

## 从哪里开始读

| 你要找的 | 文件 |
|---|---|
| 日常操作流程、PR / 审查流程 | [`CLAUDE.md`](CLAUDE.md) |
| Agent 铁律、写权限、凭据边界、破坏性操作清单 | [`AGENTS.md`](AGENTS.md) |
| **当前进度、角色分工、未完成工作、已知缺陷** | [`docs/83-project-continuity.md`](docs/83-project-continuity.md) |
| 分层模型、插件接口、安全铁律、部署验收清单 | [`ARCHITECTURE.md`](ARCHITECTURE.md) |
| 已裁定的架构决策 | [`docs/80-decisions/`](docs/80-decisions/) |

冲突时的权威顺序是 **ADR > `AGENTS.md` > REVIEW**。

`ARCHITECTURE.md` 是**原始建库规范**，其目录树与任务序列有一部分已被后续演进
取代——具体偏差见 `docs/83-project-continuity.md` 的「Spec-vs-reality drift」
一节；其设计目标、分层规则、三条铁律与部署验收清单仍然有效。
`docs/CODEX_PROMPT.md` 与 `docs/REPO_ARCHITECTURE.md` 是建库期的历史归档，
**不是现行规则**。

## Safety invariants

- Configuration is rendered in full from the database; incremental concatenation is forbidden.
- Unmatched user traffic is blocked and never falls back to `DIRECT`.
- External writes are allowlisted at the lowest client layer with no bypass switch.
- The repository contains `.env.example` only. Real credentials remain outside Git.

## Local verification

需要 Python ≥ 3.12（`pyproject.toml` 强制）。

```sh
python -m venv .venv && . .venv/bin/activate
python -m pip install -e ".[dev]"
cd frontend && npm ci && cd ..
make lint        # ruff check backend + mypy backend/app backend/tests + 前端 lint/typecheck
make test-unit   # 全 mock，无需 DB / Docker / 网络
```

完整测试（含 integration）需要 `TEST_DATABASE_URL` 指向真实 MySQL 8.4：
`make test`。集成测试在缺少该变量时**直接失败**而非跳过，这是刻意设计。

> **已知缺口：** `make lint` 只覆盖 `backend/`。`ops/**` 与
> `infrastructure/**` 下的 Python（含九步安全重载 `ops/gateway/safe_reload.py`）
> 目前不经过任何 ruff / mypy 闸门。见
> `docs/83-project-continuity.md` §8 第 2 条。

## Delivery sequence

每次一个任务，每个任务一个 PR，审查并由**人工**合并后再开始下一个。任何
Agent 都不得自行 merge（`AGENTS.md` 铁律 8）。

编号经历过一次切换：原始的 **T 系列**（T0–T8 建库，T13–T25 后续修补与自动化）
已走完，当前产品工作使用 **S 系列**（S02 / S03 / S04…）。两套编号的对照与
各自范围见 `docs/83-project-continuity.md` §4b。

## Risk-based deployment

Pull requests receive one risk label from `risk-classify.yml`. Higher risk wins
when a change touches multiple classes.

| Risk | Paths | Deployment mode |
| --- | --- | --- |
| `risk:low` | `frontend/**`, `docs/**`, `backend/app/api/**`, `backend/app/domain/**`, `backend/app/schemas/**`, `backend/tests/**` | `deploy-auto.yml` after merge; currently dry-run only |
| `risk:medium` | gateway/forwarder/egress providers, `ops/gateway/**`, `ops/forwarder/**`, Marzban and compose infrastructure | Manual `deploy-gateway.yml`; currently dry-run only |
| `risk:migration` | `infrastructure/alembic/versions/**` | Manual `deploy-migration.yml` with a mandatory backup gate; currently dry-run only |
| `risk:high` | Any path outside the classes above, or destructive/credential/host actions | No automated deployment; human VPS procedure required |

The deploy workflows are intentionally fail-closed with `DRY_RUN=true` until T7
implements the remote deployment scripts and production Actions secrets are
configured.

## Explicitly out of scope

- Referral rewards, ticketing, wallet balances, and online customer-service plugins
- Payment webhooks; payment remains manually confirmed
- Xray gRPC runtime additions
- Full provisioning state machine and automatic retries
- Concurrent-claim load testing
- A second staging VPS; this stage uses local Docker
- Unconditional automatic production deployment
- Target-site frontend replication before deployment and recovery verification
