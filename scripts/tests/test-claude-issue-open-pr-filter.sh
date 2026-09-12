#!/usr/bin/env bash
# Regression test for the deterministic "same Issue must not get a second
# PR" preflight added to the `dedup` job's `check` step in
# .github/workflows/claude.yml (Issue #71 review round 2 -- an additional,
# earlier-and-cheaper layer on top of the already-verified ensure-pr-level
# branch-name check from review round 1, which
# scripts/tests/test-claude-ensure-pr-and-dispatch.sh scenario 5 covers).
#
# The preflight runs a GraphQL query against `closedByPullRequestsReferences`
# and pipes the response through a jq filter to find the first OPEN linked
# PR. Real GitHub GraphQL responses can't be exercised outside github.com,
# so this test instead proves the one fact the fix actually depends on: that
# jq filter, run against canned (but shaped-like-real) GraphQL response
# bodies, correctly extracts an open PR number when one exists and returns
# nothing when it doesn't.
#
# JQ_FILTER below is copied verbatim from claude.yml's `check` step -- keep
# the two in sync if that filter ever changes (same convention already used
# by scripts/validate-claude-workflow-concurrency.sh for the concurrency
# group-string expression).
set -euo pipefail

JQ_FILTER='[.data.repository.issue.closedByPullRequestsReferences.nodes[] | select(.state=="OPEN") | .number] | first // empty'

failures=0
assert_equal() {
  local description="$1" expected="$2" actual="$3"
  if [ "${expected}" = "${actual}" ]; then
    echo "PASS: ${description} (= '${expected}')"
  else
    echo "FAIL: ${description} (expected '${expected}', got '${actual}')"
    failures=$((failures + 1))
  fi
}

run_filter() {
  printf '%s' "$1" | jq -r "${JQ_FILTER}"
}

echo "### Case 1: Issue has one open linked PR -> returns its number"
result=$(run_filter '{"data":{"repository":{"issue":{"closedByPullRequestsReferences":{"nodes":[{"number":72,"state":"OPEN"}]}}}}}')
assert_equal "single open PR" "72" "${result}"

echo "### Case 2: Issue has no linked PRs at all -> empty"
result=$(run_filter '{"data":{"repository":{"issue":{"closedByPullRequestsReferences":{"nodes":[]}}}}}')
assert_equal "no linked PRs" "" "${result}"

echo "### Case 3: Issue's only linked PR was already merged -> empty (safe to open a new one)"
result=$(run_filter '{"data":{"repository":{"issue":{"closedByPullRequestsReferences":{"nodes":[{"number":50,"state":"MERGED"}]}}}}}')
assert_equal "merged-only PR does not block" "" "${result}"

echo "### Case 4: Issue's only linked PR was closed without merging -> empty (safe to open a new one)"
result=$(run_filter '{"data":{"repository":{"issue":{"closedByPullRequestsReferences":{"nodes":[{"number":50,"state":"CLOSED"}]}}}}}')
assert_equal "closed-only PR does not block" "" "${result}"

echo "### Case 5: Issue has a closed PR and a separate open PR -> returns the open one"
result=$(run_filter '{"data":{"repository":{"issue":{"closedByPullRequestsReferences":{"nodes":[{"number":50,"state":"CLOSED"},{"number":72,"state":"OPEN"}]}}}}}')
assert_equal "picks the open PR among mixed states" "72" "${result}"

echo
if [ "${failures}" -gt 0 ]; then
  echo "FAILED: ${failures} assertion(s) failed."
  exit 1
fi
echo "All assertions passed."
