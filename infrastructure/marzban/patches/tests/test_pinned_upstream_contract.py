"""Fail-closed contract for ../verify_pinned_upstream.py and
../pinned_upstream_manifest.py (TASK-T16 Phase 2B4).

`apply_patch.sh --check` (covered by test_patch_drift_guard.py) only
proves the patch's own context lines still apply to whatever source tree
it is pointed at. This file covers the other, independent layer: that
the pinned Marzban commit's `app/xray/operations.py` still computes the
Xray client email the same way, and that `requirements.txt` still pins
the exact dependency versions this patch and its contract tests were
verified against -- so a future upstream change that leaves
`app/models/user.py` untouched (and would therefore not be caught by the
patch-apply drift guard alone) still fails closed here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PATCH_DIR = Path(__file__).resolve().parent.parent
if str(_PATCH_DIR) not in sys.path:
    sys.path.insert(0, str(_PATCH_DIR))

import verify_pinned_upstream  # noqa: E402
from pinned_upstream_manifest import (  # noqa: E402
    PINNED_DEPENDENCIES,
    PinnedUpstreamFetchError,
)


def test_collect_errors_is_empty_for_the_real_pinned_upstream() -> None:
    """The full, real, network-verified check against the actual pinned
    commit must currently report zero problems -- this is the "contract
    currently holds" baseline every other test in this file deliberately
    breaks in one specific way."""
    assert verify_pinned_upstream.collect_errors() == []


def test_main_exits_zero_for_the_real_pinned_upstream() -> None:
    assert verify_pinned_upstream.main() == 0


def test_check_dependency_versions_passes_on_pinned_requirements_txt() -> None:
    requirements_text = "\n".join(f"{pkg}=={ver}" for pkg, ver in PINNED_DEPENDENCIES.items())
    assert verify_pinned_upstream.check_dependency_versions(requirements_text) == []


def test_check_dependency_versions_fails_closed_on_version_drift() -> None:
    requirements_text = "pydantic==2.9.0\nfastapi==0.115.2\nstarlette==0.40.0\nSQLAlchemy==2.0.36"
    problems = verify_pinned_upstream.check_dependency_versions(requirements_text)
    assert len(problems) == 1
    assert "pydantic==2.10.4" in problems[0]


def test_check_dependency_versions_fails_closed_on_missing_pin() -> None:
    requirements_text = "fastapi==0.115.2\nstarlette==0.40.0\nSQLAlchemy==2.0.36"
    problems = verify_pinned_upstream.check_dependency_versions(requirements_text)
    assert len(problems) == 1
    assert "pydantic==2.10.4" in problems[0]


def test_collect_errors_fails_closed_when_xray_email_formula_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulates upstream silently changing the Xray client email formula
    in app/xray/operations.py without touching app/models/user.py at all
    -- a change the patch-apply drift guard alone would never see."""
    real_fetch = verify_pinned_upstream.fetch_pinned_file

    def fake_fetch(path: str) -> bytes:
        if path == "app/xray/operations.py":
            return b'email = f"{dbuser.username}"  # formula silently changed\n'
        return real_fetch(path)

    monkeypatch.setattr(verify_pinned_upstream, "fetch_pinned_file", fake_fetch)

    errors = verify_pinned_upstream.collect_errors()
    assert len(errors) == 1
    assert "Xray client email formula" in errors[0]


def test_collect_errors_fails_closed_when_a_pinned_file_fetch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_fetch = verify_pinned_upstream.fetch_pinned_file

    def fake_fetch(path: str) -> bytes:
        if path == "app/models/user.py":
            raise PinnedUpstreamFetchError("simulated network failure")
        return real_fetch(path)

    monkeypatch.setattr(verify_pinned_upstream, "fetch_pinned_file", fake_fetch)

    errors = verify_pinned_upstream.collect_errors()
    assert len(errors) == 1
    assert "simulated network failure" in errors[0]


def test_main_exits_nonzero_and_reports_every_problem_on_multiple_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_fetch(path: str) -> bytes:
        if path == "app/models/user.py":
            raise PinnedUpstreamFetchError("simulated network failure")
        if path == "app/xray/operations.py":
            return b"email = 'no longer the pinned formula'\n"
        if path == "requirements.txt":
            return b"pydantic==2.9.0\nfastapi==0.115.2\nstarlette==0.40.0\nSQLAlchemy==2.0.36\n"
        raise AssertionError(f"unexpected path {path!r}")

    monkeypatch.setattr(verify_pinned_upstream, "fetch_pinned_file", fake_fetch)

    exit_code = verify_pinned_upstream.main()
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.err.count("FAIL-CLOSED") == 3
    assert "verification FAILED (3 problem(s))" in captured.err
