# TASK-T21 — 后台 Claude Code 使用 rolling Sonnet 主模型 + medium effort（Issue #68）

风险等级：触碰 `.github/workflows/claude.yml` 的 PR 按既有先例
（T17/Issue #50/TASK-T19/TASK-T20）一律落 `risk-classify.yml` 的
`risk:high`，以自动分类结果为准。本任务本身不涉及生产凭据、不涉及
部署、不涉及数据库、不涉及业务代码，纯粹是 CI/Action 治理配置。

## 目标

按 Issue #68 的要求，让后台 `.github/workflows/claude.yml` 的**主
session**：

- 使用 Claude Code 官方 rolling 别名 `sonnet` 作为 model selector，
  不写死具体日期版本（如 `claude-sonnet-5`）；
- 显式配置 effort 为 `medium`，使用真实支持的非交互机制，不是靠
  prompt 措辞“伪造”；
- 保持 `--max-turns 30`；
- 不压制 Claude Code 正常的 Haiku 等辅助/后台/subagent 模型调用——
  `modelUsage` 里出现 Haiku 不代表主模型被切换。

同时扩展 run summary，让 configured selector/effort 与 actual
initialized main model/effort、以及 run 中实际用到的全部模型 ID
（`modelUsage`）分别可见，且不泄露原始 transcript/工具输出/密钥。

## 根因 / 验证方式

`.github/workflows/claude.yml` 之前的 `claude_args` 只有
`--max-turns 30` 和 `--append-system-prompt`，**没有设置任何
`--model`/`--effort`**——主 session 因此依赖 pinned action/CLI 的
未声明默认值。Issue #68 引用的真实 run（Issue #66）显示主 session 被
初始化为 `claude-sonnet-5`，且 `modelUsage` 里还出现了
`claude-haiku-4-5-20251001`——这是正常现象（Claude Code 的
subagent/辅助调用），不是需要压制的问题，但当前 workflow 完全没有
区分"配置的主模型"和"实际用到的所有模型"这两件事，也没有显式锁定
主模型的 rolling family。

没有凭猜测行事，而是直接拉取并核实了两处权威来源：

1. **pinned `anthropics/claude-code-action@0a8d3c9443bbff909ab973b6a17a340b913f229f`
   源码**（`base-action/src/parse-sdk-options.ts`）：
   - `claude_args` 里的 `--model <value>` 会被单独提取
     （`modelFromClaudeArgs`），映射到 Agent SDK 的
     `Options.model = options.model || modelFromClaudeArgs`——不是
     直接透传给 CLI 的 `extraArgs`，而是 SDK 的一等公民选项。
   - `--effort <value>` **不在**该文件单独提取的字段列表里（同
     `model`/`max-turns`/`allowedTools`/`mcp-config`/`json-schema`/
     `setting-sources` 不同）——它原样留在 `extraArgs` 里，随
     `sdkOptions.extraArgs` 一起透传给底层 CLI 进程。
   - action 本身的 `action.yml` **没有**任何独立的 `model:`/
     `effort:` 顶层 input——两者都必须通过 `claude_args` 配置，与
     `--max-turns`/`--append-system-prompt` 现在的做法完全一致。

2. **pinned action 依赖的确切
   `@anthropic-ai/claude-agent-sdk@0.3.268`**（`package.json` 里
   `^0.3.268`，本任务用 `npm pack` 拉取这个精确版本、解包后直接读它
   自带的 `sdk.d.ts`，而不是假设/凭记忆）：
   - `extraArgs?: Record<string, string | null>` 的文档原文就是
     "Additional CLI arguments to pass to Claude Code. Keys are
     argument names (without --), values are argument values"——
     证实 `--effort medium` 确实会被透传给真正的 Claude Code CLI
     进程。
   - CLI/Settings 的 `effort` 字段类型就是
     `'low' | 'medium' | 'high' | 'xhigh' | 'max'`（以及
     `CLAUDE_CODE_EFFORT_LEVEL` 环境变量、持久化的
     `effortLevel`/`maxEffortLevel` 设置）——是真实、非交互、文档化
     的机制，不是本任务编造的。
   - `sonnet` 是文档明确列出的 family alias 之一（同 `opus`/
     `haiku`/`fable`），且 `sdk.d.ts` 里有原话："Canonical wire
     model id this row's `value` resolves to (e.g. 'sonnet' ->
     'claude-sonnet-5')"——直接证实 `sonnet` alias 今天确实解析到
     `claude-sonnet-5`，而且这是**可以随 Anthropic 更新自动跟进**的
     alias，不是写死的日期版本。
   - `SDKSystemMessage`（`type: 'system', subtype: 'init'`）同时携带
     `model: string`（主 session 实际初始化的模型)和
     `effort?: ('low'|'medium'|'high'|'xhigh'|'max')|null`（"The
     effort level the session will send on its next request -- after
     ... model-support downgrades" —— 换句话说，即使某个具体模型
     悄悄不支持 `medium`、被降级，这个字段也会如实反映最终生效值，
     不是原样回显配置值)。
   - `SDKResultMessage`（`SDKResultSuccess`/`SDKResultError`）的
     `modelUsage: Record<string, ModelUsage>` 字段文档原文："Per-model
     totals for every model call made through the query pipeline
     during this query() call -- main loop, Task subagents,
     sidechains, and internal calls"——证实这个字段本来就包含
     Haiku 等辅助调用，是设计如此，不是 bug。

## 最终设计

### 1. `claude` job 新增 job-level `env:`，作为唯一事实来源

```yaml
env:
  CLAUDE_MODEL_SELECTOR: sonnet
  CLAUDE_EFFORT: medium
```

`claude_args` 与 run-summary step 都引用这两个 env 变量
（`${{ env.CLAUDE_MODEL_SELECTOR }}` / `${{ env.CLAUDE_EFFORT }}`），
避免同一个值在两处分别硬编码、将来改一处忘了改另一处的漂移风险。

### 2. `claude_args` 新增 `--model`/`--effort`

```
--max-turns 30
--model ${{ env.CLAUDE_MODEL_SELECTOR }}
--effort ${{ env.CLAUDE_EFFORT }}
--append-system-prompt "..."
```

不写死 `claude-sonnet-5`。System prompt 本身未改动语义（仍然是
Issue #71/TASK-T20 已经建立的版本），只是顺序上排在新增的
`--model`/`--effort` 之后。

### 3. `scripts/claude-run-summary.sh` 扩展为区分 configured/actual

新增两个必需输入：`CONFIGURED_MODEL_SELECTOR`、`CONFIGURED_EFFORT`
（由 `claude.yml` 从同一个 job-level env 传入）。摘要表格新增行：

- Configured main model selector（原样回显配置值，如 `sonnet`）
- Configured effort（原样回显配置值，如 `medium`）
- Actual initialized main model（来自 execution_file 里最后一条
  `system`/`init` 消息的 `model` 字段——**只反映主 session**，不包含
  subagent/辅助模型）
- Actual initialized effort（同一条 `init` 消息的 `effort`
  字段——SDK 文档承诺这是"any silent downgrade"之后的最终生效值）
- Models used during run（modelUsage）（最后一条 `result` 消息的
  `modelUsage` 对象的 key 列表，逗号分隔——如实展示 Sonnet 主模型
  + Haiku 等辅助模型全部出现，但不改变上面"actual main model"字段
  只认 `init.model` 的事实来源，因此 Haiku 出现在这里不会被误判为
  主模型切换）

其余字段（Turns/Estimated cost/Permission denials/Exit subtype/
is_error）逻辑不变。仍然只从 `execution_file` 里挑选这几个标量/
model-ID 字段，不读取、不输出任何 message 内容、工具调用参数或
原始 transcript；`display_report`/`show_full_output` 依然保持关闭。

### 4. 不动的部分（明确不在本任务范围）

- Issue #71/TASK-T20 建立的全部机制：`snapshot-scripts`
  trusted-copy 机制、`$RUNNER_TEMP` 执行边界、`pre_head_sha`
  false-completion 防护、同 Issue 重复 PR 的两层防护（preflight +
  branch-name 检测）、`claude-recovery` 恢复入口、owner-only 触发、
  bot 排除、actor-scoped concurrency、exact-instruction de-dup、
  human-only merge、no auto-deploy——全部原样保留，未做任何删减或
  弱化。
- `--allowedTools`——未新增任何工具权限。
- review round cap（`CLAUDE.md` 的 5 轮上限）——未修改。
- `AGENTS.md`——未修改。
- 业务代码（`backend/**`、`frontend/**`）——未触碰。
- Provider/Marzban/Xray/Mihomo——未触碰。
- `--max-turns`——保持 30，未改动。

## 验证

真实 GitHub Actions/Claude Code Action 的模型解析行为无法在这个
执行环境里端到端复现（没有真实触发 `@claude` 事件的手段）。已经
完成的验证：

1. **源码与 SDK 类型定义核实**：克隆 pinned `claude-code-action`
   并读取 `base-action/src/parse-sdk-options.ts`；用 `npm pack`
   拉取 `package.json` 精确 pin 的
   `@anthropic-ai/claude-agent-sdk@0.3.268`（与真实安装版本一致），
   解包后直接读取其 `sdk.d.ts`，逐条核实 `--model`/`--effort` 的
   真实处理路径、`sonnet` alias 的解析目标、`init` 消息的
   `model`/`effort` 字段、`result` 消息的 `modelUsage` 字段——
   全部基于实际拉取的源码/类型声明,不是凭记忆或猜测。
2. **`scripts/claude-run-summary.sh`**：新增
   `scripts/tests/test-claude-run-summary.sh`，用合成的
   `execution_file` JSON 覆盖三个场景：
   - Sonnet 主模型 + Haiku 辅助模型同时出现在 `modelUsage` 里，
     确认两者都显示，但"actual main model"字段仍然正确识别为
     Sonnet；同时确认一条含敏感占位文本的 assistant 消息完全不会
     出现在输出里。
   - configured selector（`sonnet`）与 actual 初始化模型
     （`claude-sonnet-4-6`，模拟解析到另一个具体版本）不同、且
     effort 被静默降级为 `null` 的场景——确认两者都被如实分别
     报告，不互相覆盖或吞掉。
   - execution file 缺失的场景——确认是干净的 no-op，不是崩溃。
3. **`.github/workflows/claude.yml`**：新增
   `scripts/tests/test-claude-workflow-model-config.sh`，静态断言
   `claude_args` 确实配置了 `--model ${{ env.CLAUDE_MODEL_SELECTOR }}`
   （值为 `sonnet`）、`--effort ${{ env.CLAUDE_EFFORT }}`（值为
   `medium`）、`--max-turns 30` 保持不变、run-summary step 的
   `CONFIGURED_MODEL_SELECTOR`/`CONFIGURED_EFFORT` 引用同一个
   job-level env（防止漂移）、文件里没有任何非注释行把
   `claude-sonnet-5` 当作实际配置值硬编码。
4. **回归**：重新跑了 Issue #71/TASK-T20 的三个既有测试脚本
   （`test-claude-workflow-trust-boundary.sh`、
   `test-claude-ensure-pr-and-dispatch.sh`、
   `test-claude-issue-open-pr-filter.sh`），确认全部仍然通过，本次
   改动没有影响任何既有安全边界。
5. **静态检查**：`python3 -c "yaml.safe_load(...)"` 对
   `claude.yml`/`ci.yml` 均通过；`bash -n` + `shellcheck
   --severity=error` 对所有 `scripts/*.sh`/`scripts/tests/*.sh`
   （含新增的三个文件）均无 error。

## 仍需 post-merge 验证的项目（明确标注为 `unverified until
post-merge live test`，不假装已经验证）

- 一次真实的 owner Issue-first 或 PR-rework `@claude` 触发，实际把
  `--model sonnet --effort medium` 传给真实的 pinned action/CLI，
  且真实产生的 `execution_file` 里 `system`/`init` 消息的
  `model`/`effort` 字段形状，与本任务基于 SDK 类型声明推断的完全
  一致。
- 真实运行下 `sonnet` alias 今天确实解析为 `claude-sonnet-5`（本
  任务只核实了 SDK 自带文档字符串里的这个例子，没有触发真实 API
  调用去观察实际解析结果）。
- 真实运行下 `--effort medium` 确实被当前 Sonnet 模型接受、不触发
  静默降级（`init.effort` 字段会在真实 run 里如实反映，但这个字段
  本身的真实取值需要一次真实 run 才能看到）。
- 真实 `modelUsage` 在一次典型任务里的实际内容（是否真的会包含
  Haiku、还有哪些内部模型）与本任务合成测试数据的假设是否完全
  吻合。
- run summary 新增字段在真实 GitHub Actions Step Summary 渲染中的
  实际可读性/格式表现。

## 允许修改的文件

- `.github/workflows/claude.yml`
- `.github/workflows/ci.yml`
- `scripts/claude-run-summary.sh`
- `scripts/tests/test-claude-run-summary.sh`（新增）
- `scripts/tests/test-claude-workflow-model-config.sh`（新增）
- `docs/82-tasks/TASK-T21-claude-workflow-rolling-sonnet-medium-effort.md`
  （本文件，新增）

## 验收标准

- `claude.yml` 的主 session `claude_args` 显式配置
  `--model sonnet`（rolling alias，不是 `claude-sonnet-5` 之类的
  日期版本 ID）。
- `claude_args` 显式配置 `--effort medium`，机制真实存在（非
  prompt 措辞伪造），已对照 pinned action 和精确 pin 的 Agent SDK
  版本核实。
- `--max-turns` 保持 30。
- run summary 能分别显示 configured selector/effort、actual
  initialized main model/effort、以及 run 中出现的全部
  `modelUsage` 模型 ID，且不因为 Haiku 等辅助模型出现就误判主模型
  被切换。
- run summary 不暴露原始 transcript、工具调用内容、prompt 内容或
  secrets；不开启 `display_report`/`show_full_output`。
- Issue #71/TASK-T20 建立的全部安全边界（trusted snapshot、
  pre-head-SHA 防护、同 Issue 重复 PR 防护、recovery 路径、
  owner-only 触发、bot 排除、actor-scoped concurrency、
  exact-instruction de-dup、human-only merge、no auto-deploy）
  原样保留，回归测试全部通过。
- 不修改 `AGENTS.md`、业务代码、review round cap、tool permission
  policy。
- PR 目标分支为 `main`，由人工合并。
