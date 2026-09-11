# TASK-T18 — 自动创建 PR + 自动返工 + 条件自动合并

风险等级:high(涉及无人工点击即把代码 squash merge 进 main 的自动化,
且是这个仓库第一次允许这种事情发生;不涉及生产部署本身)

本任务在 TASK-T17(`.github/workflows/claude.yml`,让 `@claude` 能触发一次
会话)的基础上,扩展出三段此前明确排除在外的能力:自动创建 PR、自动返工、
**有条件的自动合并**。这是对 `AGENTS.md`「铁律」第 8 条("不得自行 merge
PR")和 `CLAUDE.md`"No agent merges a PR automatically"的一次**用户明确
授权的例外**,不是绕过——具体的例外范围、触发条件和排除项见下文,超出这个
范围的任何合并仍然必须人工完成。

## 目标

1. **自动创建 PR**:Issue 里的有效 `@claude` 任务执行完成后,Claude 直接用
   `gh pr create` 开出一个非 Draft PR(而不是只留一个预填链接等人点),
   PR 正文包含:对应的 Issue/TASK、改动摘要、验收标准、Claude 实际跑过的
   测试结果、未完成验证和已知风险。重复触发时先查找该 Issue 是否已有分支
   /PR,有则复用,不新开第二个。
2. **自动返工**:ChatGPT Work 对最新 head SHA 给出 `NEEDS_CHANGES` 后,
   Claude 核实每条 Critical/Major,在原分支修复、推送新 commit——这部分
   `claude.yml` 在 T17 里已经支持(PR 评论触发时推到当前分支、不新开 PR),
   本任务只是把它和"自动创建 PR"的分支路径打通,并加上强制重新触发
   CI/Security/Risk-classify 的步骤(见下面"已知限制")。
3. **条件自动合并**:新增 `.github/workflows/claude-automerge.yml`,只有在
   下面"合并门禁的九个条件"**全部满足**时才 `gh pr merge --squash`,否则
   什么都不做(或者只在需要人工介入的情况下留一条评论说明原因)。

## 用户已经做出的选型决定(不是本任务自行假设的)

实现前用 `AskUserQuestion` 向用户确认过三个关键设计分支,用户的选择是:

1. **PASS 校验方式:仅做文本 + head SHA 匹配,不做身份校验。**
   用户明确知道且接受这个残留风险:ChatGPT Work 的评论和这个仓库里人工
   发的评论、以及这个 Claude Code 会话本身发的评论,都显示成同一个 GitHub
   账号(`wayne19870129`,`author_association: OWNER`)。GitHub API 拿不到
   比"评论作者账号"更强的身份信号,所以`claude-automerge.yml`**没有**、
   也**不可能**加密验证"这条 PASS 真的是 Work 发的"——它做的是:
   - 评论必须严格匹配 Work 固定格式(`## Verdict` 后紧跟 `PASS` 或
     `NEEDS_CHANGES`,`author_association == OWNER`);
   - 评论里引用的 head SHA 必须和 PR **当前**的 head SHA 完全一致
     (过期 SHA 的 PASS 一律当作旧信息,不生效);
   - 比 PASS 更晚的 `NEEDS_CHANGES` 会让之前的 PASS 失效。
   这三条挡得住"过期评论"「顺序错乱」「明显不是 Work 格式的普通聊天」,
   但挡不住"有人手动复制 Work 的格式、编个假 SHA 发一条评论"——这一点在
   `claude-automerge.yml` 顶部注释和下面"安全边界"一节里明确写出来,不假装
   已经解决。**真正防止 Claude 自己伪造 PASS 的机制,是 Claude 的行为约束
   本身**(见 `.github/workflows/claude.yml` 里新增的那句提示词:禁止
   Claude 自己写出或转述 `## Verdict\nPASS` 这种格式,也禁止谎称某次 Work
   审查给了 PASS)——这不是技术门禁能拦住的,GitHub 这边看不出这条会话和
   Work 的区别,只能是"Claude 不这样做"这条纪律本身。
2. **Token 方案:直接用默认 `GITHUB_TOKEN`,接受由此产生的限制。**
   用户没有要求另建一个最小权限的 GitHub App。真实后果(GitHub 官方行为,
   不是本仓库的 bug):用 `GITHUB_TOKEN` 创建的 PR 或推送的 commit,**不会**
   触发 `pull_request`/`push` 类型的事件(防止递归触发的官方设计),而
   `ci.yml`、`security.yml`、`risk-classify.yml` 目前只监听这些事件——这意味着
   如果什么都不做,Claude 自己开的 PR 和自己推的返工 commit 完全不会跑CI,
   直接违反目标 1 里"必须正常触发 CI/Security/Work 审查"这条验收要求。
   **缓解方式**(仍然不需要 PAT/App,`workflow_dispatch` 和
   `repository_dispatch` 明确被 GitHub 排除在"不触发新 run"的限制之外):
   - 给 `ci.yml`、`security.yml`、`risk-classify.yml` 各加一个
     `workflow_dispatch` 触发入口(`risk-classify.yml` 因为脚本依赖
     `context.payload.pull_request`,额外加了一个 `pull_number` 输入);
   - `claude.yml` 的提示词要求 Claude 每次 push 之后,自己用
     `gh workflow run ci.yml --ref <branch>` /
     `gh workflow run security.yml --ref <branch>` /
     `gh workflow run risk-classify.yml --ref <branch> -f pull_number=<N>`
     手动把这三个工作流跑起来。
   - ChatGPT Work 的审查**不受这个限制影响**——它是外部读取 PR 状态给出
     审查,不是被 GitHub event 触发的 Actions workflow。
   这个缓解方式覆盖了"CI 必须真的跑起来",但仍然是一个真实存在的额外
   环节(Claude 必须记得手动触发),不如原生 GitHub App 身份下 PR 事件
   自然触发来得可靠——这一点如实记录,不算已经等价于原生行为。
3. **上线方式:不做空跑/影子模式,直接构建可以真正合并代码的版本。**
   用户明确选择跳过"先打印决策、不真的合并"的中间阶段。因此
   `claude-automerge.yml` 从第一次满足全部条件开始就会真的执行
   `gh pr merge --squash`,没有额外的 dry-run 开关。

## 合并门禁的九个条件(`claude-automerge.yml`,全部满足才合并)

1. 存在一条来自 `author_association == OWNER` 账号、格式匹配
   `## Verdict\nPASS` 的评论,且没有比它更晚的 `NEEDS_CHANGES` 评论。
2. 该 PASS 评论里引用的 head SHA,与 PR **当前**的 head SHA 完全一致。
3. 该 PR 上历史 `NEEDS_CHANGES` 轮数 ≤ 3(超过则停止并留言,不合并,
   对应 `CLAUDE.md` 的 3 轮自动返工上限)。
4. PR 不是 Draft。
5. PR 处于可合并状态(`mergeable == MERGEABLE`,没有冲突)。
6. PR 没有 `do-not-merge` / `blocked` / `security-review-required` 或
   同类阻止标签(大小写不敏感的子串匹配)。
7. PR 改动的文件里,不包含以下任何一类"自动化信任边界"路径(不论 Work
   给不给 PASS,这些永远只能人工合并):
   `.github/workflows/**`、`.github/actions/**`、`AGENTS.md`、
   `CLAUDE.md`、`.github/dependabot.yml`、`CODEOWNERS`
   (根目录或 `.github/`)、`infrastructure/alembic/versions/**`(数据库
   迁移)、`deploy/**`、`backend/app/core/secrets.py`、
   `backend/app/core/security.py`、`backend/app/dependencies.py`。
8. 没有未 resolve 的 review thread(GraphQL `reviewThreads.isResolved`
   全部为真)。
9. 该 PR head SHA 上的所有 check run 都已经 `completed` 且
   `conclusion` 是 `success`/`skipped`/`neutral`(没有 `failure` /
   `cancelled` / `timed_out` / 仍在进行中的)。

任何一条不满足,工作流直接退出、不合并。其中条件 3 和条件 7 会在**第一次**
判定为阻塞时留一条说明性 PR 评论(帮人知道该做什么);条件 4/5/6/8/9 不满足
时**不**每次都留言(避免在每次 `check_suite` 完成时刷屏——这些状态本身在
PR 页面上(草稿标记、冲突提示、CI 红叉、未 resolve 的会话)已经是可见的,
不需要额外重复一遍)。

## 已知限制 / 未解决的差距(如实记录,不假装已经完美)

- **PASS 校验没有身份加密验证**——见上面"用户已经做出的选型决定"第 1 条。
  这是用户知情选择的残留风险,不是本任务的疏漏。
- **`GITHUB_TOKEN` 方案下 CI 触发依赖 Claude 自己记得调用
  `gh workflow run`**——如果 Claude 某次会话漏了这一步,`claude-automerge.yml`
  的条件 9(所有 check run 必须 completed)会一直不满足,PR 就会卡住不合并
  (安全的失败方向:宁可不合并,不会因为 CI 没跑就误判为"没有失败所以算
  过"),但需要人工发现"CI 从没跑过"并手动跑一次
  `gh workflow run ci.yml --ref <branch>` 之类命令解围。
- **"没有已批准任务时停止,不得自行扩展需求"这条要求里"从任务队列领取
  下一项"没有实现**:这个仓库目前没有任何形式的任务队列(GitHub Project
  board、队列文件都没有)。发明一个任务队列机制是一个独立的、范围更大的
  功能,不属于"自动创建 PR + 自动合并"这个任务本身,超出"只改必要文件"的
  原则,故不在本任务里做。合并成功后 `claude-automerge.yml` 只留一条确认
  评论并结束,不会尝试拉起任何后续工作。
- **无法在本次会话里做真实的端到端验收**(见下面"验收标准"):
  `CLAUDE_CODE_OAUTH_TOKEN` 这个 Secret 到目前为止还没有被配置到仓库里
  (`docs/82-tasks/TASK-T17-*.md` 和 PR #43 描述都提到这是需要用户在
  GitHub 网页上手动做的一步)。没有这个 Secret,`@claude` 提及根本不会
  触发任何东西,所以"开一个真实 Issue、走完整闭环、验证真的自动合并"这
  一步在这次会话里**做不了**,不能报告成"已验证"。

## 约束

- 只在 `ci/claude-code-github-action` 分支 / PR #43 上继续,不新开配置 PR
  (用户明确要求)。
- `claude-automerge.yml` 不 checkout、不执行 PR 分支里的任何代码——只调用
  GitHub API/`gh` CLI——避免恶意 PR 分支反过来利用这个工作流的写权限。
- `claude-automerge.yml` 不引入任何第三方 Action(纯 `runs-on: ubuntu-latest`
  自带的 `bash`/`gh`/`jq`),因此不存在"锁定第三方 Action SHA"的问题——
  `.github/workflows/claude.yml` 已有的两个第三方 Action 继续保持锁定
  commit SHA 不变。
- `claude.yml` 的 `actions` 权限从 `read` 提升到 `write`,仅用于
  `gh workflow run`(触发 workflow_dispatch),不用于任何其他用途。
- 永不自动部署生产环境——`claude-automerge.yml` 和 `claude.yml` 都不涉及
  `deploy-*.yml`,且 `deploy/**` 本身就在合并门禁条件 7 的排除路径里。
- 不修改 `.github/workflows/ci.yml` / `security.yml` 的既有触发条件和
  job 逻辑,只新增 `workflow_dispatch` 入口;`risk-classify.yml` 额外加
  `pull_number` 输入以兼容非 PR-event 触发,job 逻辑不变。

## 允许修改的文件

- `.github/workflows/claude.yml`(已存在,扩展提示词 + 权限)
- `.github/workflows/claude-automerge.yml`(新建)
- `.github/workflows/ci.yml`(新增 `workflow_dispatch` 触发)
- `.github/workflows/security.yml`(新增 `workflow_dispatch` 触发)
- `.github/workflows/risk-classify.yml`(新增 `workflow_dispatch` 触发 +
  `pull_number` 输入)
- `docs/82-tasks/TASK-T18-conditional-auto-merge.md`(本文件)
- `docs/82-tasks/TASK-T17-claude-code-github-action.md`(补充说明自动创建
  PR 的能力已经上线,原先"默认不自动开 PR"的描述仅适用于 T17 范围)
- `AGENTS.md`(铁律第 8 条改成有条件例外;协作角色一节同步更新)
- `CLAUDE.md`(General/Safety 章节同步更新,新增自动合并门禁的说明)

## 验收标准

- `.github/workflows/claude-automerge.yml`、更新后的
  `ci.yml`/`security.yml`/`risk-classify.yml`/`claude.yml` 均通过
  `actionlint` 和 YAML 语法校验。
- `claude-automerge.yml` 里判定 Work 格式评论、提取 verdict/SHA、轮数
  计数、过期 PASS 判定的 `jq` 逻辑,用取自 PR #43 真实评论历史的样本数据
  做过独立验证(见 PR 描述里贴出的验证过程),不是只凭读代码判断"应该
  没问题"。
- 合并门禁的九个条件在 `claude-automerge.yml` 里逐条可对应到脚本里的
  具体检查,且每条都在 TASK 文档里写明。
- **真实端到端验收本次无法完成**,原因和依赖(`CLAUDE_CODE_OAUTH_TOKEN`
  未配置)已经写清楚,PR 描述和验收报告都不得声称"已验证 @claude 能自动
  创建 PR / 自动合并"这种在当前条件下不可能做到的结论。用户配置好 Secret
  并合并这个 PR 之后,可以用一个只改动无风险文件(例如新建一个测试用的
  fixture 文档)的真实 Issue 来验证完整闭环。
- 不自行合并本任务的 PR(`claude-automerge.yml` 的运作场景也不包括它自己
  这个改动本身——条件 7 明确把 `.github/workflows/**` 和
  `AGENTS.md`/`CLAUDE.md` 排除在自动合并范围外,PR #43 只能人工合并)。
