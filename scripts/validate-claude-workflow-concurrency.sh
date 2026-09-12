#!/usr/bin/env bash
# Issue #67: reproducible, non-GitHub validation of the concurrency `group:`
# expression in .github/workflows/claude.yml.
#
# GitHub Actions concurrency semantics cannot be exercised locally (there is
# no way to run the real Actions scheduler outside github.com), so this
# script instead validates the one fact the fix actually depends on: the
# *string value* the group expression produces for different actors on the
# same issue/PR. `cancel-in-progress: true` can only ever cancel another run
# whose group string is identical -- so if this script proves the group
# string differs for an owner run vs. a bot run vs. an unauthorized-user run
# (and stays identical across two different owner instructions on the same
# target), that is a complete, provable argument that a bot/unauthorized
# event can never cancel an authorized owner run, and that a genuinely newer
# owner instruction still supersedes the older one -- independent of any
# assumption about *when* GitHub evaluates concurrency relative to a job's
# `if:` condition.
#
# `group_for` below is a literal bash re-implementation of the workflow's
# `group: claude-${{ github.event.issue.number || github.event.pull_request.number }}-${{ github.actor }}`
# expression -- same two inputs (target number, actor), same string
# concatenation. Keep the two in sync if the expression in claude.yml ever
# changes.
set -euo pipefail

group_for() {
  local number="$1" actor="$2"
  printf 'claude-%s-%s' "$number" "$actor"
}

failures=0

assert_equal() {
  local description="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "PASS: $description (both = '$expected')"
  else
    echo "FAIL: $description ('$expected' != '$actual')"
    failures=$((failures + 1))
  fi
}

assert_not_equal() {
  local description="$1" left="$2" right="$3"
  if [ "$left" != "$right" ]; then
    echo "PASS: $description ('$left' != '$right')"
  else
    echo "FAIL: $description (both = '$left', expected different groups)"
    failures=$((failures + 1))
  fi
}

echo "== Scenario: issue_comment trigger on Issue #66 =="
owner_run_1=$(group_for 66 wayne19870129)
owner_run_2=$(group_for 66 wayne19870129) # a second, later, genuinely new owner instruction
bot_run=$(group_for 66 "claude[bot]")
other_bot_run=$(group_for 66 "github-actions[bot]")
unauthorized_run=$(group_for 66 some-other-user)

echo "  owner run 1:        $owner_run_1"
echo "  owner run 2 (newer): $owner_run_2"
echo "  bot status comment: $bot_run"
echo "  other bot identity:  $other_bot_run"
echo "  unauthorized user:  $unauthorized_run"
echo

assert_equal   "D: a newer owner instruction on the same Issue still supersedes the older owner run" \
  "$owner_run_1" "$owner_run_2"
assert_not_equal "B: claude[bot]'s status comment can never cancel the owner run" \
  "$owner_run_1" "$bot_run"
assert_not_equal "B: any other bot identity can never cancel the owner run either" \
  "$owner_run_1" "$other_bot_run"
assert_not_equal "C: an unauthorized human commenter can never cancel the owner run" \
  "$owner_run_1" "$unauthorized_run"
echo

echo "== Scenario: pull_request_review_comment trigger on PR #101 (same expression, different event) =="
pr_owner_run_1=$(group_for 101 wayne19870129)
pr_owner_run_2=$(group_for 101 wayne19870129)
pr_bot_run=$(group_for 101 "claude[bot]")
pr_unauthorized_run=$(group_for 101 some-other-user)

echo "  owner run 1:        $pr_owner_run_1"
echo "  owner run 2 (newer): $pr_owner_run_2"
echo "  bot status comment: $pr_bot_run"
echo "  unauthorized user:  $pr_unauthorized_run"
echo

assert_equal   "E/D: PR review-comment trigger -- newer owner instruction still supersedes the older owner run" \
  "$pr_owner_run_1" "$pr_owner_run_2"
assert_not_equal "E/B: PR review-comment trigger -- claude[bot]'s status comment can never cancel the owner run" \
  "$pr_owner_run_1" "$pr_bot_run"
assert_not_equal "E/C: PR review-comment trigger -- an unauthorized commenter can never cancel the owner run" \
  "$pr_owner_run_1" "$pr_unauthorized_run"
echo

echo "== Scenario: two different issues/PRs never share a group even for the same actor =="
assert_not_equal "different issue numbers for the same owner never collide" \
  "$owner_run_1" "$pr_owner_run_1"
echo

if [ "$failures" -eq 0 ]; then
  echo "All checks passed: 0 failures."
  exit 0
else
  echo "$failures check(s) FAILED."
  exit 1
fi
