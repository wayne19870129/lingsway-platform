"""Acceptance table for the ADR-030 §3 pipeline health digest.

TASK-S10 requires each acceptance case to be demonstrated reproducibly rather
than argued. These run `scripts/pipeline_health.py`, the same module the
workflow calls, so a regression in an alert fails CI instead of quietly
producing an all-clear digest -- which is this file's whole point: a missing
alert reads exactly like "nothing is wrong".
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPO_ROOT / "scripts" / "pipeline_health.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "pipeline-health.yml"
DIGEST_PATH = REPO_ROOT / "docs" / "87-pipeline-health.json"

_spec = importlib.util.spec_from_file_location("pipeline_health", MODULE_PATH)
assert _spec is not None and _spec.loader is not None
health = importlib.util.module_from_spec(_spec)
sys.modules["pipeline_health"] = health
_spec.loader.exec_module(health)

NOW = "2026-09-19T12:00:00+00:00"
HEAD_SHA = "c" * 40
OTHER_SHA = "d" * 40

PASS_BODY = f"Critical\nNone.\n\nVerdict: PASS\n\nReviewed at {HEAD_SHA}.\n"
REJECT_BODY = PASS_BODY.replace("Verdict: PASS", "Verdict: NEEDS_CHANGES")


def pr(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "number": 140,
        "head_sha": HEAD_SHA,
        "created_at": "2026-09-19T09:00:00+00:00",
        "last_commit_at": "2026-09-19T11:00:00+00:00",
        "ci": "success",
        "review_rounds": 1,
        "labels": ["risk:low"],
        "changed_files": ["docs/00-overview.md"],
        "mergeable": True,
        "reviews": [
            {
                "author": "wayne19870129",
                "author_association": "OWNER",
                "submitted_at": "2026-09-19T11:30:00+00:00",
                "body": PASS_BODY,
            }
        ],
    }
    base.update(overrides)
    return base


def facts(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "generated_at": NOW,
        "main": {"sha": "e" * 40, "ci_conclusion": "success", "commits_since_last_digest": 2},
        "breaker": "true\n",
        "merged_without_user_since_breaker_on": 1,
        "prs": [pr()],
    }
    base.update(overrides)
    return base


# --- case 6: the quiet path, which is the one that saves tokens -------------


def test_healthy_pipeline_produces_no_alerts() -> None:
    digest = health.build_digest(facts())
    assert digest["alerts"] == [], digest


def test_digest_shape_is_fixed() -> None:
    digest = health.build_digest(facts())
    assert set(digest) == {
        "generated_at",
        "main",
        "automerge",
        "open_prs",
        "alerts",
        "judgement_only_signals",
    }


# --- case 1: main CI red ----------------------------------------------------


def test_red_main_alerts() -> None:
    digest = health.build_digest(facts(main={"sha": "e" * 40, "ci_conclusion": "failure"}))
    assert health.ALERT_MAIN_CI_RED in digest["alerts"]


@pytest.mark.parametrize("conclusion", ["success", "pending"])
def test_main_not_red_does_not_alert(conclusion: str) -> None:
    digest = health.build_digest(facts(main={"sha": "e" * 40, "ci_conclusion": conclusion}))
    assert health.ALERT_MAIN_CI_RED not in digest["alerts"]


# --- case 2: the circuit breaker --------------------------------------------


@pytest.mark.parametrize("breaker", ["false", None, "", "yes"])
def test_breaker_not_true_alerts(breaker: str | None) -> None:
    digest = health.build_digest(facts(breaker=breaker))
    assert health.ALERT_BREAKER_OFF in digest["alerts"]


def test_absent_breaker_is_reported_as_absent_not_as_false() -> None:
    assert health.build_digest(facts(breaker=None))["automerge"]["breaker"] == "absent"


def test_breaker_true_with_trailing_newline_does_not_alert() -> None:
    assert health.ALERT_BREAKER_OFF not in health.build_digest(facts())["alerts"]


# --- case 3: a stuck pull request -------------------------------------------


def test_thirteen_idle_hours_with_unresolved_findings_alerts() -> None:
    stuck = pr(
        last_commit_at="2026-09-18T23:00:00+00:00",
        reviews=[
            {
                "author": "wayne19870129",
                "author_association": "OWNER",
                "submitted_at": "2026-09-18T23:30:00+00:00",
                "body": REJECT_BODY,
            }
        ],
    )
    assert health.ALERT_PR_STUCK in health.build_digest(facts(prs=[stuck]))["alerts"]


def test_eleven_idle_hours_does_not_alert() -> None:
    nearly = pr(
        last_commit_at="2026-09-19T01:00:00+00:00",
        reviews=[
            {
                "author": "wayne19870129",
                "author_association": "OWNER",
                "submitted_at": "2026-09-19T01:30:00+00:00",
                "body": REJECT_BODY,
            }
        ],
    )
    assert health.ALERT_PR_STUCK not in health.build_digest(facts(prs=[nearly]))["alerts"]


def test_idle_but_passing_is_not_stuck() -> None:
    """A PASSing pull request waiting on a human merge is not stuck."""
    waiting = pr(last_commit_at="2026-09-18T20:00:00+00:00")
    assert health.ALERT_PR_STUCK not in health.build_digest(facts(prs=[waiting]))["alerts"]


# --- case 4: the rework cap --------------------------------------------------


def test_three_rounds_alerts() -> None:
    """ADR-031 §6 tightened the cap from 5 to 3."""
    digest = health.build_digest(facts(prs=[pr(review_rounds=3)]))
    assert health.ALERT_ROUNDS_AT_CAP in digest["alerts"]


def test_two_rounds_does_not_alert() -> None:
    digest = health.build_digest(facts(prs=[pr(review_rounds=2)]))
    assert health.ALERT_ROUNDS_AT_CAP not in digest["alerts"]



# --- case 5: unattended auto-merge streak ------------------------------------


def test_three_consecutive_automerges_alerts() -> None:
    digest = health.build_digest(facts(merged_without_user_since_breaker_on=3))
    assert health.ALERT_THREE_AUTOMERGES in digest["alerts"]


def test_two_consecutive_automerges_does_not_alert() -> None:
    digest = health.build_digest(facts(merged_without_user_since_breaker_on=2))
    assert health.ALERT_THREE_AUTOMERGES not in digest["alerts"]


# --- case 7: no commit when only generated_at moved --------------------------


def test_only_the_timestamp_changing_is_not_a_change() -> None:
    first = health.build_digest(facts())
    second = health.build_digest(facts(generated_at="2026-09-19T14:00:00+00:00"))
    assert first != second
    assert health.has_changed(first, second) is False


def test_a_real_change_is_detected() -> None:
    first = health.build_digest(facts())
    second = health.build_digest(facts(breaker="false"))
    assert health.has_changed(first, second) is True


def test_no_previous_digest_counts_as_changed() -> None:
    assert health.has_changed(None, health.build_digest(facts())) is True


# --- case 8: the digest and the merge gate must agree on verdicts ------------


def test_verdict_matches_the_merge_gate_on_the_same_input() -> None:
    """One rule, one implementation. Two would drift silently."""
    # The same module object pipeline_health imported -- loading a second copy
    # would compare two implementations, which is the opposite of the point.
    gate = sys.modules["automerge_gate"]

    reviews = [
        {
            "author": "wayne19870129",
            "author_association": "OWNER",
            "submitted_at": "2026-09-19T11:30:00+00:00",
            "body": PASS_BODY,
        }
    ]
    assert health.latest_verdict_on_head(reviews, HEAD_SHA) == "PASS"

    gate_result = gate.evaluate(
        {
            "head_sha": HEAD_SHA,
            "head_ref": "task/x",
            "state": "open",
            "draft": False,
            "merged": False,
            "mergeable": True,
            "labels": [],
            "changed_files": ["docs/00-overview.md"],
            "automerge_switch": "true",
            "check_runs": [
                {"name": w, "workflow": w, "status": "completed", "conclusion": "success"}
                for w in ("CI", "Security", "Risk classification")
            ],
            "reviews": reviews,
        }
    )
    assert gate_result["decision"] == gate.MERGE
    assert any("review (PASS" in entry for entry in gate_result["passed"])


@pytest.mark.parametrize(
    "body,expected",
    [
        (PASS_BODY, "PASS"),
        (REJECT_BODY, "NEEDS_CHANGES"),
        (PASS_BODY.replace(HEAD_SHA, OTHER_SHA), "none"),
        ("no verdict here", "none"),
    ],
)
def test_verdict_cases(body: str, expected: str) -> None:
    reviews = [
        {
            "author": "wayne19870129",
            "author_association": "OWNER",
            "submitted_at": "2026-09-19T11:30:00+00:00",
            "body": body,
        }
    ]
    assert health.latest_verdict_on_head(reviews, HEAD_SHA) == expected


def test_untrusted_author_verdict_is_ignored() -> None:
    reviews = [
        {
            "author": "drive-by",
            "author_association": "NONE",
            "submitted_at": "2026-09-19T11:30:00+00:00",
            "body": PASS_BODY,
        }
    ]
    assert health.latest_verdict_on_head(reviews, HEAD_SHA) == "none"


# --- fields the supervisor relies on ----------------------------------------


def test_high_risk_paths_are_flagged() -> None:
    risky = pr(changed_files=["docs/x.md", "backend/app/domain/provisioning.py"])
    assert health.build_digest(facts(prs=[risky]))["open_prs"][0]["touches_high_risk_paths"]


def test_ordinary_paths_are_not_flagged() -> None:
    assert not health.build_digest(facts())["open_prs"][0]["touches_high_risk_paths"]


def test_undetectable_signals_are_named_rather_than_omitted() -> None:
    """A gap that is not named reads as 'all clear'. That must not happen."""
    digest = health.build_digest(facts())
    assert "SAME_FINDING_FAILED_TWICE" in digest["judgement_only_signals"]
    assert set(digest["judgement_only_signals"]).isdisjoint(digest["alerts"])


def test_empty_facts_do_not_crash() -> None:
    digest = health.build_digest({})
    assert digest["alerts"] == [health.ALERT_BREAKER_OFF]


def test_unparseable_timestamps_do_not_crash_or_alert() -> None:
    digest = health.build_digest(facts(prs=[pr(last_commit_at="not-a-date")]))
    assert digest["open_prs"][0]["hours_since_last_commit"] is None
    assert health.ALERT_PR_STUCK not in digest["alerts"]


# --- the workflow the digest is consumed by ---------------------------------


def test_workflow_calls_no_llm_and_no_claude_secret() -> None:
    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    for forbidden in ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_PR_TOKEN", "claude-code-action"):
        assert forbidden not in source, f"{forbidden} must not appear: TASK-S10 forbids it"


def test_workflow_cannot_touch_pull_requests() -> None:
    """TASK-S10: it observes and records. It must not be able to act on a PR."""
    parsed = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    for job in parsed["jobs"].values():
        assert set(job.get("permissions") or {}) <= {"contents"}, job.get("permissions")


def test_workflow_is_scheduled_and_dispatchable() -> None:
    parsed = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    triggers = parsed[True] if True in parsed else parsed["on"]
    assert "schedule" in triggers
    assert "workflow_dispatch" in triggers


def test_committed_digest_is_valid_and_has_the_expected_shape() -> None:
    stored = json.loads(DIGEST_PATH.read_text(encoding="utf-8"))
    assert set(stored) == set(health.build_digest(facts()))


# --- the command-line entrypoint the workflow actually calls ----------------


def test_cli_writes_digest_and_change_flag(tmp_path: Path) -> None:
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps(facts()), encoding="utf-8")
    out = tmp_path / "digest.json"
    changed = tmp_path / "changed.txt"

    argv = ["--facts", str(facts_file), "--output", str(out), "--changed-output", str(changed)]
    assert health.main(argv) == 0
    assert changed.read_text(encoding="utf-8") == "true"  # no previous digest
    assert json.loads(out.read_text(encoding="utf-8"))["alerts"] == []

    # Second run, same state, only the timestamp moved -> no commit.
    facts_file.write_text(
        json.dumps(facts(generated_at="2026-09-19T14:00:00+00:00")), encoding="utf-8"
    )
    assert health.main(
        ["--facts", str(facts_file), "--previous", str(out), "--changed-output", str(changed)]
    ) == 0
    assert changed.read_text(encoding="utf-8") == "false"


def test_cli_treats_a_corrupt_previous_digest_as_changed(tmp_path: Path) -> None:
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps(facts()), encoding="utf-8")
    previous = tmp_path / "previous.json"
    previous.write_text("{ not json", encoding="utf-8")
    changed = tmp_path / "changed.txt"

    assert health.main(
        ["--facts", str(facts_file), "--previous", str(previous), "--changed-output", str(changed)]
    ) == 0
    assert changed.read_text(encoding="utf-8") == "true"
