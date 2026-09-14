# TASK-T24 — Complete the minimal Claude development loop

## Goal and scope

User-requested follow-up to merged PR #88. Add a small PAT-backed PR
creation tail, verification tool permissions, Actions read access and a
30-turn limit. Create a separate PR; do not change PR #90, AGENTS.md,
business code, merge/deploy policy, or CI dispatch behavior.

## Evidence

Run 34792595873 reports 84 turns and 15 permission denials. Its public
sanitized log does not expose the 15 individual denied tool inputs.
Issue #89's Claude reply identifies pytest, ruff, mypy and python3 -c as
blocked. The logged allowlist contains file reading, comment/CI MCP
tools and the Action's git tools, but no test/lint commands.
The reply provides a Create PR link; PR #90 was created separately.

Official FAQ confirms interactive mode does not create PRs by default:
https://github.com/anthropics/claude-code-action/blob/main/docs/faq.md
The pinned Action accumulates user allowedTools with its tag-mode tools:
https://github.com/anthropics/claude-code-action/blob/0a8d3c9443bbff909ab973b6a17a340b913f229f/base-action/src/parse-sdk-options.ts

## Acceptance

- Owner/mention gate and OAuth/OIDC identity remain intact.
- Successful Issue runs with a pushed, nonempty diff create one PR to main.
- No branch, no changes, or existing head PR: no creation. If a different
  branch appears for an Issue that already has an open PR, add one durable,
  idempotent pointer to that branch on the existing PR; never create a second
  PR or write/merge branches automatically.
- Serialize only PR creation per Issue; never cancel a running job.
- PAT is supplied only to trusted code on a separate runner.
- Normal verification tools are scoped; max turns is 30; no model change.
- No branch-write/merge/approval/deploy/delete/dispatch API or general Bash grant.
- Mock API regression tests and normal PR CI pass; live acceptance awaits
  reviewed merge and maintainer configuration of CLAUDE_PR_TOKEN.

## Design and one-time maintainer setup

The create-pr job runs after a successful Claude job on Issue comments
only, on a separate GitHub-hosted runner. It checks out github.sha (the
Issue event's default-branch commit), never the Claude branch. The PAT
is supplied only to github-script, never checkout, Claude, dependency
installation, or tests. Only a branch name crosses jobs; Issue text is
passed as JSON to the API, never interpolated into a shell/script.

The helper lists all head PRs and all open PRs (paginated), checks the
Issue branch prefix or an explicit Issue reference in the body, then
compares main...head. It creates only when ahead_by > 0 and files differ.
The small creation job uses per-Issue concurrency with cancellation off;
this serializes automated check/create transactions. It cannot lock out
a human creating a different-head PR at precisely the same time, or
recognize an unrelated branch with no Issue reference at all. Existing
PR bodies should retain the Issue URL. GitHub concurrency may replace an
older pending job when more than two queue; it never cancels the running
creation job. API failures are surfaced, never treated as permission to
create. Rerun failed jobs after fixing configuration to retry safely.

Because the pinned Action names Issue branches with a timestamp
(`claude/issue-<n>-<timestamp>`), a second successful `@claude` run on
the same Issue can produce a *different* branch than the one already
backing an open PR. GitHub's PR API cannot repoint an existing PR's head
branch. The least-privilege helper therefore does not attempt to merge
or write either branch. It edits the existing PR body once, guarded by
an HTML-comment marker for that branch, and records the pending branch
plus a maintainer reconciliation instruction. This keeps one Issue / one
PR, prevents silent orphaning, and preserves Contents read-only access.

The generated PR body deliberately never contains a closing keyword
(`Closes`/`Fixes`/`Resolves #N`) and states the Issue is not
auto-closed. This is not an oversight: `docs/83-project-continuity.md`
records a real incident (PR #77 / Issue #76) where GitHub's keyword
parser closed an Issue from a PR body containing the literal string
"`Closes #76`" even inside a sentence explicitly saying it was *not*
included. Given the create-pr job has no reliable signal for whether an
Issue is fully or partially resolved (only a branch name crosses the
trust boundary from the Claude job), emitting a conditional closing
keyword would reproduce that exact failure mode. The Summary section
instead lists the actual commit subjects and changed file paths pulled
from the already-fetched `compare` response, so a reviewer gets a real,
data-derived "what changed" without rereading the whole diff, while the
Issue-closure decision stays a manual, reviewed step.

After review, the maintainer creates a fine-grained PAT under GitHub
Settings > Developer settings > Personal access tokens > Fine-grained
tokens > Generate new token. Choose resource owner wayne19870129,
Only select repositories: lingsway-platform, and a bounded expiration.
Repository permissions:

| Permission | Level | Actual use |
| --- | --- | --- |
| Metadata | Read (automatic) | Repository metadata |
| Contents | Read | Compare main and the pushed branch |
| Pull requests | Read and write | List existing PRs, create one, and append a pending-branch pointer to an existing PR |

No Contents-write, Issues, Actions, Workflows, or Administration grant
is needed. Add the token in
repository Settings > Secrets and variables
> Actions > New repository secret, named CLAUDE_PR_TOKEN. Do not paste
its value in chat, an Issue, or a PR. Leave CLAUDE_CODE_OAUTH_TOKEN and
the Claude App/OIDC setup unchanged. Missing/expired credentials cause
failure; there is no GITHUB_TOKEN fallback.

Permission minima are verified against GitHub's REST documentation for
[list/create PR](https://docs.github.com/en/rest/pulls/pulls) and
[compare commits](https://docs.github.com/en/rest/commits/commits#compare-two-commits).
They have not yet been exercised with the new PAT, which is not configured.
[GitHub's workflow-trigger rules](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)
explain why the built-in GITHUB_TOKEN must not create the PR. Normal PR
workflows remain unchanged; no dispatch workaround is added.

### One-time workflow approval setting

The rework commit on PR #91 reproduced `action_required` for CI,
Security, and Risk even though the jobs did not fail: the
`pull_request/synchronize` event actor was `github-actions[bot]`.
GitHub evaluates both the PR author and the actor that triggered the PR
event against the repository's public-fork contributor approval policy.
Under the current policy the automation actor is approval-gated.

There is no workflow-YAML change that can remove that repository policy
while also preserving the required standard Claude OAuth/OIDC/App
authentication path. After review, the owner must go to repository
**Settings > Actions > General > Approval for running fork pull request
workflows from contributors** and select **Require approval for first-time
contributors who are new to GitHub** (instead of all external
contributors). This leaves an approval boundary for newly created,
first-time external accounts while excluding GitHub's established
automation actor. Do not enable secrets or write tokens for fork
workflows. Then validate on a new Claude-pushed SHA. The setting cannot
be read or changed with the
current CLI token (GitHub returned 403), so this PR does not claim it is
already applied.

Reference: https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository

## Tool permissions and limits

Preinstall Python 3.12 dev dependencies and frontend dependencies using
Node 22 from the trusted default-branch checkout. Allow pytest, ruff check,
mypy (direct and python/python3 -m forms), exact make test-unit/make lint,
exact `node --test`, and exact frontend npm lint/typecheck/build commands. No general python,
python -c, pip, npm, make, npx, gh, curl, ssh, or Bash grant. The current
model default is unchanged; max-turns 30 bounds the previous 84-turn run.
Actions read is granted both in workflow permissions and the Action's
additional_permissions input, as its official FAQ requires.

These are command permissions, not a sandbox for arbitrary modified test
code: pytest, make and npm execute repository code. Human-only merge and
no deployment remain repository policy; the PAT never reaches that code.
The PR helper has no branch-write/merge/review/deploy/delete/dispatch
call. Its Contents permission is read-only. PR-write inherently permits
reviews, but the helper calls only list/create/update PR APIs.
Do not claim command allowlisting alone makes malicious code harmless.

## Validation and remaining live acceptance

Local Node tests cover create/no-op/duplicate/error decisions, literal
Markdown handling, rerunning after a prior creation, and the durable
pending-branch pointer, including its idempotent rerun. CI runs the same
tests in its existing lint job. Syntax and PR CI
results are recorded in the PR description.

After configuring the secret and human merge, the owner should request a
small documentation change on a new Issue. Confirm one PR is created,
normal pull_request CI/Security/Risk runs automatically with no approval
button, and the requested test/lint commands execute without denials.
Repeat a question-only request (no PR), rerun the successful creation job
(no duplicate), and request another change on the same Issue — confirm
the existing PR records the second branch once rather than creating a
second PR or writing either branch automatically.
Check unauthorized comments skip and PR rework skips creation.
Do not probe dangerous operations against the live repository. Until
these tests run, do not report the end-to-end workflow as validated.

## Rollback

Revert this PR through a reviewed PR; remove CLAUDE_PR_TOKEN and revoke
the dedicated PAT. Claude returns to the PR #88 behavior (manual PR link).
No production/customer runtime or database changes are involved.
