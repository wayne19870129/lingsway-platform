"""Truth table for the ADR-029 auto-merge gate.

ADR-029 "验证要求" and `docs/82-tasks/TASK-S08-automerge-pipeline.md` both
require a *reproducible* demonstration of each blocking condition rather than
"逻辑上应该成立". These tests are that demonstration: they exercise
`scripts/automerge_gate.py`, which is the same code
`.github/workflows/auto-merge.yml` runs, so a regression here fails CI before
it can merge anything.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml  # a declared runtime dependency: a missing import here is a failure

REPO_ROOT = Path(__file__).resolve().parents[3]
GATE_PATH = REPO_ROOT / "scripts" / "automerge_gate.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "auto-merge.yml"
SWITCH_PATH = REPO_ROOT / ".github" / "automerge-enabled"

_spec = importlib.util.spec_from_file_location("automerge_gate", GATE_PATH)
assert _spec is not None and _spec.loader is not None
gate = importlib.util.module_from_spec(_spec)
sys.modules["automerge_gate"] = gate
_spec.loader.exec_module(gate)

HEAD_SHA = "a" * 40
OTHER_SHA = "b" * 40

PASSING_REVIEW = (
    "Critical\n\nNone.\n\nMajor\n\nNone.\n\nMinor\n\nNone.\n\n"
    "Checks performed\n\n- read the full diff\n\n"
    f"Verdict: PASS\n\nReviewed at head SHA {HEAD_SHA}.\n"
)

GREEN_CHECKS = [
    {"name": "policy", "workflow": "CI", "status": "completed", "conclusion": "success"},
    {"name": "lint", "workflow": "CI", "status": "completed", "conclusion": "success"},
    {"name": "backend", "workflow": "CI", "status": "completed", "conclusion": "success"},
    {"name": "gitleaks", "workflow": "Security", "status": "completed", "conclusion": "success"},
    {
        "name": "classify",
        "workflow": "Risk classification",
        "status": "completed",
        "conclusion": "success",
    },
]


def mergeable_facts(**overrides: Any) -> dict[str, Any]:
    """A pull request that satisfies all eight conditions, before overrides."""
    facts: dict[str, Any] = {
        "pr_number": 140,
        "head_sha": HEAD_SHA,
        "head_ref": "task/s08-automerge-pipeline",
        "base_ref": "main",
        "state": "open",
        "draft": False,
        "merged": False,
        "mergeable": True,
        "labels": ["risk:low"],
        "changed_files": ["backend/app/services.py", "docs/83-project-continuity.md"],
        "automerge_switch": "true\n",
        "check_runs": list(GREEN_CHECKS),
        "reviews": [
            {
                "author": "wayne19870129",
                "author_association": "OWNER",
                "submitted_at": "2026-09-18T10:00:00Z",
                "body": PASSING_REVIEW,
            }
        ],
    }
    facts.update(overrides)
    return facts


# --- acceptance case 8: the only path that merges ---------------------------


def test_all_conditions_met_merges() -> None:
    result = gate.evaluate(mergeable_facts())
    assert result["decision"] == gate.MERGE, result
    assert result["blocks"] == []
    assert result["waits"] == []


# --- acceptance cases 1 and 2: the circuit breaker --------------------------


def test_missing_switch_file_blocks() -> None:
    result = gate.evaluate(mergeable_facts(automerge_switch=None))
    assert result["decision"] == gate.BLOCK
    assert any("absent" in reason for reason in result["blocks"])


@pytest.mark.parametrize("value", ["false", "FALSE", "", "true-ish", "0", "yes"])
def test_switch_not_exactly_true_blocks(value: str) -> None:
    result = gate.evaluate(mergeable_facts(automerge_switch=value))
    assert result["decision"] == gate.BLOCK
    assert any("circuit-breaker" in reason for reason in result["blocks"])


def test_switch_tolerates_trailing_newline() -> None:
    assert gate.evaluate(mergeable_facts(automerge_switch="true\n"))["decision"] == gate.MERGE


# --- acceptance case 3: stale review ----------------------------------------


def test_review_against_an_older_sha_does_not_count() -> None:
    stale = PASSING_REVIEW.replace(HEAD_SHA, OTHER_SHA)
    result = gate.evaluate(
        mergeable_facts(
            reviews=[
                {
                    "author": "wayne19870129",
                    "author_association": "OWNER",
                    "submitted_at": "2026-09-18T10:00:00Z",
                    "body": stale,
                }
            ]
        )
    )
    assert result["decision"] == gate.WAIT, result
    assert any("no verdict scoped to head SHA" in reason for reason in result["waits"])


def test_needs_changes_on_the_current_sha_blocks() -> None:
    rejection = PASSING_REVIEW.replace("Verdict: PASS", "Verdict: NEEDS_CHANGES")
    result = gate.evaluate(
        mergeable_facts(
            reviews=[
                {
                    "author": "wayne19870129",
                    "author_association": "OWNER",
                    "submitted_at": "2026-09-18T10:00:00Z",
                    "body": rejection,
                }
            ]
        )
    )
    assert result["decision"] == gate.BLOCK
    assert any("NEEDS_CHANGES" in reason for reason in result["blocks"])


def test_latest_verdict_wins_over_an_earlier_pass_on_the_same_sha() -> None:
    rejection = PASSING_REVIEW.replace("Verdict: PASS", "Verdict: NEEDS_CHANGES")
    result = gate.evaluate(
        mergeable_facts(
            reviews=[
                {
                    "author": "wayne19870129",
                    "author_association": "OWNER",
                    "submitted_at": "2026-09-18T10:00:00Z",
                    "body": PASSING_REVIEW,
                },
                {
                    "author": "wayne19870129",
                    "author_association": "OWNER",
                    "submitted_at": "2026-09-18T11:30:00Z",
                    "body": rejection,
                },
            ]
        )
    )
    assert result["decision"] == gate.BLOCK


def test_a_body_carrying_both_verdicts_is_treated_as_a_rejection() -> None:
    mixed = PASSING_REVIEW + "\nEarlier rounds were Verdict: NEEDS_CHANGES.\n"
    result = gate.evaluate(
        mergeable_facts(
            reviews=[
                {
                    "author": "wayne19870129",
                    "author_association": "OWNER",
                    "submitted_at": "2026-09-18T10:00:00Z",
                    "body": mixed,
                }
            ]
        )
    )
    assert result["decision"] == gate.BLOCK


def test_verdict_from_an_untrusted_author_does_not_count() -> None:
    result = gate.evaluate(
        mergeable_facts(
            reviews=[
                {
                    "author": "drive-by",
                    "author_association": "NONE",
                    "submitted_at": "2026-09-18T10:00:00Z",
                    "body": PASSING_REVIEW,
                }
            ]
        )
    )
    assert result["decision"] == gate.WAIT


def test_no_review_at_all_waits_rather_than_merging() -> None:
    assert gate.evaluate(mergeable_facts(reviews=[]))["decision"] == gate.WAIT


def test_markdown_emphasis_around_the_verdict_still_parses() -> None:
    emphasised = PASSING_REVIEW.replace("Verdict: PASS", "**Verdict:** `PASS`")
    result = gate.evaluate(
        mergeable_facts(
            reviews=[
                {
                    "author": "wayne19870129",
                    "author_association": "MEMBER",
                    "submitted_at": "2026-09-18T10:00:00Z",
                    "body": emphasised,
                }
            ]
        )
    )
    assert result["decision"] == gate.MERGE, result


def test_a_longer_sha_prefix_match_is_not_accepted() -> None:
    """`HEAD_SHA` must not match when it is a prefix of a different SHA."""
    misleading = PASSING_REVIEW.replace(HEAD_SHA, HEAD_SHA + "cc")
    result = gate.evaluate(
        mergeable_facts(
            reviews=[
                {
                    "author": "wayne19870129",
                    "author_association": "OWNER",
                    "submitted_at": "2026-09-18T10:00:00Z",
                    "body": misleading,
                }
            ]
        )
    )
    assert result["decision"] == gate.WAIT


# --- acceptance case 4: checks ----------------------------------------------


@pytest.mark.parametrize(
    "conclusion", ["failure", "cancelled", "timed_out", "action_required", "neutral", ""]
)
def test_any_non_passing_check_blocks(conclusion: str) -> None:
    runs = [dict(run) for run in GREEN_CHECKS]
    runs[2]["conclusion"] = conclusion
    result = gate.evaluate(mergeable_facts(check_runs=runs))
    assert result["decision"] == gate.BLOCK, result
    assert any("backend" in reason for reason in result["blocks"])


def test_a_still_running_check_waits_instead_of_merging() -> None:
    runs = [dict(run) for run in GREEN_CHECKS]
    runs[2].update({"status": "in_progress", "conclusion": ""})
    result = gate.evaluate(mergeable_facts(check_runs=runs))
    assert result["decision"] == gate.WAIT, result


@pytest.mark.parametrize("missing", ["CI", "Security", "Risk classification"])
def test_a_required_workflow_that_never_reported_waits(missing: str) -> None:
    runs = [dict(run) for run in GREEN_CHECKS if run["workflow"] != missing]
    result = gate.evaluate(mergeable_facts(check_runs=runs))
    assert result["decision"] == gate.WAIT, result
    assert any(missing in reason for reason in result["waits"])


def test_a_failing_check_outside_the_required_workflows_still_blocks() -> None:
    runs = [*GREEN_CHECKS, {
        "name": "some-app",
        "workflow": "Third party",
        "status": "completed",
        "conclusion": "failure",
    }]
    assert gate.evaluate(mergeable_facts(check_runs=runs))["decision"] == gate.BLOCK


def test_a_skipped_check_does_not_block() -> None:
    runs = [dict(run) for run in GREEN_CHECKS]
    runs[1]["conclusion"] = "skipped"
    assert gate.evaluate(mergeable_facts(check_runs=runs))["decision"] == gate.MERGE


# --- acceptance case 5: the opt-out label -----------------------------------


@pytest.mark.parametrize("label", ["no-automerge", "NO-AUTOMERGE"])
def test_no_automerge_label_blocks(label: str) -> None:
    result = gate.evaluate(mergeable_facts(labels=["risk:low", label]))
    assert result["decision"] == gate.BLOCK
    assert any("no-automerge" in reason for reason in result["blocks"])


# --- acceptance case 6: the audit must not approve itself -------------------


def test_audit_branch_blocks() -> None:
    result = gate.evaluate(mergeable_facts(head_ref="claude/audit-2026-09-18"))
    assert result["decision"] == gate.BLOCK
    assert any("audit" in reason for reason in result["blocks"])


def test_an_ordinary_claude_branch_is_not_treated_as_an_audit() -> None:
    assert gate.evaluate(mergeable_facts(head_ref="claude/fix-typo"))["decision"] == gate.MERGE


# --- acceptance case 7: production deployment keeps its manual gate ---------


def test_touching_deploy_blocks_even_when_everything_else_passes() -> None:
    result = gate.evaluate(
        mergeable_facts(changed_files=["docs/00-overview.md", "deploy/lib/70_verify.sh"])
    )
    assert result["decision"] == gate.BLOCK
    assert any("deploy/lib/70_verify.sh" in reason for reason in result["blocks"])


# --- additional fail-closed properties --------------------------------------


def test_merge_conflict_blocks() -> None:
    result = gate.evaluate(mergeable_facts(mergeable=False))
    assert result["decision"] == gate.BLOCK
    assert any("conflict" in reason for reason in result["blocks"])


def test_unknown_mergeability_waits_rather_than_merging() -> None:
    assert gate.evaluate(mergeable_facts(mergeable=None))["decision"] == gate.WAIT


@pytest.mark.parametrize(
    "state,draft,merged",
    [("closed", False, False), ("open", True, False), ("open", False, True)],
)
def test_closed_draft_or_already_merged_blocks(state: str, draft: bool, merged: bool) -> None:
    result = gate.evaluate(mergeable_facts(state=state, draft=draft, merged=merged))
    assert result["decision"] == gate.BLOCK


@pytest.mark.parametrize("sha", ["", "a" * 7, "z" * 40, None])
def test_a_head_sha_that_is_not_a_full_commit_id_blocks(sha: object) -> None:
    result = gate.evaluate(mergeable_facts(head_sha=sha))
    assert result["decision"] == gate.BLOCK


def test_empty_facts_block_rather_than_merge() -> None:
    assert gate.evaluate({})["decision"] == gate.BLOCK


def test_every_failure_is_reported_not_just_the_first() -> None:
    """The workflow log must show the whole truth table, not stop at one."""
    result = gate.evaluate(
        mergeable_facts(
            automerge_switch="false",
            labels=["no-automerge"],
            head_ref="claude/audit-x",
            changed_files=["deploy/run.sh"],
            mergeable=False,
        )
    )
    assert result["decision"] == gate.BLOCK
    assert len(result["blocks"]) >= 5, result["blocks"]


# --- the gate has no bypass -------------------------------------------------


def test_the_workflow_exposes_no_bypass_input() -> None:
    """ADR-029 §4: 不得提供任何绕过开关.

    The only manual entrypoint is `workflow_dispatch`, and it must expose
    nothing beyond the two simulation knobs -- no `force`, no `override`,
    no way to skip a condition.
    """
    parsed = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    triggers = parsed[True] if True in parsed else parsed["on"]
    inputs = set((triggers["workflow_dispatch"] or {}).get("inputs") or {})
    assert inputs == {"pr_number", "facts"}, inputs


def test_the_committed_switch_file_is_enabled() -> None:
    assert SWITCH_PATH.read_text(encoding="utf-8").strip() == "true"


def test_the_workflow_merges_only_on_a_merge_decision() -> None:
    """The merge job must be unreachable from the simulation entrypoint."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "needs.evaluate.outputs.decision == 'MERGE'" in workflow
    assert "github.event_name != 'workflow_dispatch'" in workflow


# --- the command-line entrypoint the workflow actually calls ----------------


def test_cli_reports_the_decision(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps(mergeable_facts()), encoding="utf-8")
    out_file = tmp_path / "decision.json"

    assert gate.main(["--facts", str(facts_file), "--output", str(out_file)]) == 0

    assert "decision: MERGE" in capsys.readouterr().out
    assert json.loads(out_file.read_text(encoding="utf-8"))["decision"] == "MERGE"


def test_cli_exits_zero_when_it_declines_to_merge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A BLOCK is a normal outcome; failing the job would just add red noise."""
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps(mergeable_facts(automerge_switch="false")), encoding="utf-8")

    assert gate.main(["--facts", str(facts_file)]) == 0
    assert "decision: BLOCK" in capsys.readouterr().out


def test_workflow_yaml_is_parseable() -> None:
    parsed = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert "evaluate" in parsed["jobs"]
    assert "merge" in parsed["jobs"]
