# TASK-T20 — 修复 Claude Code workflow 的工具权限不匹配与 turn 浪费(Issue #71)

风险等级:触碰 `.github/workflows/claude.yml` 的 PR 按既有先例
(T17/Issue #50/TASK-T19)一律落 `risk-classify.yml` 的 `risk:high`,以
自动分类结果为准。本任务本身不涉及生产凭据、不涉及部署、不涉及数据库、
不涉及业务代码,纯粹是 CI/Action 治理修复。

## 背景 / 根因

真实 GitHub Actions run `#103`(处理 Issue #66)在 31 turns 后因
`Reached maximum number of turns (30)` 失败,日志显示
`permission_denials_count: 13`。但失败之前 Claude 已经正确修改了
`CLAUDE.md`、commit、push 到远程分支——真正的实现工作已经完成。

`.github/workflows/claude.yml` 之前的 `claude_args` 只设置了
`--max-turns 30` 和 `--append-system-prompt`,**没有设置任何
`--allowedTools`**。System prompt 里明确要求 Claude 在 push 之后:

- 用 `gh pr create` 开 PR(Issue-first 场景);
- 用 `gh workflow run ci.yml` / `security.yml` / `risk-classify.yml`
  重新触发这三个因为 `GITHUB_TOKEN` 反递归规则而不会自动跑的 workflow。

直接克隆并检出 pinned commit
(`anthropics/claude-code-action@0a8d3c9443bbff909ab973b6a17a340b913f229f`)
的源码来核实这个假设,而不是猜测:

- `action.yml` 里**没有** `prompt:` 之外任何顶层 `allowed_tools` 输入
  会被我们的 workflow 设置——我们的 workflow 也没有设置 `prompt:` 输入,
  这意味着这个 Action 走的是 **tag mode**(`src/modes/tag/index.ts`),
  不是 agent mode(agent mode 只在显式提供 `prompt` 时才启用,见该文件
  自己的文档字符串)。
- `src/modes/tag/index.ts` 里 tag mode 的 `tagModeTools`(实际生效的
  `--allowedTools` 硬编码默认值)是:

  ```
  Glob, Grep, LS, Read,
  mcp__github_comment__update_claude_comment,
  mcp__github_ci__get_ci_status,
  mcp__github_ci__get_workflow_run_details,
  mcp__github_ci__download_job_log,
  Bash(git add:*), Bash(git commit:*), Bash(<push wrapper>:*), Bash(git rm:*)
  ```

  `Edit`/`MultiEdit`/`Write` 有意不在这个列表里——源码注释解释这是因为
  `--permission-mode acceptEdits` 已经在 `$GITHUB_WORKSPACE` 内自动放行
  文件编辑,单独把它们列进 allowlist 会变成对整个 runner(包括
  `$GITHUB_WORKSPACE` 之外的路径,例如 `~/.bashrc`)的无差别写权限。
  **这个默认列表里完全没有 `gh`**,过去也从来没有。
  `docs/capabilities-and-limitations.md` 也明确写着:"By default, Claude
  cannot execute Bash commands unless explicitly allowed using the
  `allowed_tools` configuration"。

- 我们的 workflow 从未在 `claude_args` 里追加过 `--allowedTools`,所以
  `tagModeTools` 就是 Claude 实际拥有的全部工具——比 system prompt
  要求的能力窄得多。

结论:**system prompt 要求 Claude 做的事(`gh pr create`、`gh workflow
run ...`),Claude 从来没有权限做**。headless SDK 没有交互式确认可以
"等待批准",所以每次调用这些命令都是硬拒绝(permission denial),不是
排队等待——Claude 于是反复重试,直到耗尽 `--max-turns 30`。这正是
`permission_denials_count: 13` 的来源。

**额外发现(同样来自读源码,而不是原来的 system prompt 假设)**:旧版
system prompt 还告诉 Claude,在 Issue-first 场景下"先检查这个 Issue
是否已经有分支/PR,如果有就复用它,不要重新开始"。但
`src/github/operations/branch.ts::setupBranch()` 显示,对于 Issue
触发(或已关闭/已合并 PR 触发),tag mode **总是**在 Claude 的第一个
turn 开始之前就已经确定性地创建了一个全新分支(名字模板里带
`{{timestamp}}`),完全不会去搜索或复用同一 Issue 之前的分支/PR。也就
是说,旧 prompt 里那条指令在 Claude 拿到第一个 turn 之前就已经不可能
被执行——这也是浪费 turn 的另一个来源(Claude 可能会尝试
`gh pr list`/`git branch -a` 之类的操作去核实,同样被拒绝或找不到
结果)。

## 目标(与验收标准对齐 Issue #71)

不是简单调大 `--max-turns`。优先级:

1. 让 system prompt 要求的能力 == Claude 实际被允许的能力。
2. 优先用确定性的 workflow step 完成 PR 创建 / CI-Security-Risk
   dispatch,而不是给 Claude 更宽的 Bash/`gh` 权限。
3. 只有确实必须由 Claude 自己执行的操作,才增加最小必要的
   `--allowedTools`。
4. 提供失败恢复路径,避免已完成实现工作后仅仅因为一个确定性步骤失败
   就要重新支付一次完整模型执行成本。
5. 在不泄露 secrets/工具原始输出的前提下,让 model/turns/cost/
   permission-denial/exit-reason 在 Actions 里可见。

## 最终设计

### 1. `--allowedTools`:本次改动**没有新增任何一条**

因为把 PR 创建和 CI/Security/Risk dispatch 整体移出了 Claude 的推理
循环(见下),tag mode 默认的
`Glob/Grep/LS/Read` + `git add/commit/push/rm` 已经足够 Claude 完成
"实现变更 → commit → push"这整条链路——不需要为了让 Claude 自己跑
`gh pr create`/`gh workflow run` 而放宽 `--allowedTools`。这是比"给
Claude 加 `Bash(gh pr create:*)`/`Bash(gh workflow run:*)`"更小的改动
面,完全符合 Issue 原文"确定性 workflow step 优先于给 Claude 宽泛 shell
权限"的要求——本次改动权限面是**收紧**而不是放宽(见下面"移除
`GH_TOKEN` 环境变量")。

### 2. System prompt:诚实描述 Claude 实际能做什么

重写 `claude_args` 里的 `--append-system-prompt`:

- 删除"运行 `gh pr create`"、"运行 `gh workflow run ci.yml/
  security.yml/risk-classify.yml`"的指令,改为明确告知 Claude:
  "你没有 `gh` 命令或任何通用 shell 访问权限,只有 Glob/Grep/LS/Read
  和 `git add`/`git commit`/`git push`/`git rm`;不要尝试
  `gh pr create`、`gh workflow run` 或任何其它 `gh`/shell
  命令,会被拒绝,重试只会浪费 turn。"
- 删除"检查 Issue 是否已有分支/PR、复用它"的指令(如上所述,这条在
  tag mode 下从一开始就不可执行),改为准确描述:"你已经在一个为这个
  Issue 新建的分支上;不要去找或试图复用更早的分支/PR(同一个 Issue
  上的不同指令按本仓库'一个任务一个 PR'的约定,本来就应该各自拥有
  自己的分支/PR)——直接在当前分支上实现、commit、push。"
- 新增:要求 Claude 把**最后一次 commit** 的 message 写成完整的 PR
  描述——subject 行是可以直接当 PR 标题用的一句话,正文包含
  `## Summary`、`## Acceptance criteria`、`## Verification`、
  `## Known gaps / risks` 四个小节,并且**只有在这次改动确实完整解决了
  触发该次运行的 GitHub Issue 时**,才另起一行写
  `Closes #<issue-number>`。明确告知 Claude:这条 commit message 会被
  一个确定性的 workflow step 原样读出来当作 PR body,不是写给自己看的
  commit log 备注。
- PR-comment(rework)触发场景:保留"直接推送到当前已检出的这个 PR
  分支,不要开第二个 PR"的指令,并说明这个分支已经有 PR 了,所以
  commit message 不需要完整的 PR 描述小节,给 reviewer 看得懂的上下文
  就够。
- 保留原有的"绝不 merge/close/deploy"、"只读取真正相关的评论/diff/
  最新未过期的审查、不要重新分析已经修过的旧一轮审查、审查引用的 SHA
  如果不是当前 head 就是过期的"这些效率与安全指令,完全未改动其含义。

### 3. PR 创建 + CI/Security/Risk dispatch:改成确定性 workflow step

新增 `scripts/claude-ensure-pr-and-dispatch.sh`,作为 `claude` job 里
`claude-code-action` 步骤**之后**的一个普通 shell step(`id: ensure-pr`,
`if: always()`)运行,不消耗任何 Claude turn:

1. 确定目标分支:优先用 `steps.claude.outputs.branch_name`(仅当 tag
   mode 真的新建了分支时才有值,见根因部分);如果为空(说明这是
   PR-rework 触发,Claude 直接推到了已存在的 PR 分支上,而不是一个新
   分支),回退到用 `gh pr view <target_number> --json headRefName`
   解析出那个已存在 PR 自己的分支名(`target_number` 直接复用
   `dedup` job 已经算好的同一个值,不用重新从事件 payload 推导)。
2. 如果两条路径都拿不到分支名,说明这一轮什么都没有发生(例如更早的
   `if:` 门禁没通过,或 prepare 阶段本身失败),直接退出,不做任何事。
3. 用 `git ls-remote`/`git rev-list --count` 确认这个分支真的存在、
   真的比 base branch 多至少一个 commit——没有就退出,不做任何事
   (避免为空分支开一个空 PR)。
4. 用 `gh pr list --head <branch> --state all --json number` 检查这个
   分支是否已经有 PR。**有就直接复用**(不管是 PR-rework 场景下"本来
   就有"的那个 PR,还是 Issue-first 场景下上一次调用这同一个脚本已经
   建好的 PR)——同一个分支永远不会被开出第二个 PR,`if: always()` 的
   重复调用天然幂等。
5. 没有就用 `git log -1 --format=%s`/`%b` 取这个分支最新 commit 的
   subject/body,原样传给 `gh pr create --title ... --body ...`
   (默认非 draft,满足验收标准)。
6. 不管是刚建的还是复用的 PR,都执行
   `gh workflow run ci.yml --ref <branch>`、
   `gh workflow run security.yml --ref <branch>`,如果拿到了 PR 号,
   再执行 `gh workflow run risk-classify.yml --ref <branch> -f
   pull_number=<N>`——这三行和旧 system prompt 里要求 Claude 做的完全
   一样,只是现在由一个普通 shell step、用 job 自己已有的
   `GITHUB_TOKEN`(`actions: write`/`pull-requests: write` 权限不变)
   确定性执行,不再经过 Claude 的推理循环、不再可能被拒绝。

`if: always()` 是关键:即使 `claude-code-action` 这一步本身报告失败
(比如撞到 `--max-turns`),只要它已经真的 push 了 commit,这一步依然
会运行、依然会开 PR、依然会 dispatch 检查——这正好对应 run #103 的
真实场景(实现已经做完、只是后续步�具因为工具权限不匹配而失败)。

### 4. 失败恢复:workflow_dispatch 恢复入口 + 幂等脚本复用

给 `claude.yml` 新增一个 `workflow_dispatch` 触发(输入:`branch`,
必填)和一个新 job `claude-recovery`(owner-only,不调用
`claude-code-action`,只 checkout 后跑同一个
`scripts/claude-ensure-pr-and-dispatch.sh`)。

设计理由:

- **`ensure-pr` 这一步本身的失败(网络抖动、`gh` API 临时错误等,和
  "工具权限不匹配"是两类不同的失败)才需要这条恢复路径**——工具权限
  不匹配这一类失败,`if: always()` 已经让同一次 run 自己恢复了,不需要
  额外的人工介入。
- 复用同一个脚本(不是重复实现一遍逻辑),保证正常路径和恢复路径的
  行为完全一致、不会出现"正常路径修了一个 bug,恢复路径忘记同步修"
  这种漂移。
- `workflow_dispatch` 本身已经要求触发者拥有仓库写权限,但为了和这个
  文件里其它每一处 actor 检查保持一致的防御深度,`claude-recovery` job
  仍然显式加了 `github.actor == 'wayne19870129'` 门禁。
- 恢复路径完全不涉及 Claude/`CLAUDE_CODE_OAUTH_TOKEN`,因此**不会
  重新支付一次完整模型执行成本**——这正是 Issue #71 第 5 条要求的
  "避免再次支付完整模型执行成本"。

### 5. `--max-turns`:保持 30,不改

按 Issue 原文"先解决根因,若有证据表明 30 仍不合理再给出具体数据和
理由;否则保持 30"——本次修复直接消灭了造成 13 次 permission denial、
进而把 turn 数从"实现所需的真实 turn 数"推高到 31、超过 30 的那个
根因。修复后 Claude 需要做的事情(读评论/diff → 实现 → commit → push)
比之前少了"反复尝试被拒绝的 `gh` 调用"和"徒劳检查一个不可能存在的
可复用分支"这两类无意义 turn,预期同样复杂度的任务所需 turn 数会明显
低于 31。因为改动本身还没有真实触发过一次 Issue-first 运行(见下面
"仍需 post-merge 验证"),**没有实际数据支持现在就调整 30**,所以维持
不变;如果 merge 后的真实运行仍然频繁碰到 30 turns 上限,应该带着那次
运行的真实 `num_turns`/`permission_denials_count`(现在通过下面第 6
条的运行摘要就能直接看到)重新开一个 Issue 讨论是否需要调整,而不是
在这个 PR 里预先加大。

### 6. 可观测性:新增 `scripts/claude-run-summary.sh`

`claude-code-action` 自己的 `display_report`/`show_full_output` 两个
输入都会把完整的逐 turn 记录(包括每次工具调用的原始结果)写进公开的
Actions 日志/Step Summary——`show_full_output` 自己的官方说明就写着
"may contain secrets, API keys, or other sensitive information"、
"These logs are publicly visible in GitHub Actions"。这正是 Issue
原文明确禁止的"危险 full-output 模式",本次改动没有打开这两个开关
(两者维持默认的 `false`)。

改为新增一个独立、`always()` 执行的 shell step,只从
`steps.claude.outputs.execution_file`(这个文件本身包含完整原始
turn 数据,但只存在于 runner 磁盘上,不会自动出现在任何日志里)里用
`jq` 挑出四个标量字段——model、`num_turns`、`total_cost_usd`、
`permission_denials` 数组长度、退出 `subtype`/`is_error`——写进
`$GITHUB_STEP_SUMMARY`,**从不**输出原始文件、从不输出任何消息正文、
从不作为 artifact 上传。这组字段和 Issue #71 原文引用的 run #103
日志字段(`model`/`num_turns`/`total_cost_usd`/
`permission_denials_count`)逐一对应。

### 移除的一处不必要 token 暴露

`claude-code-action` 步骤原来有一行
`env: { GH_TOKEN: ${{ github.token }} }`,注释写着"让 Claude 的
shell 工具里的 `gh` 调用可以用这个 token 认证"。但由本任务对源码的
核实可知,Claude 在 tag mode 下的 `--allowedTools` 里从来没有任何
`gh` 相关模式,这个 token 从一开始就不可能被 Claude 实际用上
(它能触发的 Bash 模式就只有 `git add/commit/push/rm`,推送凭据由
action 自己的 `configureGitAuth`/push wrapper 处理,不依赖这个环境
变量)。本次改动直接删除这一行——不是新的收紧,而是移除一个此前从未
真正被使用过、纯粹增加 token 暴露面的死配置,符合最小权限原则。

## 已确认不改动的部分(明确不在本任务范围)

- `dedup`/`claude` 两个 job 的 owner-only `if:` actor 门禁——逐字未变
  (只多加了 `github.event_name != 'workflow_dispatch'` 一个额外条件,
  防止新增的 `workflow_dispatch` 触发器意外命中这两个 job;原有的
  `github.actor == 'wayne19870129'` 判断完全没变)。
- bot 评论排除条件(`github.event.comment.user.type != 'Bot'`)——未变。
- TASK-T19(Issue #67)的 actor-scoped concurrency
  group(`claude-<number>-<actor>`)——完全未触碰这个 `concurrency:`
  块。
- exact-instruction de-dup 的 hash/marker 计算逻辑
  (`dedup` job 的 `check` 步骤)——完全未触碰;唯一相关的改动是
  `claude` job 里"何时才算完成、可以打 marker"的条件(见下一条),
  不是 dedup 本身怎么算 hash/marker。
- 完成 marker 的**判定条件**从 `success()` 改为
  `always() && (claude 步骤成功 || ensure-pr 步骤真的确定了 PR
  号)`——这个改动直接服务于 Issue #71 第 5 条"避免重复支付完整模型
  执行成本"的要求(一次因为旧 bug 而"报告失败但其实已经真正做完工作"
  的 run,不应该让下一次同样的指令重新触发一次完整 Claude 执行),
  不是放松 dedup 的判定标准本身。
- `AGENTS.md`——未修改。
- 业务代码(`backend/**`、`frontend/**`)——未触碰。
- Issue #68——未处理,本 PR 只解决 #71。
- 没有固定/修改任何模型或 effort 配置(`claude_code_oauth_token`、
  Claude 使用的模型选择完全未涉及)。
- 没有引入 auto-merge、auto-deploy、自动关闭 PR/Issue。
- `timeout-minutes`(`claude` job 20 分钟、`dedup`/`claude-recovery`
  分别 5 分钟)——未改动既有的 20 分钟;`claude-recovery` 是新 job,
  给了一个保守的 5 分钟(纯 `gh`/git 调用,不涉及模型)。

## 验证

真实的 GitHub Actions / Claude Code Action 调度行为无法在这个执行
环境里端到端复现(没有真实触发 `@claude` 事件、没有观察真实 run 的
手段)。已经完成的验证:

1. **根因验证**:直接克隆
   `anthropics/claude-code-action` 并 checkout 到 pinned commit
   `0a8d3c9443bbff909ab973b6a17a340b913f229f`,读取
   `action.yml`、`src/modes/tag/index.ts`、
   `src/github/operations/branch.ts`、`src/entrypoints/run.ts`、
   `base-action/src/run-claude-sdk.ts`、
   `base-action/src/execution-file.ts` 源码,逐条确认:tag mode 默认
   `--allowedTools` 里没有 `gh`;`branch_name` 输出只在新建分支时才有
   值;`execution_file` 输出即使在失败路径也会被设置;`execution_file`
   写入的是未经过滤的完整消息数组(需要自己用 `jq` 挑字段,不能整个
   读出来当摘要)。
2. **`scripts/claude-ensure-pr-and-dispatch.sh`**:用一个本地临时
   bare git 仓库 + 一个模拟真实 `--json`/`--jq` 语义的 `gh` stub
   脚本,实际跑过三种场景并核实输出:
   - 场景一(无已有 PR、有新 commit)→ 创建 PR、拿到编号、
     dispatch 三个 workflow、写出 `pr_number` 输出。
   - 场景二(已有 PR)→ 复用已有编号,**不**调用 `gh pr create`,
     仍然 dispatch 三个 workflow。
   - 场景三(分支没有比 base 多任何 commit)→ 直接退出,不调用任何
     `gh` 命令。
   三种场景全部符合预期(见 PR 描述里贴出的实际终端输出)。
3. **`scripts/claude-run-summary.sh`**:用一个手工构造、字段值与
   Issue #71 引用的真实 run #103 完全一致的 `execution_file`
   JSON(`model=claude-sonnet-5`、`num_turns=31`、
   `total_cost_usd=0.64365`、13 个 `permission_denials`、
   `subtype=error_max_turns`、`is_error=true`,外加一条包含敏感占位
   文本的 assistant 消息)运行脚本,确认摘要表格精确复现这五个字段,
   且那条 assistant 消息的文本完全没有出现在输出里。
4. **静态检查**:`python3 -c "yaml.safe_load(...)"` 确认
   `claude.yml` 语法合法;`bash -n` + `shellcheck --severity=error`
   对两个新脚本和 `ensure-pr` 步骤里内联的 bash 块(单独抽出后跑
   shellcheck)均无 error。

## 仍需 post-merge 验证的项目(明确标注为 `unverified until
post-merge live test`,不假装已经验证)

以下条目只能在这个 PR 合并到 `main` 之后,由一次真实的 `@claude`
Issue-first 触发来验证——`unverified until post-merge live test`:

- owner Issue-first 触发能真正进入 Claude、实现改动、commit、push,
  且这一次不再出现旧版那 13 次 permission denial。
- `ensure-pr` 步骤在真实 Actions 环境里成功创建一个 non-draft PR,
  PR body 确实是 Claude 最后一次 commit message 的原文,且 CI/
  Security/Risk 三个 workflow 确实被成功 dispatch。
- `claude-code-action` 报告的实际 `branch_name`/`execution_file`
  输出在真实 run 里的确切取值与本任务基于源码读出的预期一致(本地
  测试用的是手工构造的替身数据,不是这个 Action 真实产生的数据)。
- PR-rework 场景(在已有 PR 上评论 `@claude`)下,`branch_name`
  输出确实为空、`ensure-pr` 步骤确实成功用
  `gh pr view --json headRefName` 解析出正确的分支名。
- `claude-recovery` 这个新 `workflow_dispatch` 入口在真实仓库里可以
  被 `wayne19870129` 手动触发并成功执行。
- 一次真实 Issue-first 运行的实际 `num_turns` 是否明显低于之前的
  31——用来判断 `--max-turns 30` 目前是否确实已经足够(见上面第 5
  条,如证据显示仍不足,应该另开 Issue 并附上这次真实数据讨论,不在
  本 PR 里预先调整)。
- `scripts/claude-run-summary.sh` 处理真实(而不是本任务手工构造)
  `execution_file` JSON 的实际结构与本任务读源码得出的字段名假设
  完全吻合(未能在 `node_modules` 未安装的情况下找到 SDK 的 TypeScript
  类型定义原文做逐字段交叉核对,只读到了 `base-action/src/
  run-claude-sdk.ts` 里手写的 `sanitizeSdkOutput()` 函数对这些字段名
  的引用)。

## 允许修改的文件

- `.github/workflows/claude.yml`
- `scripts/claude-ensure-pr-and-dispatch.sh`(新增)
- `scripts/claude-run-summary.sh`(新增)
- `docs/82-tasks/TASK-T20-claude-workflow-tool-permission-mismatch.md`
  (本文件,新增)

## 验收标准

- `claude.yml` 的 system prompt 不再要求 Claude 执行任何它没有权限
  执行的操作。
- `claude_args` 没有新增任何 `--allowedTools`。
- PR 创建、CI/Security/Risk dispatch 由确定性 workflow step 完成,
  `if: always()`,不消耗 Claude turn,即使 `claude-code-action` 步骤
  本身报告失败也能在已有真实 commit 的前提下正常完成。
- 存在一个不需要重新调用 `claude-code-action` 的确定性恢复入口
  (`workflow_dispatch` + `claude-recovery` job)。
- `--max-turns` 保持 30。
- 新增可观测性 step 只暴露 model/turns/cost/permission-denials/
  exit-subtype 五个标量字段,不暴露原始工具输出或 secrets。
- 不引入 auto-merge/auto-deploy/自动关闭 PR 或 Issue。
- 不修改业务代码、不修改 `AGENTS.md`。
- PR 目标分支为 `main`,由人工合并。
