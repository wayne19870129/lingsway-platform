# TASK-T17 — 官方 Claude Code GitHub Action(`@claude` 触发)

风险等级:medium(新增 CI 触发面 + 仓库写权限授予给 Action,但不涉及生产
凭据、不涉及部署、不涉及数据库)

## 目标

在 `wayne19870129/lingsway-platform` 里接入官方
`anthropics/claude-code-action@v1`,让**拥有仓库写权限**的用户在
GitHub Issue 评论或 PR 评论里 `@claude` 时,能触发一次 Claude Code 会话
去处理该请求(读代码、按需改代码、推送分支、开 PR)。

这是"交互模式"的官方 Action,和这个仓库里已经在跑的、由本会话通过
`subscribe_pr_activity` 驱动的人工 PR 陪跑流程是两回事——本任务只负责
把官方 Action 接进 CI,不改变、不依赖本会话现有的 PR 审查/返工流程。

## 约束

- 只监听 `issue_comment`(`created`)和 `pull_request_review_comment`
  (`created`)两种事件,不额外加 `pull_request_review`、`issues`
  (新建时的 body/title 触发)等官方支持但本次未要求的触发面。
- 用户是否有仓库写权限、是否是真人(非 bot)由 Action 自带的检查完成
  (`Who can trigger runs`),不额外实现一遍。
- 权限声明为完成任务所需的最小集合:`contents: write`、
  `pull-requests: write`、`issues: write`、`id-token: write`、
  `actions: read`。不加 `deployments`、`packages`、`administration` 等
  本任务用不到的权限。
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
- 提供一个可直接照做的、无风险的测试 Issue 文案(仅要求 Claude 创建一个
  不改业务逻辑的测试 PR),但由于需要 Secret 已配置、Action 已合并到
  `main` 才能真正触发,端到端验证必须由用户在这两步完成后自行确认,
  报告里不得声称"已验证 @claude 能创建 PR"这种在本任务 PR 阶段不可能
  完成的结论。
- 不自行合并本任务的 PR。
