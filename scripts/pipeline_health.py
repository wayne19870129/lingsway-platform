"""Compress pipeline state into a fixed-length digest (ADR-030 §3, TASK-S10).

`.github/workflows/pipeline-health.yml` collects raw facts from the GitHub API
and hands them here. The result is the **only** input Claude reads when
supervising, so its whole value is that `alerts` can be trusted on its own:

* `alerts` empty  -> Claude stops reading. Nothing else need be opened.
* `alerts` non-empty -> the remaining fields say which pull request and why.

That is the token budget ADR-030 §3 exists to protect. A digest that requires
expanding three pull requests before it can be understood has not done its job.

Two rules this file follows:

* **Alerts are derived here, never by the reader.** Every halt signal in
  ADR-030 §5 that *can* be detected mechanically has its own enum value below.
  The one that cannot is named in `JUDGEMENT_ONLY_SIGNALS` rather than left
  silently missing -- an undocumented gap in a supervision digest reads as
  "all clear", which is the worst possible failure mode for this file.
* **Review verdicts are not re-implemented.** `latest_verdict_on_head` defers
  to `automerge_gate`, so the digest and the merge gate can never disagree
  about what counts as a `PASS`. Two implementations of that rule would drift,
  and the drift would be invisible until it mattered.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parent))

import automerge_gate  # noqa: E402  (path must be set first)

#: Halt signals from ADR-030 §5 that this module detects mechanically.
ALERT_MAIN_CI_RED: Final[str] = "MAIN_CI_RED"
ALERT_BREAKER_OFF: Final[str] = "BREAKER_OFF"
ALERT_PR_STUCK: Final[str] = "PR_STUCK_OVER_12H"
ALERT_ROUNDS_AT_CAP: Final[str] = "ROUNDS_AT_CAP"
ALERT_THREE_AUTOMERGES: Final[str] = "THREE_CONSECUTIVE_AUTOMERGES"

#: ADR-030 §5 signals that are NOT mechanically detectable, named explicitly so
#: their absence from `alerts` is never mistaken for "did not happen".
#:
#: * SAME_FINDING_FAILED_TWICE -- requires deciding whether two review findings
#:   are "the same" finding, which is a judgement about meaning, not text.
#: * PASS_BUT_CRITICAL -- "ChatGPT passed it but a Critical exists on that SHA"
#:   is exactly the judgement the supervision layer is for; a digest that could
#:   detect it would not need a supervisor.
JUDGEMENT_ONLY_SIGNALS: Final[tuple[str, ...]] = (
    "SAME_FINDING_FAILED_TWICE",
    "PASS_BUT_CRITICAL",
)

#: `CLAUDE.md`: automatic rework is capped at 5 rounds per pull request.
REVIEW_ROUND_CAP: Final[int] = 5

#: ADR-030 §5: a pull request with unresolved findings and no new commit for
#: this long is stuck, not in progress.
STUCK_AFTER_HOURS: Final[float] = 12.0

#: `docs/85` §6: pause and report rather than let the pipeline run unattended.
AUTOMERGE_STREAK_CAP: Final[int] = 3

HIGH_RISK_PREFIXES: Final[tuple[str, ...]] = (
    "backend/app/domain/",
    "backend/app/providers/",
    "infrastructure/alembic/versions/",
    "deploy/",
    "backend/app/core/secrets.py",
    "backend/app/core/security.py",
)


def _parse(moment: str | None) -> datetime | None:
    if not moment:
        return None
    try:
        return datetime.fromisoformat(str(moment).replace("Z", "+00:00"))
    except ValueError:
        return None


def _hours_between(earlier: str | None, later: str | None) -> float | None:
    start, end = _parse(earlier), _parse(later)
    if start is None or end is None:
        return None
    return round((end - start).total_seconds() / 3600.0, 2)


def latest_verdict_on_head(reviews: list[dict[str, Any]], head_sha: str) -> str:
    """Return ``PASS`` / ``NEEDS_CHANGES`` / ``none`` for this exact head SHA.

    Deliberately delegates every rule to `automerge_gate`: trusted author
    associations, exact 40-character SHA matching, and "a body carrying both
    verdicts is a rejection". The digest must never disagree with the gate.
    """
    scoped: list[tuple[str, str]] = []
    for review in reviews or []:
        body = str(review.get("body") or "")
        association = str(review.get("author_association") or "").upper()
        verdict = automerge_gate._verdict_of(body)
        if verdict is None or association not in automerge_gate.TRUSTED_ASSOCIATIONS:
            continue
        if not automerge_gate._mentions_sha(body, head_sha):
            continue
        scoped.append((str(review.get("submitted_at") or ""), verdict))
    if not scoped:
        return "none"
    return max(scoped, key=lambda item: item[0])[1]


def _summarize_pr(pr: dict[str, Any], now: str) -> dict[str, Any]:
    head_sha = str(pr.get("head_sha") or "")
    changed = [str(path) for path in pr.get("changed_files") or []]
    return {
        "number": pr.get("number"),
        "head_sha": head_sha,
        "age_hours": _hours_between(pr.get("created_at"), now),
        "hours_since_last_commit": _hours_between(pr.get("last_commit_at"), now),
        "ci": str(pr.get("ci") or "pending"),
        "latest_verdict_on_head": latest_verdict_on_head(pr.get("reviews") or [], head_sha),
        "review_rounds": int(pr.get("review_rounds") or 0),
        "labels": [str(label) for label in pr.get("labels") or []],
        "touches_high_risk_paths": any(path.startswith(HIGH_RISK_PREFIXES) for path in changed),
        "mergeable": pr.get("mergeable"),
    }


def _alerts(
    main: dict[str, Any], automerge: dict[str, Any], prs: list[dict[str, Any]]
) -> list[str]:
    found: list[str] = []

    if str(main.get("ci_conclusion") or "") == "failure":
        # A red base branch poisons every downstream judgement, so it outranks
        # anything happening on an individual pull request.
        found.append(ALERT_MAIN_CI_RED)

    if str(automerge.get("breaker") or "") != "true":
        found.append(ALERT_BREAKER_OFF)

    for pr in prs:
        idle = pr.get("hours_since_last_commit")
        unresolved = pr["latest_verdict_on_head"] in ("NEEDS_CHANGES", "none")
        if idle is not None and idle > STUCK_AFTER_HOURS and unresolved:
            found.append(ALERT_PR_STUCK)
            break

    if any(pr["review_rounds"] >= REVIEW_ROUND_CAP for pr in prs):
        found.append(ALERT_ROUNDS_AT_CAP)

    streak = int(automerge.get("merged_without_user_since_breaker_on") or 0)
    if streak >= AUTOMERGE_STREAK_CAP:
        found.append(ALERT_THREE_AUTOMERGES)

    return found


def build_digest(facts: dict[str, Any]) -> dict[str, Any]:
    """Return the fixed-shape digest. Never raises on missing fields."""
    now = str(facts.get("generated_at") or "")
    main = dict(facts.get("main") or {})
    raw_breaker = facts.get("breaker")
    automerge = {
        "breaker": "absent" if raw_breaker is None else str(raw_breaker).strip(),
        "merged_without_user_since_breaker_on": int(
            facts.get("merged_without_user_since_breaker_on") or 0
        ),
    }
    prs = [_summarize_pr(pr, now) for pr in facts.get("prs") or []]

    return {
        "generated_at": now,
        "main": {
            "sha": str(main.get("sha") or ""),
            "ci_conclusion": str(main.get("ci_conclusion") or "pending"),
            "commits_since_last_digest": int(main.get("commits_since_last_digest") or 0),
        },
        "automerge": automerge,
        "open_prs": prs,
        "alerts": _alerts(main, automerge, prs),
        "judgement_only_signals": list(JUDGEMENT_ONLY_SIGNALS),
    }


#: Fields that move on every run purely because time passed. They carry no new
#: information about the pipeline, so they must not make a digest count as
#: "changed" -- otherwise this workflow commits a no-op every two hours, which
#: is the single easiest way to turn a supervision aid into repository noise.
#: `age_hours` and `hours_since_last_commit` are derived from `generated_at`,
#: so stripping only the top-level timestamp is NOT enough.
_TIME_DERIVED_PR_FIELDS: Final[tuple[str, ...]] = ("age_hours", "hours_since_last_commit")


def without_timestamp(digest: dict[str, Any]) -> dict[str, Any]:
    """The comparable part of a digest: everything that is not just the clock."""
    comparable = {key: value for key, value in digest.items() if key != "generated_at"}
    comparable["open_prs"] = [
        {key: value for key, value in pr.items() if key not in _TIME_DERIVED_PR_FIELDS}
        for pr in comparable.get("open_prs") or []
    ]
    return comparable


def has_changed(previous: dict[str, Any] | None, current: dict[str, Any]) -> bool:
    if previous is None:
        return True
    return without_timestamp(previous) != without_timestamp(current)


def render(digest: dict[str, Any]) -> str:
    alerts = digest.get("alerts") or []
    lines = [
        f"main {digest['main']['sha'][:12]} ci={digest['main']['ci_conclusion']} "
        f"(+{digest['main']['commits_since_last_digest']} commits)",
        f"breaker={digest['automerge']['breaker']} "
        f"automerges_since_user={digest['automerge']['merged_without_user_since_breaker_on']}",
        f"alerts: {', '.join(alerts) if alerts else 'none'}",
    ]
    for pr in digest.get("open_prs") or []:
        lines.append(
            f"  #{pr['number']} ci={pr['ci']} verdict={pr['latest_verdict_on_head']} "
            f"rounds={pr['review_rounds']} idle={pr['hours_since_last_commit']}h "
            f"high_risk={pr['touches_high_risk_paths']} labels={','.join(pr['labels']) or '-'}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--facts", required=True, help="collected facts JSON, or '-' for stdin")
    parser.add_argument("--previous", help="path to the digest from the last run, if any")
    parser.add_argument("--output", help="write the new digest here")
    parser.add_argument(
        "--changed-output",
        help="write 'true'/'false' here: whether the digest changed apart from generated_at",
    )
    args = parser.parse_args(argv)

    if args.facts == "-":
        raw = sys.stdin.read()
    else:
        with open(args.facts, encoding="utf-8") as handle:
            raw = handle.read()
    digest = build_digest(json.loads(raw))

    previous: dict[str, Any] | None = None
    if args.previous and Path(args.previous).exists():
        try:
            with open(args.previous, encoding="utf-8") as handle:
                previous = json.load(handle)
        except json.JSONDecodeError:
            previous = None

    print(render(digest))

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(digest, handle, indent=2, sort_keys=True)
            handle.write("\n")
    if args.changed_output:
        with open(args.changed_output, "w", encoding="utf-8") as handle:
            handle.write("true" if has_changed(previous, digest) else "false")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
