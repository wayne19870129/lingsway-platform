"""Decide whether a pull request may be merged automatically (ADR-029 §4).

This module is the *entire* decision. `.github/workflows/auto-merge.yml`
collects facts from the GitHub API, hands them to `evaluate()`, and merges
only when the returned decision is exactly ``MERGE``. Keeping the logic here
rather than inline in the workflow is deliberate: it is the only way the
ADR-029 "验证要求" can be satisfied with a reproducible check instead of
"逻辑上应该成立" -- `backend/tests/guards/test_automerge_gate.py` runs the
full truth table in CI.

Design rules this file follows, all of them fail-closed:

* Anything unknown blocks. A missing switch file, an unknown check
  conclusion, an unreadable mergeability flag -- none of them merge.
* There is no bypass, and no input that disables a condition. ADR-029 §4
  forbids one, mirroring iron rule 3's "unmatched traffic BLOCKs".
* All conditions are evaluated, not short-circuited, so the workflow log
  shows the complete truth table for the SHA it ran on.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Final

#: Workflows whose check runs must all be present and successful.
REQUIRED_WORKFLOWS: Final[tuple[str, ...]] = ("CI", "Security", "Risk classification")

#: Check conclusions that do not block. Everything else does -- including
#: ``neutral`` and ``action_required``, which are not evidence of success.
PASSING_CONCLUSIONS: Final[frozenset[str]] = frozenset({"success", "skipped"})

#: Only these author associations may cast a binding verdict. Anyone can type
#: "Verdict: PASS" into a comment box; only someone with write access to this
#: repository is part of the review model ADR-029 §1 describes.
TRUSTED_ASSOCIATIONS: Final[frozenset[str]] = frozenset(
    {"OWNER", "MEMBER", "COLLABORATOR"}
)

#: ADR-029 §4.6 -- the scheduled architecture audit must not approve itself.
AUDIT_BRANCH_PREFIX: Final[str] = "claude/audit-"

#: ADR-029 "不做的事" -- production deployment keeps its manual gate.
PROTECTED_PATH_PREFIXES: Final[tuple[str, ...]] = ("deploy/",)

#: The label that switches a single PR off without touching the breaker.
BLOCKING_LABEL: Final[str] = "no-automerge"

_FULL_SHA_RE: Final[re.Pattern[str]] = re.compile(r"\A[0-9a-f]{40}\Z", re.IGNORECASE)
_VERDICT_RE: Final[re.Pattern[str]] = re.compile(
    r"Verdict\s*[:：]\s*(PASS|NEEDS_CHANGES)\b"
)

MERGE: Final[str] = "MERGE"
BLOCK: Final[str] = "BLOCK"
WAIT: Final[str] = "WAIT"


@dataclass
class Gate:
    """Accumulates the outcome of every condition, then reports one decision."""

    blocks: list[str] = field(default_factory=list)
    waits: list[str] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)

    def check(self, name: str, ok: bool, reason: str) -> None:
        if ok:
            self.passed.append(name)
        else:
            self.blocks.append(f"{name}: {reason}")

    def block(self, name: str, reason: str) -> None:
        self.blocks.append(f"{name}: {reason}")

    def wait(self, name: str, reason: str) -> None:
        self.waits.append(f"{name}: {reason}")

    def decision(self) -> str:
        if self.blocks:
            return BLOCK
        if self.waits:
            return WAIT
        return MERGE


def _strip_markdown(body: str) -> str:
    """Drop emphasis/code markers so ``**Verdict:** PASS`` parses.

    ``_`` is deliberately kept: stripping it would turn ``NEEDS_CHANGES``
    into ``NEEDSCHANGES`` and a rejection would read as no verdict at all.
    """
    return re.sub(r"[*`]", "", body)


def _verdict_of(body: str) -> str | None:
    """Return the verdict a review body carries, or ``None``.

    A body containing both verdicts resolves to ``NEEDS_CHANGES``: a review
    that rejects anywhere is a rejection.
    """
    found = {match.group(1) for match in _VERDICT_RE.finditer(_strip_markdown(body))}
    if "NEEDS_CHANGES" in found:
        return "NEEDS_CHANGES"
    if "PASS" in found:
        return "PASS"
    return None


def _mentions_sha(body: str, head_sha: str) -> bool:
    pattern = rf"(?<![0-9a-fA-F]){re.escape(head_sha)}(?![0-9a-fA-F])"
    return re.search(pattern, _strip_markdown(body)) is not None


def _evaluate_review(gate: Gate, facts: dict[str, Any], head_sha: str) -> None:
    """Condition 1: a ``Verdict: PASS`` scoped to *this* head SHA.

    Reviews that name a different SHA are stale and are ignored rather than
    counted either way -- `CLAUDE.md` "Only act on a review of the PR's
    current head SHA".
    """
    scoped: list[tuple[str, str, str]] = []
    for review in facts.get("reviews") or []:
        body = str(review.get("body") or "")
        association = str(review.get("author_association") or "").upper()
        verdict = _verdict_of(body)
        if verdict is None:
            continue
        if association not in TRUSTED_ASSOCIATIONS:
            continue
        if not _mentions_sha(body, head_sha):
            continue
        scoped.append(
            (str(review.get("submitted_at") or ""), verdict, str(review.get("author") or "?"))
        )

    if not scoped:
        gate.wait("review", f"no verdict scoped to head SHA {head_sha} from a trusted author")
        return

    submitted_at, verdict, author = max(scoped, key=lambda item: item[0])
    if verdict != "PASS":
        gate.block("review", f"latest verdict on {head_sha} is {verdict} (by {author})")
        return
    gate.passed.append(f"review (PASS on {head_sha} by {author} at {submitted_at})")


def _evaluate_checks(gate: Gate, facts: dict[str, Any]) -> None:
    """Condition 2: every required check succeeded on this head SHA."""
    runs = facts.get("check_runs") or []
    required = tuple(facts.get("required_workflows") or REQUIRED_WORKFLOWS)

    seen_workflows = {str(run.get("workflow") or "") for run in runs}
    for workflow in required:
        if workflow not in seen_workflows:
            gate.wait("checks", f"required workflow {workflow!r} has not reported yet")

    for run in runs:
        name = str(run.get("name") or "?")
        status = str(run.get("status") or "")
        conclusion = str(run.get("conclusion") or "")
        if status != "completed":
            gate.wait("checks", f"{name} is {status or 'unknown'}")
        elif conclusion not in PASSING_CONCLUSIONS:
            gate.block("checks", f"{name} concluded {conclusion or 'unknown'}")


def _evaluate_switch(gate: Gate, facts: dict[str, Any]) -> None:
    """Condition 4: the circuit breaker, read from the *default* branch.

    The collector must read `.github/automerge-enabled` at the default branch,
    never at the PR head -- otherwise a pull request could switch the breaker
    on for itself. ``None`` means the file is absent, which blocks.
    """
    raw = facts.get("automerge_switch")
    if raw is None:
        gate.block(
            "circuit-breaker",
            ".github/automerge-enabled is absent on the default branch",
        )
        return
    value = str(raw).strip()
    if value != "true":
        gate.block("circuit-breaker", f".github/automerge-enabled is {value!r}, not 'true'")
        return
    gate.passed.append("circuit-breaker (enabled)")


def evaluate(facts: dict[str, Any]) -> dict[str, Any]:
    """Return the auto-merge decision for one pull request.

    The result is ``{"decision": MERGE|BLOCK|WAIT, "blocks": [...],
    "waits": [...], "passed": [...]}``. Only ``MERGE`` authorizes a merge;
    ``WAIT`` means "not yet, a later event will re-evaluate".
    """
    gate = Gate()

    head_sha = str(facts.get("head_sha") or "")
    if not _FULL_SHA_RE.match(head_sha):
        gate.block("head-sha", f"{head_sha!r} is not a full 40-character commit SHA")
        return _result(gate, facts)

    gate.check(
        "state",
        str(facts.get("state") or "") == "open",
        f"pull request is {facts.get('state')!r}",
    )
    gate.check("draft", not bool(facts.get("draft")), "pull request is a draft")
    gate.check("merged", not bool(facts.get("merged")), "pull request is already merged")

    _evaluate_switch(gate, facts)

    labels = {str(label).lower() for label in facts.get("labels") or []}
    gate.check("label", BLOCKING_LABEL not in labels, f"{BLOCKING_LABEL!r} label is present")

    head_ref = str(facts.get("head_ref") or "")
    gate.check(
        "audit-branch",
        not head_ref.startswith(AUDIT_BRANCH_PREFIX),
        f"branch {head_ref!r} is an architecture audit (ADR-029 §4.6)",
    )

    changed = [str(path) for path in facts.get("changed_files") or []]
    protected = sorted(
        path for path in changed if path.startswith(PROTECTED_PATH_PREFIXES)
    )
    gate.check(
        "protected-paths",
        not protected,
        f"touches manually gated paths: {', '.join(protected)}",
    )

    mergeable = facts.get("mergeable")
    if mergeable is True:
        gate.passed.append("mergeable (no conflict)")
    elif mergeable is False:
        gate.block("mergeable", "the pull request has a merge conflict")
    else:
        gate.wait("mergeable", "GitHub has not computed mergeability yet")

    _evaluate_checks(gate, facts)
    _evaluate_review(gate, facts, head_sha)

    return _result(gate, facts)


def _result(gate: Gate, facts: dict[str, Any]) -> dict[str, Any]:
    return {
        "pr_number": facts.get("pr_number"),
        "head_sha": facts.get("head_sha"),
        "decision": gate.decision(),
        "blocks": gate.blocks,
        "waits": gate.waits,
        "passed": gate.passed,
    }


def render(result: dict[str, Any]) -> str:
    lines = [
        f"PR #{result.get('pr_number')} @ {result.get('head_sha')}",
        f"decision: {result['decision']}",
    ]
    for label, key in (("BLOCK", "blocks"), ("WAIT", "waits"), ("OK", "passed")):
        for entry in result.get(key) or []:
            lines.append(f"  [{label}] {entry}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--facts",
        required=True,
        help="path to the collected facts JSON, or '-' to read stdin",
    )
    parser.add_argument(
        "--output",
        help="write the decision JSON here in addition to stdout",
    )
    args = parser.parse_args(argv)

    if args.facts == "-":
        raw = sys.stdin.read()
    else:
        with open(args.facts, encoding="utf-8") as handle:
            raw = handle.read()
    result = evaluate(json.loads(raw))

    print(render(result))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
    # A non-MERGE decision is a normal outcome, not a workflow failure.
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
