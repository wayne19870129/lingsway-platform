#!/usr/bin/env bash
# Issue #71: deterministic PR-creation + CI/Security/Risk dispatch.
#
# This is intentionally a plain GitHub Actions shell step, never something
# Claude itself is asked to do. The pinned anthropics/claude-code-action's
# tag mode (the mode this repo's claude.yml runs, since it sets no `prompt`
# input) hardcodes a narrow tool allowlist for Claude -- Glob/Grep/LS/Read,
# a few read-only CI-status MCP tools, and `git add`/`git commit`/the
# action's own push wrapper/`git rm`. It does NOT include `gh` at all. The
# workflow's previous system prompt nonetheless told Claude to run
# `gh pr create` and `gh workflow run ci.yml/security.yml/risk-classify.yml`
# after pushing -- every one of those calls was denied by the SDK's
# permission gate (there is no interactive prompt to fall back to in this
# headless action, so a disallowed tool call is a hard denial, not a wait),
# and Claude kept retrying, burning turns until --max-turns was hit. A real
# run (#103, Issue #66) hit exactly this: 13 permission denials, 31 turns,
# failure -- despite having already made and pushed the one real commit the
# task needed.
#
# Moving PR creation and CI/Security/Risk dispatch into this deterministic
# script removes the mismatch instead of widening Claude's allowedTools to
# paper over it: these are fixed, mechanical actions with no judgment
# involved once a branch has been pushed, so they belong in a normal
# workflow step (which has full `gh` access via the job's own GITHUB_TOKEN)
# rather than in Claude's own reasoning loop.
#
# Used from two places in .github/workflows/claude.yml:
#   1. The `claude` job, immediately after the claude-code-action step, with
#      `if: always()` -- so this still runs even if that step reported
#      failure (e.g. hit --max-turns) as long as a branch with real commits
#      was actually pushed. This is what lets a run that already did the
#      real work finish (PR opened, checks dispatched) without needing any
#      retry at all, turn-mismatch failures included.
#   2. The `claude-recovery` job (workflow_dispatch, owner-only), so a
#      transient `gh`/network failure in (1) -- as opposed to a mismatch
#      failure, which this script's normal post-Claude invocation already
#      recovers from by itself -- can be retried by hand against an
#      already-pushed branch without re-running the full Claude model turn
#      loop a second time.
#
# Idempotent: if a PR already exists for BRANCH (Claude pushed directly onto
# an existing PR's branch for a PR-rework trigger, or a previous invocation
# of this same script already created one for an Issue-first trigger), that
# PR is reused rather than duplicated -- "one task, one PR" (CLAUDE.md) is
# preserved across retries and across the normal/recovery invocation paths.
#
# Independent review (PR #72, round 1) found two further correctness gaps,
# both fixed below:
#   - PRE_HEAD_SHA (optional): the PR-rework branch's own head SHA captured
#     BEFORE Claude ran (see claude.yml's `dedup` job). A PR-rework branch is
#     *always* ahead of base (that's what makes it an open PR), so the old
#     "commits ahead of base" check alone could not tell "Claude pushed a
#     real fix this run" apart from "Claude's step failed before pushing
#     anything, and this is just the PR's pre-existing history." If the
#     branch's current head still equals PRE_HEAD_SHA, this run added
#     nothing new, and the script exits without touching the PR or
#     dispatching checks -- and, critically, without setting `pr_number`, so
#     claude.yml's completion marker is correctly left unset for a genuine
#     retry, instead of a stale PR number being taken as proof this run
#     completed anything.
#   - ISSUE_NUMBER (optional, Issue-first triggers only): before creating a
#     brand-new PR, refuse if an open PR already exists for the same Issue
#     on a *different* branch. The pinned claude-code-action creates a new,
#     uniquely-timestamped branch on every Issue-first trigger and never
#     searches for or reuses an earlier one (see the TASK doc), so without
#     this guard a second, differently-worded `@claude` comment on the same
#     still-open Issue could open a second PR for what CLAUDE.md's "one
#     task, one PR" rule treats as the same task. Detected by the default
#     tag-mode branch naming template (`{{prefix}}issue-{{number}}-...` with
#     this workflow's unmodified default `branch_prefix: claude/`) --
#     documented assumption, not a hardcoded requirement of the Action.
set -euo pipefail

: "${REPO:?REPO is required (owner/repo)}"
: "${BRANCH:?BRANCH is required}"
: "${BASE_BRANCH:?BASE_BRANCH is required}"

if ! git ls-remote --exit-code origin "refs/heads/${BRANCH}" >/dev/null 2>&1; then
  echo "Branch '${BRANCH}' does not exist on origin -- nothing was pushed, nothing to do."
  exit 0
fi

git fetch origin "${BASE_BRANCH}" "${BRANCH}" --quiet

commit_count=$(git rev-list --count "origin/${BASE_BRANCH}..origin/${BRANCH}")
if [ "${commit_count}" -eq 0 ]; then
  echo "Branch '${BRANCH}' has no commits ahead of '${BASE_BRANCH}' -- nothing to do."
  exit 0
fi

current_head=$(git rev-parse "origin/${BRANCH}")
if [ -n "${PRE_HEAD_SHA:-}" ] && [ "${current_head}" = "${PRE_HEAD_SHA}" ]; then
  echo "Branch '${BRANCH}' head (${current_head}) is unchanged from before this run -- this run pushed nothing new, nothing to do."
  exit 0
fi

pr_number=$(gh pr list --repo "${REPO}" --head "${BRANCH}" --state all --json number --jq '.[0].number // empty')

if [ -z "${pr_number}" ]; then
  if [ -n "${ISSUE_NUMBER:-}" ]; then
    existing_for_issue=$(gh pr list --repo "${REPO}" --state open --json number,headRefName \
      | jq -r --arg prefix "claude/issue-${ISSUE_NUMBER}-" \
          '[.[] | select(.headRefName | startswith($prefix))] | (.[0].number // empty)')
    if [ -n "${existing_for_issue}" ]; then
      echo "Refusing to open a second PR for Issue #${ISSUE_NUMBER}: PR #${existing_for_issue} is already open for it on a different branch. Push follow-up work to PR #${existing_for_issue} instead (one task, one PR)." >&2
      exit 1
    fi
  fi
  echo "No existing PR for branch '${BRANCH}' -- creating one from its latest commit."
  # The latest commit's subject/body become the PR title/body: Claude is
  # instructed (see claude.yml's system prompt) to make its final commit
  # message the complete, structured PR description (Issue reference,
  # Summary, Acceptance criteria, Verification, Known gaps, and a `Closes
  # #N` line when the change fully resolves the triggering Issue).
  title=$(git log -1 --format=%s "origin/${BRANCH}")
  body=$(git log -1 --format=%b "origin/${BRANCH}")
  if [ -z "${body}" ]; then
    body="(no commit body was provided; see the commit history on this branch for details)"
  fi
  pr_create_err_file="$(mktemp)"
  # Capture gh's real exit status with `set +e` around the call: inside an
  # `if ! cmd; then ...` branch, `$?` reflects the negated `!` result (i.e.
  # 0 on failure), not cmd's own status, so that pattern cannot be used to
  # propagate the original non-zero exit code.
  set +e
  pr_url=$(gh pr create --repo "${REPO}" --base "${BASE_BRANCH}" --head "${BRANCH}" \
    --title "${title}" --body "${body}" 2>"${pr_create_err_file}")
  pr_create_status=$?
  set -e
  if [ "${pr_create_status}" -ne 0 ]; then
    cat "${pr_create_err_file}" >&2
    if grep -qF 'GitHub Actions is not permitted to create or approve pull requests' "${pr_create_err_file}"; then
      echo "HINT: this is the known GitHub repository-level restriction, not a script or token-scope bug. The repository owner must enable Settings -> Actions -> General -> Workflow permissions -> \"Allow GitHub Actions to create and approve pull requests\" before this deterministic step can create PRs. Widening this workflow's own 'permissions:' block does not fix it." >&2
    fi
    rm -f "${pr_create_err_file}"
    exit "${pr_create_status}"
  fi
  rm -f "${pr_create_err_file}"
  pr_number=$(printf '%s' "${pr_url}" | grep -oE '[0-9]+$')
  echo "Created PR #${pr_number} for branch '${BRANCH}': ${pr_url}"
else
  echo "Reusing existing PR #${pr_number} for branch '${BRANCH}' -- not creating a duplicate."
fi

echo "Dispatching ci.yml and security.yml against ref '${BRANCH}'..."
gh workflow run ci.yml --repo "${REPO}" --ref "${BRANCH}"
gh workflow run security.yml --repo "${REPO}" --ref "${BRANCH}"

if [ -n "${pr_number}" ]; then
  echo "Dispatching risk-classify.yml for PR #${pr_number}..."
  gh workflow run risk-classify.yml --repo "${REPO}" --ref "${BRANCH}" -f "pull_number=${pr_number}"
fi

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "pr_number=${pr_number}" >> "${GITHUB_OUTPUT}"
fi
