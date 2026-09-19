# TASK-S11-补全风险分类器的路径表

> 触发 `AGENTS.md` 的 TASK 门槛：触及 `.github/workflows/`。
>
> **这是新流水线的第一个真实任务**，刻意选得小、纯治理、判错也不伤人。
> 它同时是第一个准备走完整自动合并流程的 PR。

## 背景：这条标签现在在说谎

`risk-classify.yml` 的 `classifyPath` 对没有规则的路径**兜底判 `high`**。
`.github/**`、`scripts/**`、仓库根的 `AGENTS.md` / `CLAUDE.md` 都没有规则，
于是：

```
- .github/workflows/pipeline-health.yml → high
- AGENTS.md                             → high
- CLAUDE.md                             → high
- backend/tests/guards/...              → low
- docs/...                              → low
```

（2026-09-19，PR #142 的实际输出。）

而 `risk:high` 这个标签的语义是**部署风险**——bot 自己贴的说明是
「禁止自动化部署；需人工在 VPS 执行并单独制定回滚方案」。改一个 Markdown
文档显然不是这个意思。

**后果不是"标签不好看"，是这条信号失去了区分能力**：PR #136、#137、
#140、#141、#142 连续五个都是 `risk:high`，其中没有一个碰过部署。
一个永远亮的警告灯等于没有警告灯。

## 目标

交付后必须成立：

1. `classifyPath` 对下面「分类表」列出的每一条路径，返回表中指定的等级。
2. **兜底仍然是 `high`**，一行都不许改。没列出的路径继续判 `high`。
3. `rank` / `deployment` 文案 / 打标签与评论的逻辑**一行都不改**。
4. 分类逻辑有测试覆盖，逐条断言，不是"跑一下看看"。

## 分类表（**严格照此实现，不要自行增删或改判**）

按**部署风险**分级，不是按"改动重不重要"。

| 路径前缀 | 等级 | 为什么 |
|---|---|---|
| `.github/ISSUE_TEMPLATE/` | `low` | Issue 模板，不进运行时 |
| `.github/workflows/deploy-` | **不加规则** | 保持兜底 `high`——部署 workflow 就是真高危 |
| `.github/workflows/` | `medium` | 改 CI/合并行为会影响所有后续 PR，但自身不部署 |
| `.github/` | `low` | 其余（`dependabot.yml`、`CODEOWNERS`、`automerge-enabled`、PR 模板） |
| `scripts/` | `low` | 开发期工具，不进运行时镜像 |
| `AGENTS.md` | `low` | 文档 |
| `CLAUDE.md` | `low` | 文档 |
| `README.md` | `low` | 文档 |
| `ARCHITECTURE.md` | `low` | 文档 |

**顺序很重要**：`.github/workflows/deploy-` 必须比 `.github/workflows/`
先被判定，`.github/workflows/` 必须比 `.github/` 先被判定。
否则宽的规则会先命中，窄的永远轮不到。

**明确不动的**：`backend/app/core/`、`backend/app/models/`、
`backend/app/providers/`（除已有的 gateway/forwarder/egress 三条）、
`deploy/`、`infrastructure/`（除已有两条）、`Makefile`、`pyproject.toml`
—— 这些继续兜底判 `high`，**这是对的，不要"顺手也分类一下"**。

## 约束

- **不得改动兜底 `return 'high'`。** 这是 fail-closed 的来源。
- **不得改动** `rank`、`deployment` 四段文案、`riskLabels`、`colors`、
  加标签/建评论的任何逻辑。本任务只动 `classifyPath` 用到的那几张表。
- **不得改动** `ci.yml` / `security.yml` / `auto-merge.yml` /
  `pipeline-health.yml`。
- **不得**给 workflow 增加任何权限；`permissions:` 一行不动。
  （加 `actions/checkout` **不需要**改 permissions —— `contents: read` 已经
  在那里了。所以加 checkout 不违反本条。）
- **不得**动 `scripts/automerge_gate.py` 或 `scripts/pipeline_health.py`。

## 允许修改的文件

未列出的路径一律不得修改。

```
.github/workflows/risk-classify.yml
scripts/risk_classify_paths.js              # 新增：分类表 + classifyPath，供测试直接 require
scripts/tests/risk-classify-paths.test.cjs  # 新增：逐条断言
docs/82-tasks/TASK-S11-risk-classifier-path-table.md
docs/85-agent-operating-model.md
```

**明确不在范围内**：任何 `backend/**`、`frontend/**`、`ops/**`、
`infrastructure/**`、`deploy/**`。本任务不碰一行业务代码。

> **为什么抽成 `scripts/risk_classify_paths.js`**：现在的分类逻辑内联在
> workflow 的 `github-script` 里，**没有任何办法在本地测**。抽成一个被
> workflow `require()` 的模块之后才能逐条断言。这是 `scripts/automerge_gate.py`
> 已经用过的同一个做法（TASK-S08），照着它来。
>
> workflow 里改成：
> `const { classifyPath } = require('./scripts/risk_classify_paths.js');`
>
> **⚠️ 2026-09-19 修订（本条原文是错的，已实测失败）：**
> `risk-classify.yml` **原本没有 `actions/checkout` 步骤** —— 它只用
> `github-script` 调 API，从不需要仓库文件落到磁盘。因此仅仅加一行
> `require()` 会失败：
>
> ```
> Error: Cannot find module '.../scripts/risk_classify_paths.js'
> ```
>
> （PR #144 实测，job `classify` 失败。）
>
> **所以必须在 `classify` job 的 `github-script` 之前加一步 checkout：**
>
> ```yaml
>     steps:
>       - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
>         with:
>           persist-credentials: false
>       - uses: actions/github-script@3a2844b7e9c422d3c10d287c895573f7108da1b3 # v9.0.0
>         with:
>           script: |
>             const { classifyPath } = require(
>               `${process.env.GITHUB_WORKSPACE}/scripts/risk_classify_paths.js`
>             );
>             ...
> ```
>
> 两个细节都不能省：
> - **`persist-credentials: false`** —— 这个 job 只读文件，不需要 git 凭据。
> - **`${process.env.GITHUB_WORKSPACE}` 绝对路径** —— `github-script` 里的
>   `require()` 是相对于 action 自己的目录解析的，不是相对于仓库根，
>   写 `'./scripts/...'` 即使 checkout 了也可能解析不到。
>
> 本条此前写的「其余逻辑保持原样」**造成了那次失败**：它被正确理解成
> "不要加步骤"。判定逻辑、`rank`、文案、标签与评论逻辑仍然一行不改，
> 但 checkout 这一步必须加。

## 验收标准

```sh
node --test scripts/tests/risk-classify-paths.test.cjs
python -c "import yaml;yaml.safe_load(open('.github/workflows/risk-classify.yml'))"
make lint
python -m pytest backend/tests/unit backend/tests/guards
```

测试必须逐条覆盖以下用例，**把真实输出贴进 PR 描述**：

| 输入路径 | 期望 |
|---|---|
| `.github/workflows/deploy-auto.yml` | `high` |
| `.github/workflows/deploy-gateway.yml` | `high` |
| `.github/workflows/ci.yml` | `medium` |
| `.github/workflows/pipeline-health.yml` | `medium` |
| `.github/ISSUE_TEMPLATE/codex-dispatch.md` | `low` |
| `.github/dependabot.yml` | `low` |
| `.github/automerge-enabled` | `low` |
| `scripts/automerge_gate.py` | `low` |
| `AGENTS.md` | `low` |
| `CLAUDE.md` | `low` |
| `docs/83-project-continuity.md` | `low` |
| `backend/tests/guards/test_x.py` | `low` |
| `backend/app/providers/gateway/xray_file.py` | `medium` |
| `infrastructure/alembic/versions/0024_x.py` | `migration` |
| `deploy/lib/70_verify.sh` | **`high`** |
| `backend/app/core/security.py` | **`high`** |
| `Makefile` | **`high`** |
| `some/brand/new/path.py` | **`high`**（兜底仍然生效） |

最后四条是**回归守卫**：证明这次改动没有把兜底削弱。缺这四条不算完成。

## 人工检查

- 这个 PR 触及 `.github/workflows/`，属治理变更。
- **本次刻意让它走完整的自动合并流程**：ChatGPT 按 `docs/85` §3.1 的格式
  审查 → 条件满足 → workflow 自动合并。这是这条流水线第一次真正跑通
  happy path，**过程比结果重要**——如果它卡住了，卡在哪一步就是下一个要修的。
