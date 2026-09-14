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

The generated PR body carries a trustworthy full-vs-partial Issue-closure
signal without any new permission or trust-boundary crossing. The
create-pr job already fetches every commit message on the branch via the
Contents-read `compareCommitsWithBasehead` call (used for the Summary
section); `scripts/claude-create-pr.cjs` trusts only a literal,
line-anchored `Closes #<issue-number>` line for the exact triggering
Issue — never a substring inside prose — and otherwise states plainly
that the Issue stays open.

The Claude-side half of this signal is now wired into
`.github/workflows/claude.yml`'s existing `--append-system-prompt`.
Claude is instructed to place a line containing exactly
`Closes #<issue-number>` in one of its commit messages if, and only if,
its change fully resolves the triggering Issue. Partial work must not
write Closes/Fixes/Resolves followed by an Issue number in a commit
message or comment and must state the remaining work instead. This keeps
the full-vs-partial decision with the implementation agent while the
trusted PR helper accepts only the exact line-anchored marker.

`docs/83-project-continuity.md` records a real prior
incident (PR #77 / Issue #76) where GitHub's keyword parser closed an
Issue from a PR body containing the literal string "`Closes #76`" even
inside a sentence explicitly saying it was *not* included; the lesson
from that incident is "never write a negated/conditional closing-keyword
phrase," not "never write a real one when it's actually true." The
negative-case wording here never contains the words Closes/Fixes/Resolves
next to an issue number, so it cannot reproduce that failure mode.
A second, related risk this round's testing surfaced directly (not
previously covered): a commit *subject* quoted verbatim in the Summary
section could itself contain an incidental `Closes #N` for some other
Issue (e.g. a commit message that legitimately closes a different,
already-fixed issue). GitHub's keyword parser does not care that the text
is inside a quoted "commits in this run" bullet list. `claude-create-pr.cjs`
now defuses any such token in quoted commit-subject text with a
zero-width space before it reaches the PR body, so only the one, explicit,
line-anchored `closureNote` computed above can ever act as a real closing
reference.

**Open architectural conflict (not resolved by this change) —
same-Issue follow-up work does not reach the existing PR's head/CI.**
When a later same-Issue Claude run produces a different timestamped
branch while an open PR already exists, this helper records one durable
pending-branch pointer on that PR (see Acceptance) rather than creating a
second PR. That prevents silent loss, but it is not equivalent to the
later commits becoming part of the existing PR's reviewable head or
triggering its CI. GitHub's PR API has no way to repoint an existing PR's
head branch; the only way to land another branch's commits on that head
(`git merge`/`git push` equivalent) needs Contents:write to that specific
ref. A prior round of independent review flagged exactly that Contents:
write expansion as "broader than the intended small PR-creation tail" and
asked for it to be reverted, and a later round asked for the head/CI
reachability back while explicitly keeping Contents read-only. Those two
asks are mutually exclusive under GitHub's current REST surface — there
is no third mechanism. This is intentionally left as an open question for
a human decision (accept the pointer as final behavior and have
maintainers reply on the PR directly for follow-ups, vs. deliberately
approving a narrower reviewed Contents:write grant), not silently decided
either way in code.

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
Under the prior policy the automation actor was approval-gated.

The owner has now applied the documented repository setting:
**Settings > Actions > General > Approval for running fork pull request
workflows from contributors > Require approval for first-time
contributors who are new to GitHub**. Do not enable secrets or write
tokens for fork workflows. A fresh owner-authored SHA after this change
started normal PR checks without an approval gate; the actual
Claude-bot-triggered path remains part of the post-merge live acceptance.

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
