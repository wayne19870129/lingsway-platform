# TASK-T19 — 修复 `.github/workflows/claude.yml` 的 self-cancellation bug(Issue #67)

风险等级:`risk-classify.yml` 对触碰 `.github/workflows/**` 的 PR 一律落
`risk:high`(与 T17/Issue #50 相同的分类先例),这里同样以自动分类结果
为准。本任务本身不涉及生产凭据、不涉及部署、不涉及数据库、不涉及业务
代码,纯粹是 CI 治理修复。

## 背景 / 根因

`.github/workflows/claude.yml` 同时监听 `issue_comment` 和
`pull_request_review_comment`,此前的 workflow-level concurrency 为:

```yaml
concurrency:
  group: claude-${{ github.event.issue.number || github.event.pull_request.number }}
  cancel-in-progress: true
```

这个 group key 只按 Issue/PR 编号区分,不区分触发者。真实复现过两次的
故障链路:

1. `wayne19870129` 发送合法 `@claude` 指令,触发一次真正的 Claude Code
   run(owner run);
2. `claude[bot]` 在处理过程中发出"Claude Code is working…"状态评论;
3. 这条 bot 评论本身又是一次新的 `issue_comment` 事件,触发同一个
   workflow 的又一次 run;
4. 因为 group key 只看 Issue 编号,这个 bot 触发的新 run 与步骤 1 的
   owner run 落在**完全相同**的 concurrency group;
5. workflow-level concurrency 在任何 job 的 `if:` 被求值**之前**就已经
   参与调度决策——`cancel-in-progress: true` 因此直接把仍在运行的 owner
   run 取消掉;
6. bot 触发的这次 run 随后才走到 job 级别的
   `github.event.comment.user.type != 'Bot'` 判断,被正常 skip——但为时
   已晚,真正干活的 run 已经被取消。

真实证据(Issue #67 原文引用):owner run `34696297261` → `cancelled`;
bot run `34696312829` → `skipped`。同样的模式在前一次尝试里也出现过一次。

结果:Issue-first 的 Claude Code 会话事实上无法稳定跑完(还没来得及建
分支/开 PR 就被自己的状态评论打断)。

## 修改方案

只改一行 group key 表达式,把触发者本身也纳入 group:

```yaml
concurrency:
  group: claude-${{ github.event.issue.number || github.event.pull_request.number }}-${{ github.actor }}
  cancel-in-progress: true
```

`github.actor` 对 `issue_comment` 和 `pull_request_review_comment` 两种
事件都等于"发出触发该事件的那条评论的用户",这一点本仓库自己的
`dedup`/`claude` 两个 job 的 `if:` 门禁已经在依赖同一个事实
(`github.actor == 'wayne19870129'`),不是新引入的假设。

没有采用"把 concurrency 移到 job 级别、靠 `if:`-skip 不占用 group"这条
路线,原因见下面「为什么没有选择 job-level concurrency」。

## 为什么 Bot 评论不再能取消 owner run

`cancel-in-progress: true` 的语义是:取消**同一个 concurrency group**里
仍在排队/运行的旧 run。取消能否发生,只取决于两个 run 的 group 字符串
是否相等——不依赖"谁的 job 门禁判断得早或晚"这类时序假设。

- owner 触发:`github.actor == 'wayne19870129'` → group =
  `claude-<number>-wayne19870129`
- `claude[bot]` 的状态评论触发:`github.actor` 是该评论实际的作者身份
  (例如 `claude[bot]` 或 workflow 用 `GITHUB_TOKEN` 发评论时表现出来的
  `github-actions[bot]`,具体取决于发评论时用的凭据)→ group =
  `claude-<number>-claude[bot]`(或等价的另一个 bot 身份字符串)
- 这两个字符串**按字符串比较必然不相等**(`wayne19870129` 不可能等于任何
  bot 账号的 login),所以 bot 触发的这次 run 从一开始就在一个和 owner
  run 完全不同的 concurrency group 里——它能取消的至多是它自己那个
  bot-only group 里的旧 run,永远碰不到 owner 的 group。

这个论证不依赖任何关于"workflow-level concurrency 到底在 job 的 `if:`
之前还是之后生效"的精确时序细节——只要两个 group 字符串不同,
`cancel-in-progress` 就是字面意义上的无效动作,不管调度器什么时候检查。
这是选择这个修复而不是"移到 job-level + 依赖 skip 语义"的核心原因:
后者的正确性依赖一个只能通过 GitHub 官方文档/实测才能确认、且本任务
执行环境的出站网络策略屏蔽了 `docs.github.com`、无法直接查证到权威原文的
细节(job 因 `if:` skip 时是否真的完全不参与 job-level concurrency
group 的排队/取消)。Group-key 按 actor 拆分则是纯字符串相等性论证,
可以脱离 GitHub Actions 调度器、用一个本地脚本完全证明(见下方「验证」),
不需要依赖任何未经证实的调度时序细节——这就是 Issue 里要求的"简单、
可证明"。

## 为什么新的 owner 指令仍然可以 supersede 旧 owner run

同一个 Issue/PR 上,`wayne19870129` 发出的两条不同 `@claude` 指令,
`github.event.issue.number`(或 PR number)相同、`github.actor` 也相同
(都是 `wayne19870129`)——两次的 group 字符串完全一样,`cancel-in-progress:
true` 在这个未变的语义下继续生效:更晚触发的 owner run 会取消掉同一个
group 里仍在跑的旧 owner run。这正是修复前后都需要保留的"新指令 supersede
旧指令"行为,这里没有被这次修改影响或削弱。

## 为什么没有选择 job-level concurrency

Issue 描述和一些常见实践建议把 concurrency 移到 `claude` job 级别,
利用"job 的 `if:` 为假时会被 skip、可能根本不占用 concurrency group"这个
特性,让 unauthorized/bot 触发的 run 因为被 skip 而完全不参与调度。这个
思路在网络可查的资料里得到部分印证(GitHub 官方 CLI/Discussion 提到过
"如果 workflow-level 和 job-level 用同一个 group 名字,job 会检测到死锁
直接 skip 报错"这样的交互细节,间接说明两个层级的 group 生效时机确实不同
),但没能查到一条可以直接引用的官方原文,明确回答"job 因 `if:` 为假被
skip 时,是否连 job-level concurrency 的排队/取消都完全跳过"——本任务
执行环境的出站网络策略屏蔽了 `docs.github.com`,只能通过搜索引擎摘要间接
验证,没有拿到可以在 PR 里作为证据引用的权威原文。

按 Issue 原文"先独立验证 GitHub Actions concurrency 语义,再采用最简单
且正确的方案,不要机械照抄"的要求:即使 job-level + if-skip 这个方案
经证实也是正确的,它也不比"group key 里直接加 actor"更简单——后者是
一行改动、纯字符串比较就能完整证明正确性,不需要额外假设任何调度时序
细节,而且不用把 concurrency 从 workflow 级别搬到 job 级别去重新组织
`dedup`/`claude` 两个 job 的关系。因此选择了对 group key 表达式做最小
改动的方案。

## 修改范围

只改了 `.github/workflows/claude.yml` 里 workflow-level `concurrency:`
块的 `group:` 一行表达式(新增 `-${{ github.actor }}` 后缀)及其上方的
说明注释;新增一个独立验证脚本和本 TASK 文档。

**没有改动**:
- `dedup`/`claude` 两个 job 的 `if:` 门禁条件(`github.actor ==
  'wayne19870129'`、`github.event.comment.user.type != 'Bot'`、
  `needs.dedup.outputs.proceed == 'true'`)——保持原样,继续是唯一能真正
  进入 Claude job 的授权判断。
- `dedup` job 的去重(exact-instruction de-dup)逻辑——完全未动。
- `claude` job 的 `permissions:`、`claude_args`、pinned action SHA、
  `--append-system-prompt` 里关于禁止自动 merge/deploy 的措辞——完全未动。
- 没有引入 auto-merge、auto-deploy、自动关闭 PR/Issue。
- 没有修改任何业务代码(`backend/**`、`frontend/**` 均未触碰)。
- 没有处理 Issue #66、Issue #68——本 PR 只解决 Issue #67。

## 验证

GitHub Actions 的 concurrency 调度器本身无法在本地或这个执行环境里真实
复现(没有触发真实 webhook 事件、没有观测真实 workflow run 状态的手段)。
按 Issue 原文要求,补充了一个可独立执行、结果确定性的验证脚本:
`scripts/validate-claude-workflow-concurrency.sh`。

这个脚本把 group 表达式 `claude-${{ number }}-${{ actor }}` 原样翻译成
一个 bash 字符串拼接函数(`group_for`),分别用真实场景里会出现的四类
actor 值(owner 本人、`claude[bot]`、另一个可能的 bot 身份
`github-actions[bot]`、任意一个非 owner 用户)代入,同时覆盖
`issue_comment`(Issue #66 场景)和 `pull_request_review_comment`
(PR #101 场景)两种事件对应的 number 来源,断言:

- owner 的两次指令(模拟"更新的指令")产生**完全相同**的 group 字符串
  (D:仍可 supersede);
- owner 与 `claude[bot]`/其它 bot 身份产生**不同**的 group 字符串
  (B:bot 永远不能取消 owner run);
- owner 与任意非 owner 用户产生**不同**的 group 字符串
  (C:unauthorized 用户永远不能取消 owner run);
- 上面三条在 `issue_comment` 和 `pull_request_review_comment` 两种触发
  来源下结果一致(E)。

实际执行(本次 PR 这次改动上真实跑过):

```
$ bash scripts/validate-claude-workflow-concurrency.sh
== Scenario: issue_comment trigger on Issue #66 ==
  owner run 1:        claude-66-wayne19870129
  owner run 2 (newer): claude-66-wayne19870129
  bot status comment: claude-66-claude[bot]
  other bot identity:  claude-66-github-actions[bot]
  unauthorized user:  claude-66-some-other-user

PASS: D: a newer owner instruction on the same Issue still supersedes the older owner run (both = 'claude-66-wayne19870129')
PASS: B: claude[bot]'s status comment can never cancel the owner run ('claude-66-wayne19870129' != 'claude-66-claude[bot]')
PASS: B: any other bot identity can never cancel the owner run either ('claude-66-wayne19870129' != 'claude-66-github-actions[bot]')
PASS: C: an unauthorized human commenter can never cancel the owner run ('claude-66-wayne19870129' != 'claude-66-some-other-user')

== Scenario: pull_request_review_comment trigger on PR #101 (same expression, different event) ==
  owner run 1:        claude-101-wayne19870129
  owner run 2 (newer): claude-101-wayne19870129
  bot status comment: claude-101-claude[bot]
  unauthorized user:  claude-101-some-other-user

PASS: E/D: PR review-comment trigger -- newer owner instruction still supersedes the older owner run (both = 'claude-101-wayne19870129')
PASS: E/B: PR review-comment trigger -- claude[bot]'s status comment can never cancel the owner run ('claude-101-wayne19870129' != 'claude-101-claude[bot]')
PASS: E/C: PR review-comment trigger -- an unauthorized commenter can never cancel the owner run ('claude-101-wayne19870129' != 'claude-101-unauthorized-run')

== Scenario: two different issues/PRs never share a group even for the same actor ==
PASS: different issue numbers for the same owner never collide ('claude-66-wayne19870129' != 'claude-101-wayne19870129')

All checks passed: 0 failures.
```

此外实际执行(本地真实跑过,输出见 PR body):
- `python -c "import yaml; yaml.safe_load(open('.github/workflows/claude.yml'))"` →
  YAML 语法合法。
- `bash -n scripts/validate-claude-workflow-concurrency.sh` → 语法检查通过。
- `shellcheck --severity=error scripts/validate-claude-workflow-concurrency.sh` →
  无 error 级别问题。

## 已知限制 / 未验证项

- 无法在这个执行环境里触发真实的 GitHub Actions 事件、真实观察一次
  workflow run 被取消或被 skip——这是 GitHub 托管的调度器行为,没有办法
  在仓库外或没有真实 webhook 事件的情况下复现。上面的脚本验证的是
  "group 字符串在不同 actor 下确实不同/相同"这个**充分且不依赖调度时序**
  的事实,而不是对 GitHub 调度器本身的端到端复现。
- `github.actor` 具体在 bot 评论场景下的字面值(`claude[bot]` /
  `github-actions[bot]` / 其它)取决于官方 `claude-code-action` 内部
  发评论时使用的凭据身份,这个仓库不控制、也没有必要精确得知——修复的
  正确性只依赖"这个值不等于 `wayne19870129`",不依赖具体是哪一个 bot
  身份字符串。
- 本次修复是仅针对 workflow 文件的治理修复,真正的端到端确认(下一次
  owner 在 Issue 或 PR 评论里 `@claude`、观察 bot 状态评论不再取消 owner
  run)只能在这个 PR 合并到 `main`、下一次真实 `@claude` 事件发生时才能
  最终确认。

## 允许修改的文件

- `.github/workflows/claude.yml`
- `scripts/validate-claude-workflow-concurrency.sh`(新增)
- `docs/82-tasks/TASK-T19-claude-workflow-concurrency-actor-scoping.md`(本文件,新增)

## 验收标准

- `.github/workflows/claude.yml` 的 concurrency `group:` 表达式包含
  `github.actor`。
- `dedup`/`claude` 两个 job 的授权门禁条件逐字未变。
- `scripts/validate-claude-workflow-concurrency.sh` 可独立执行,对
  owner/bot/unauthorized 三类 actor 在两种触发事件下的 group 字符串
  给出确定性的 PASS 结果。
- 不引入 auto-merge/auto-deploy/自动关闭 PR 或 Issue。
- 不修改业务代码。
- PR 目标分支为 `main`,由人工合并。
