# TASK-T24 — Remove legacy `arrickcherney-ops` identity from active workflow (audit)

## 目标

Issue #92: the repository owner (`wayne19870129`) has decided the legacy
`arrickcherney-ops` GitHub account/identity is no longer needed for
ongoing development on `wayne19870129/lingsway-platform`. Independently
audit the current working tree for any *active-workflow* dependency on
or reference to that identity (workflows, CI/config, docs, helper
scripts, `CODEOWNERS`, PR/issue templates, Dependabot config), remove
anything obsolete found, and record the result so a future session does
not need to re-derive it from scratch.

## 约束

- Do not rewrite PR #91's authorship or any other Git history merely to
  erase the old username — PR #91 is historical/in-flight work and stays
  as-is (`CLAUDE.md` "External AI Review" / general history-preservation
  rule; this Issue's own instructions repeat it explicitly).
- Do not modify `AGENTS.md` (requires separate explicit human approval
  per `AGENTS.md`'s own 模块写权限 table and this Issue's instructions).
- Do not modify anything under `.github/workflows/` — out of scope for
  this audit (nothing there referenced the legacy identity — see
  Findings) and outside this session's GitHub App write permissions
  regardless.
- Do not weaken external/fork contributor approval protections while
  doing this cleanup.
- Do not perform any account-, repository-, or collaborator-level
  action (no deletions, no settings changes) — those are GitHub
  Settings-level actions for the human owner, not something expressible
  as a code change, and destructive/account operations are outside an
  agent's authority regardless (`AGENTS.md` 禁止自主执行).
- No merge, no deploy, no closing Issue #92 before human merge.

## 允许修改的文件

- `docs/82-tasks/TASK-T24-remove-legacy-identity-audit.md` (this file)
- `docs/83-project-continuity.md`

No other file needed a change — see Findings below.

## 方法

Case-insensitive full-tree search for `arrickcherney` (and the fuller
`arrickcherney-ops`) across every tracked file, plus a manual read of
the files most likely to carry an identity/approval dependency:
`.github/workflows/*.yml`, `.github/CODEOWNERS`,
`.github/pull_request_template.md`, `.github/dependabot.yml`,
`.github/ISSUE_TEMPLATE/*`, `AGENTS.md`, `CLAUDE.md`,
`docs/83-project-continuity.md`, `docs/10-deploy-new-server.md` (the
"GitHub settings that require manual configuration" section), and the
current `docs/82-tasks/TASK-T2*` series covering the Claude automation
workflow's identity/token design.

## Findings

- **Zero matches.** A case-insensitive search for `arrickcherney` across
  the entire working tree returns no results. No workflow YAML, script,
  doc, or config file in this repository references that account.
- `.github/workflows/claude.yml`'s job-level gate already reads
  `github.event.comment.user.login == 'wayne19870129'` — the only
  account that can trigger the Issue-first Claude automation is the
  canonical owner account, not the legacy one.
- `.github/CODEOWNERS` already lists only `@wayne19870129` for every
  path.
- `docs/10-deploy-new-server.md`'s "Protect `main`" instructions already
  target `wayne19870129/lingsway-platform` by name.
- `docs/83-project-continuity.md` (the living handoff index) already
  describes the collaboration model exclusively in terms of `User`
  (`wayne19870129`), `Claude Code`, and `ChatGPT Work` — no legacy
  identity is named anywhere in it.
- `TASK-T22-claude-automation-app-token-identity.md` (a superseded
  proposal for a custom GitHub App token) and `TASK-T23-simplify-claude-
  workflow.md` (the current design) were both read in full for identity
  references; neither names `arrickcherney-ops`. The workflow's only
  Claude-side credential is `CLAUDE_CODE_OAUTH_TOKEN`, scoped to
  `wayne19870129`'s own Claude Code Action installation.
- The only observed remaining link to the legacy identity is **PR #91
  itself**, which GitHub recorded as authored by `arrickcherney-ops` at
  creation time (historical fact, not reproducible from any file in this
  repo) and whose `pull_request`-triggered check runs were held behind
  GitHub's maintainer-approval gate. That gate is a repository/Actions
  **setting** (see `docs/83-project-continuity.md` §5's "failure class
  B" discussion of `action_required` runs and the "Protect main" ruleset
  in `docs/10-deploy-new-server.md`), not a value stored in any
  version-controlled file — it cannot be changed by this PR, and this
  session has no `gh`/GitHub API access to inspect or change repository
  collaborator/Actions-approval settings even if it were otherwise in
  scope. Whether `arrickcherney-ops` still holds collaborator access,
  and whether that access should be revoked, is therefore a GitHub
  Settings action for `wayne19870129` to take directly — not a finding
  this audit can resolve by editing code.

## 验收标准

- `git grep -i arrickcherney` (or equivalent) against the working tree
  returns no matches outside this TASK file's own description of the
  audit.
- `docs/83-project-continuity.md` records this audit's outcome so a
  future session doesn't need to repeat it.
- No `.github/workflows/**`, `AGENTS.md`, or PR #91 change is included.
- This PR's body does not use a GitHub closing keyword for Issue #92,
  because the remaining collaborator/Actions-approval-setting question
  identified above is outside this repository's version-controlled
  files and therefore outside what a code PR can close out — see the
  PR description for the explicit statement of what remains.
