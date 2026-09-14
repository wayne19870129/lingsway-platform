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
- No branch, no changes, or existing head PR: no creation. An existing
  same-Issue PR gets the new branch merged into its own-repo head (or a
  recorded pointer on fork/conflict) instead of a second PR.
- Serialize only PR creation per Issue; never cancel a running job.
- PAT is supplied only to trusted code on a separate runner.
- Normal verification tools are scoped; max turns is 30; no model change.
- No PR-merge/approval/deploy/delete/dispatch API or general Bash grant.
  ("Merge" here is `repos.merge` — combining two branches with a merge
  commit — never `pulls.merge`, which would merge a PR into `main`.)
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
the same Issue produces a *different* branch than the one already
backing an open PR. Since GitHub's PR API cannot repoint an existing
PR's head branch, and simply returning on "Issue already has a PR" would
silently drop that second branch's commits, the helper instead merges
the new branch into the existing PR's own-repository head branch via
`repos.merge` (`main...head` was already compared; this is a second,
fixed, non-interpolated API call with `base`/`head` taken only from
validated branch names). A successful merge updates the existing PR's
diff in place — no second PR, no lost work. If the existing PR's head is
on a fork (cross-repository, no push access) or the merge API reports a
conflict (`409`), the helper cannot safely consolidate the branches
automatically; it instead edits the existing PR's body (idempotently,
guarded by an HTML-comment marker per branch) to record the pending
branch name and state that a maintainer must merge or rebase it
manually. The branch and its commits are never deleted or lost either
way; the PR body change and warnings are the durable evidence a
maintainer relies on to complete a genuine conflict/fork case.

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
| Contents | Read and write | Compare main and the pushed branch; merge a later same-Issue branch into an existing open PR's own-repo head so follow-up work is not orphaned |
| Pull requests | Read and write | List existing PRs, create one, and update an existing PR's body when a follow-up branch cannot be auto-merged |

Contents write is scoped to this one repository and used only for the
single, fixed `repos.merge` call described below; no other write path
(arbitrary push, force-push, ref deletion) is exposed. No Issues,
Actions, Workflows, or Administration grant is needed. Add the token in
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
workflows remain unchanged; no dispatch or approval workaround is added.

## Tool permissions and limits

Preinstall Python 3.12 dev dependencies and frontend dependencies using
Node 22 from the trusted default-branch checkout. Allow pytest, ruff check,
mypy (direct and python/python3 -m forms), exact make test-unit/make lint,
and exact frontend npm lint/typecheck/build commands. No general python,
python -c, pip, npm, make, npx, gh, curl, ssh, or Bash grant. The current
model default is unchanged; max-turns 30 bounds the previous 84-turn run.
Actions read is granted both in workflow permissions and the Action's
additional_permissions input, as its official FAQ requires.

These are command permissions, not a sandbox for arbitrary modified test
code: pytest, make and npm execute repository code. Human-only merge and
no deployment remain repository policy; the PAT never reaches that code.
The PR helper has no `pulls.merge`/review/deploy/delete/dispatch call —
it never merges a pull request into `main` or approves one. Its Contents
permission is Read and write, scoped to this one repository, used only
for the single fixed `repos.merge` (branch-to-branch merge) call so a
later same-Issue implementation reaches an already-open PR instead of
being orphaned on an unreferenced branch; PR-write inherently permits
reviews, but the helper never calls that API either.
Do not claim command allowlisting alone makes malicious code harmless.

## Validation and remaining live acceptance

Local Node tests cover create/no-op/duplicate/error decisions, literal
Markdown handling, rerunning after a prior creation, merging a later
same-Issue branch into an existing own-repo PR, and the pending-branch
fallback note (fork head or merge conflict), including its idempotent
rerun. CI runs the same tests in its existing lint job. Syntax and PR CI
results are recorded in the PR description.

After configuring the secret and human merge, the owner should request a
small documentation change on a new Issue. Confirm one PR is created,
normal pull_request CI/Security/Risk runs automatically with no approval
button, and the requested test/lint commands execute without denials.
Repeat a question-only request (no PR), rerun the successful creation job
(no duplicate), and request another change on the same Issue — confirm
the second branch's commits land in the existing PR (merged, or a
pending-branch note if a real conflict occurs) rather than a second PR.
Check unauthorized comments skip and PR rework skips creation.
Do not probe dangerous operations against the live repository. Until
these tests run, do not report the end-to-end workflow as validated.

## Rollback

Revert this PR through a reviewed PR; remove CLAUDE_PR_TOKEN and revoke
the dedicated PAT. Claude returns to the PR #88 behavior (manual PR link).
No production/customer runtime or database changes are involved.
