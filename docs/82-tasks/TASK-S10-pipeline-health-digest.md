# TASK-S10-流水线健康摘要（机器采集，供 Claude 低频判断）

> 触发 `AGENTS.md` 的 TASK 门槛：触及 `.github/workflows/`。
>
> 架构依据：**ADR-030 §3**（状态「已接受」），因此本任务可以开工。
>
> **这个任务的全部价值在于"让 Claude 不必爬仓库就能判断"。** 摘要若不是
> 定长、可机读、自带异常标记，它就没有完成——Claude 读一份需要再展开三个
> PR 才能理解的摘要，等于没省 token。

## 目标

交付 `.github/workflows/pipeline-health.yml`。交付后必须成立：

1. 每 2 小时（以及 `workflow_dispatch`）运行一次，**不调用任何 LLM**，
   只用 `GITHUB_TOKEN`。
2. 产出一份**定长 JSON 摘要**，写入 workflow 的 step summary **并**更新
   仓库里一个固定的滚动记录（见「摘要落在哪里」）。
3. 摘要**自带异常标记**：ADR-030 §5 的每一条停机信号，摘要里都有一个明确
   的布尔/枚举字段，Claude 不需要自己推导。
4. 状态没有变化时，**不产生任何新的提交、Issue 或评论**——只更新 step
   summary。噪音本身就是成本。

## 摘要必须包含的字段（缺一不可）

```json
{
  "generated_at": "<ISO8601>",
  "main": {
    "sha": "<40 hex>",
    "ci_conclusion": "success|failure|pending",
    "commits_since_last_digest": 3
  },
  "automerge": {
    "breaker": "true|false|absent",
    "merged_without_user_since_breaker_on": 2
  },
  "open_prs": [
    {
      "number": 140,
      "head_sha": "<40 hex>",
      "age_hours": 3.5,
      "hours_since_last_commit": 0.4,
      "ci": "success|failure|pending",
      "latest_verdict_on_head": "PASS|NEEDS_CHANGES|none",
      "review_rounds": 2,
      "labels": ["risk:low"],
      "touches_high_risk_paths": false,
      "mergeable": true
    }
  ],
  "alerts": [
    "MAIN_CI_RED",
    "PR_STUCK_OVER_12H",
    "ROUNDS_AT_CAP",
    "THREE_CONSECUTIVE_AUTOMERGES",
    "BREAKER_OFF"
  ]
}
```

`alerts` 是**唯一** Claude 必须读的字段；其余字段只在 `alerts` 非空时才
需要展开。`alerts` 为空数组 ⇒ Claude 一眼跳过，这正是 token 的节省点。

告警枚举与 ADR-030 §5 的对应关系必须在 workflow 注释里逐条写出，
**不得只实现其中一部分**。`SAME_FINDING_FAILED_TWICE` 无法机械判定的话，
明确写出"本告警不可机械判定，由 Claude 在判断层处理"，不要静默漏掉。

- `review_rounds` 的定义：**收到审查 → 推新提交**算一轮（`CLAUDE.md`
  「Cap automatic rework at 5 rounds」的同一定义）。
- `latest_verdict_on_head` 必须复用 `scripts/automerge_gate.py` 已有的
  判定语义（信任的 author association、SHA 精确匹配、两种 verdict 同现按
  `NEEDS_CHANGES`），**不得重新实现一套**。两份实现会漂移。
- `touches_high_risk_paths`：`backend/app/domain/`、
  `backend/app/providers/`、`infrastructure/alembic/versions/`、`deploy/`、
  以及支付/凭据路径。

## 摘要落在哪里

写入 `docs/87-pipeline-health.json`（新增），**仅在内容实质变化时提交**。

- 提交者：workflow 自身，提交信息固定前缀 `chore(health):`。
- 该文件**不得**触发 `risk-classify` 之外的任何判断，也不得被
  `auto-merge.yml` 当成 PR 内容对待。
- `generated_at` 每次都会变，**因此比较"是否变化"时必须排除该字段**，
  否则每 2 小时就会产生一个无意义提交。这条是本任务最容易做错的地方。

> **2026-09-19 实测更正:下面这段"在 `AGENTS.md` 里写一条例外"的方案是
> 行不通的,已被实际运行否决。** 首次真实运行时向 `main` 的推送被远端拒绝
> (`GH013`:改动必须走 PR + 9 个必需 check)。`main` 有 ruleset,不是
> `AGENTS.md` 原文以为的"无机械保护"。
>
> **现行方案:摘要写到独立的 `pipeline-health` 分支**,通过 Contents API
> 提交,完全不碰 `main`。因此**不需要任何铁律例外**——那条例外已从
> `AGENTS.md` 删除。这比原方案更好:main 的保护保持绝对,不留一个必须被
> 信任的缺口。下面保留原文,作为"为什么不这么做"的记录。

~~如果直接提交到 `main` 与「`main` 不得接收直接提交」冲突——**它确实冲突**。
因此按以下方式解决，不得自行选别的：`main` 那条铁律的保护对象是**业务
变更**，健康摘要是机器生成的运行状态记录、无业务语义。在
`AGENTS.md` 铁律 8 处**显式写出这一条例外**，限定为：路径只能是
`docs/87-pipeline-health.json`、提交者只能是该 workflow、提交信息前缀
固定。**例外必须写进 `AGENTS.md`，不得只在本 TASK 里声明。**~~

## 约束

- **不得调用任何 LLM**，不得引用 `CLAUDE_CODE_OAUTH_TOKEN` 或
  `CLAUDE_PR_TOKEN`。
- **不得**改动 `ci.yml` / `security.yml` / `risk-classify.yml` /
  `auto-merge.yml` 的判定逻辑。可以 `import` `scripts/automerge_gate.py`，
  不得修改它的行为。
- **不得**让本 workflow 具备 `contents: write` 之外的任何写权限；
  不得给它 `pull-requests: write`。它只观察和记录，**不动 PR**。
- **不得**在状态无变化时产生提交或评论。
- **不得**回显任何 secret；`GITHUB_TOKEN` 不得出现在日志里。
- 摘要里**不得**包含审查正文（那是不可信外部文本），只保留结构化结论。

## 允许修改的文件

未列出的路径一律不得修改。

```
.github/workflows/pipeline-health.yml     # 新增
scripts/pipeline_health.py                # 新增（采集与摘要，纯函数可测）
backend/tests/guards/test_pipeline_health.py  # 新增
docs/87-pipeline-health.json              # 新增（初始内容为空摘要）
AGENTS.md                                 # 仅添加铁律 8 的那条例外，需人工确认
docs/83-project-continuity.md
docs/85-agent-operating-model.md
```

**明确不在范围内**：任何 `backend/app/**`、`frontend/**`、`ops/**`、
`infrastructure/**`、`deploy/**`。本任务不碰一行业务代码。

## 验收标准

```sh
python -c "import yaml;yaml.safe_load(open('.github/workflows/pipeline-health.yml'))"
make lint
python -m pytest backend/tests/unit backend/tests/guards
```

逐条给出**可复现**的验证，不接受"逻辑上应该成立"：

1. `main` CI 红 ⇒ `alerts` 含 `MAIN_CI_RED`。
2. 断路器为 `false` 或缺失 ⇒ `alerts` 含 `BREAKER_OFF`。
3. 一个 13 小时无新提交且有未解决 findings 的 PR ⇒ `alerts` 含
   `PR_STUCK_OVER_12H`；12 小时以内的同样 PR **不**产生该告警。
4. 返工 5 轮的 PR ⇒ `alerts` 含 `ROUNDS_AT_CAP`。
5. 连续 3 次自动合并 ⇒ `alerts` 含 `THREE_CONSECUTIVE_AUTOMERGES`。
6. 一切正常 ⇒ `alerts` 为 **空数组**，且**不产生提交**。
7. 只有 `generated_at` 变化 ⇒ **不产生提交**（见「摘要落在哪里」）。
8. `latest_verdict_on_head` 与 `scripts/automerge_gate.py` 在同一组输入上
   结论一致——用同一份 facts 跑两边，贴出两边输出。

验证方式按 TASK-S08 的先例：把判定抽成 `scripts/pipeline_health.py` 里的
纯函数，用 guard 测试逐条构造，**把真实输出贴进 PR 描述**。
不接受只贴 workflow 源码说"这里判断了"。

## 人工检查

- `AGENTS.md` 的改动需人工确认（写权限表规定）。
- 这个 PR 触及 `.github/workflows/` 与 `AGENTS.md`，属治理变更，
  **由 User 人工合并**，不走自动合并。
- PR 描述必须写明：这份摘要是 ADR-030 §3 判断层的唯一输入，
  **摘要漏报等于监督失效**——审查时请重点看 `alerts` 的覆盖是否与
  ADR-030 §5 的表逐条对应。
