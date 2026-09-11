# TASK-T17 — 官方 Claude Code GitHub Action(`@claude` 触发)

风险等级:medium(新增 CI 触发面 + 仓库写权限授予给 Action,但不涉及生产
凭据、不涉及部署、不涉及数据库)

**范围更新(见 `TASK-T18-conditional-auto-merge.md`)**:下面"关于'开 PR'
这一步"这段描述的是**本任务(T17)范围内**、`@claude` 首次接入时的默认
行为。用户后续在同一个 PR(#43)里明确要求把自动创建 PR(而不是只给链接)
也做出来,这个能力已经在 T18 里实现——`claude.yml` 的提示词现在会让
Claude 在 Issue 场景下自己用 `gh pr create` 开出非 Draft PR。T17 这里
保留"官方 Action 默认不自动开 PR"这句事实描述本身没有错(那确实是
`anthropics/claude-code-action` 本身、不叠加任何自定义步骤时的行为),
但不要把它读成"这个仓库最终也不自动开 PR"——完整的当前行为、以及为什么
要做这个改动,以 T18 为准。

## 目标

在 `wayne19870129/lingsway-platform` 里接入官方
`anthropics/claude-code-action@v1`,让**拥有仓库写权限**的用户在
GitHub Issue 评论或 PR 评论里 `@claude` 时,能触发一次 Claude Code 会话
去处理该请求(读代码、按需改代码、推送分支)。

**关于"开 PR"这一步,需要如实说明官方 Action 的默认行为**(来自其官方
FAQ:"Claude doesn't create PRs by default. Instead, it pushes commits to
a branch and provides a link to a pre-filled PR submission page."):
在 Issue 场景下,Claude **不会自动创建 PR**,而是推送一个分支,并在评论里
给出一个预填好的"创建 PR"链接,由人点击才会真正生成 PR。这不是本任务
的缺陷,而是官方 Action 的默认设计(为了尊重分支保护规则、让 PR 创建这个
动作始终由人确认),和这个仓库"关键动作需要人工确认"的一贯风格是一致的,
不需要额外实现一个自动开 PR 的步骤去"补上"它。

如果触发源是 PR 上的评论(`pull_request_review_comment`,或者
`issue_comment` 但那个 issue 实际是个 PR),行为不同:Claude 已经在这个
PR 的分支上,应该直接推送修复到**当前 PR 的分支**,而不是新建分支/新建
PR——这是 `CLAUDE.md`"一个任务一个 PR"规则的直接要求,提示词必须按
事件类型区分这两种场景,不能用同一段无条件文字覆盖两种完全不同的期望
行为。

这是"交互模式"的官方 Action,和这个仓库里已经在跑的、由本会话通过
`subscribe_pr_activity` 驱动的人工 PR 陪跑流程是两回事——本任务只负责
把官方 Action 接进 CI,不改变、不依赖本会话现有的 PR 审查/返工流程。

**为什么 `claude.yml` 需要 `actions: write` 并在推送后手动调用
`gh workflow run`**:这个 job 用的是内置 `GITHUB_TOKEN`,而 GitHub 有个
反递归规则——用 `GITHUB_TOKEN` 创建的 PR 或推送的 commit,不会触发
`pull_request`/`push` 类型的事件。`ci.yml`/`security.yml`/
`risk-classify.yml` 只监听这些事件,所以如果什么都不做,Claude 自己开的
PR、自己推的返工 commit 完全不会跑 CI。缓解方式是给这三个工作流各加一个
`workflow_dispatch` 触发入口(`workflow_dispatch`/`repository_dispatch`
明确被排除在这条反递归限制之外),让 `claude.yml` 的提示词在每次 push 后
自己调用 `gh workflow run ci.yml --ref <branch>` 之类命令手动触发——这是
当前实现方式,依赖 Claude 每次记得执行这一步,不如原生事件触发可靠,但是
在不引入独立 GitHub App/PAT 的前提下能做到的最简方案。

**"自动返工"目前是什么、不是什么**:`claude.yml` 的触发条件是
`contains(github.event.comment.body, '@claude')`。ChatGPT Work 的审查
评论本身不包含这个字符串,所以 Work 给出 `NEEDS_CHANGES` **不会**自动
唤醒任何工作流去修复——这仍然需要有人(或者未来某个专门做"评论内容中继"
的工作流)在该评论下手动 `@claude` 一下才会触发返工。目前存在的"自动"
指的是:一旦 `@claude` 被提及,Claude 会自动完成"读取上下文 → 修复 →
推送 → 重新触发 CI"这一段,而不是"从 Work 给出结论到 Claude 开始修复"
这一段全自动。不要把这个仍然存在的人工触发步骤,读成一个完整的
审查→修复闭环。

## 约束

- 只监听 `issue_comment`(`created`)和 `pull_request_review_comment`
  (`created`)两种事件,不额外加 `pull_request_review`、`issues`
  (新建时的 body/title 触发)等官方支持但本次未要求的触发面。
- 用户是否有仓库写权限、是否是真人(非 bot)由 Action 自带的检查完成
  (`Who can trigger runs`),不额外实现一遍。
- 权限声明为完成任务所需的最小集合:`contents: write`、
  `pull-requests: write`、`issues: write`、`id-token: write`、
  `actions: read`。不加 `deployments`、`packages`、`administration` 等
  本任务用不到的权限。其中 `id-token: write` 的准确理由是官方文档写明的
  "required for the Claude Code GitHub Action's default GitHub App
  authentication"——是 Action 默认走 GitHub App 身份认证这条路径本身
  需要 OIDC token,不是 `CLAUDE_CODE_OAUTH_TOKEN` 这个订阅 token 本身
  需要 OIDC。
- **第三方 Action 必须锁定到具体 commit SHA,不用可变的版本 tag**
  (`actions/checkout@v6`、`anthropics/claude-code-action@v1` 这类 tag
  可以被上游重新指向,而这两个 Action 都会拿到仓库写权限,后者还能读到
  `CLAUDE_CODE_OAUTH_TOKEN`)。用 `owner/repo@<40位 commit sha> # vX.Y`
  的写法,注释里保留版本号方便人读;后续升级走人工审查过的 PR(可以用
  Dependabot 的 `github-actions` 生态自动开升级 PR,但升级本身仍要过
  一次审查,不能自动合并)。
- 必须有并发控制(同一个 Issue/PR 上新的 `@claude` 事件应取消或排队,
  不允许同一 Issue/PR 上多个 Action run 并行改同一个分支)、合理的
  `timeout-minutes`、以及通过 `--max-turns` 限制单次会话的最大轮数。
- **不得通过这条 Action 自动合并 PR、关闭他人的 PR、或触发生产部署**。
  这条约束的技术边界必须在任务书和验收里说清楚,不能只写提示词就假装
  已经硬性拦住:
  - `pull-requests: write` 这个 GitHub 粗粒度权限本身**技术上确实
    包含**合并 PR 的能力,GitHub 不提供"允许评论/开 PR 但不允许合并"
    这种更细的权限档位。真正的硬性拦截只能来自分支保护规则(要求人工
    批准才能合并),而这个仓库目前(`AGENTS.md` 铁律第 8 条)明确写着
    机械分支保护尚未启用。
  - 因此本任务只能做到:①提示词层面明确指示 Claude 不得 merge/close/
    部署;②仓库现有的 `deploy-*.yml` 全部是 `workflow_dispatch` 手动
    触发或 dry-run,这条新 Action 的权限里不含 `actions: write`,
    无法自己触发部署 workflow。这两条组合起来是当前能做到的全部,
    不构成机械意义上的"不可能",验收时必须如实说明。
- 认证优先用 `CLAUDE_CODE_OAUTH_TOKEN`(订阅模式);只有在用户明确要求
  或订阅方式不可用时才退回 `ANTHROPIC_API_KEY`。不管哪种,**都不能要求
  用户把 token/key 粘贴进聊天、Issue、PR 或仓库文件**——只能告诉用户
  在 GitHub 仓库 Settings 的哪个页面、用什么名字、怎么在本地生成后
  直接粘进 GitHub 的 Secret 输入框。
- Claude GitHub App(`https://github.com/apps/claude`)是否已安装在这个
  仓库,Claude Code 这边没有 API 能查到,必须让用户自己去
  `Settings → GitHub Apps`(或 Organization 的 GitHub Apps 设置)确认,
  没装的话引导安装,不能假设已装。
- 本任务只新增 `.github/workflows/claude.yml` 和这份 TASK 文档,不改动
  任何业务代码或既有 workflow。

## 允许修改的文件

- `.github/workflows/claude.yml`(新建)
- `docs/82-tasks/TASK-T17-claude-code-github-action.md`(本文件)
- `.github/dependabot.yml`(新建,仅用于 `github-actions` 生态的锁定
  SHA 升级提醒;不新增其他生态的自动升级)

## 验收标准

- `.github/workflows/claude.yml` 通过 `actionlint`/YAML 校验(语法正确、
  触发条件和权限字段拼写正确)。
- 权限声明只包含约束里列出的五项,不多不少。
- 工作流包含 `concurrency` 分组、`timeout-minutes`、`claude_args` 里的
  `--max-turns` 上限。
- 使用 `secrets.CLAUDE_CODE_OAUTH_TOKEN`,PR 描述里给出了如果要退回
  `ANTHROPIC_API_KEY` 该怎么改一行配置。
- PR 描述/验收报告里,给用户的指令包含:精确的 Secret 添加页面路径、
  Secret 名称、如何在本地生成 token(命令)、以及"不要把值发给我"的
  明确提醒。
- 提供一个可直接照做的、无风险的测试 Issue 文案,准确描述预期行为是
  "Claude 推送一个分支 + 给出创建 PR 的链接",不得写成"会自动出现一个
  PR"。由于需要 Secret 已配置、Action 已合并到 `main` 才能真正触发,
  端到端验证必须由用户在这两步完成后自行确认,报告里不得声称"已验证
  @claude 能工作"这种在本任务 PR 阶段不可能完成的结论。
- `--append-system-prompt` 必须按事件类型区分:PR 场景下的指令是"推到
  当前 PR 分支、不要新开 PR";Issue 场景下的指令是"新建分支,让 Action
  默认流程给出创建 PR 的链接,不要试图绕过去强行调用 API 创建 PR"。
- `actions/checkout` 与 `anthropics/claude-code-action` 都锁定到具体
  commit SHA,注释保留对应版本号。
- 不自行合并本任务的 PR。
