"""Fail-closed contract for ../lock_consistency.py (TASK-T16 Phase 2B4,
Path B: reproducible `marzban-contract` CI dependency lock).

A committed lock file nobody checks for staleness is worse than no lock
at all -- these tests prove the guard actually catches every drift mode
it claims to (missing top-level pin, version mismatch, an unpinned
transitive line, a missing hash), not just that it happens to pass
against today's real lock file.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PATCH_DIR = Path(__file__).resolve().parent.parent
if str(_PATCH_DIR) not in sys.path:
    sys.path.insert(0, str(_PATCH_DIR))

from lock_consistency import (  # noqa: E402
    collect_lock_errors,
    collect_lock_errors_from_paths,
)

_GOOD_IN = "fastapi==0.115.2\nhttpx==0.28.1\n"
# Fake but well-formed (64 lowercase hex chars) sha256-shaped hashes --
# these packages/hashes are not real, only used to exercise the parser.
_HASH_A = "a" * 64
_HASH_B = "b" * 64
_HASH_C = "c" * 64
_GOOD_LOCK = f"""\
fastapi==0.115.2 \\
    --hash=sha256:{_HASH_A}
    # via -r requirements-contract.in
httpx==0.28.1 \\
    --hash=sha256:{_HASH_B}
    # via -r requirements-contract.in
starlette==0.40.0 \\
    --hash=sha256:{_HASH_C}
    # via fastapi
"""


def test_the_real_committed_lock_is_consistent() -> None:
    """The actual, currently-committed lock must pass -- this is the
    baseline every other test in this file deliberately breaks."""
    errors = collect_lock_errors_from_paths(
        _PATCH_DIR / "requirements-contract.in",
        _PATCH_DIR / "requirements-contract.txt",
    )
    assert errors == []


def test_consistent_synthetic_lock_passes() -> None:
    assert collect_lock_errors(_GOOD_IN, _GOOD_LOCK) == []


def test_top_level_version_mismatch_fails_closed() -> None:
    drifted_lock = _GOOD_LOCK.replace("fastapi==0.115.2", "fastapi==0.116.0")
    errors = collect_lock_errors(_GOOD_IN, drifted_lock)
    assert len(errors) == 1
    assert "fastapi" in errors[0]
    assert "0.115.2" in errors[0]
    assert "0.116.0" in errors[0]


def test_missing_top_level_pin_fails_closed() -> None:
    lock_without_httpx = "\n".join(
        line for line in _GOOD_LOCK.splitlines() if "httpx" not in line
    )
    errors = collect_lock_errors(_GOOD_IN, lock_without_httpx)
    assert len(errors) == 1
    assert "httpx" in errors[0]
    assert "missing" in errors[0]


def test_unpinned_transitive_line_fails_closed() -> None:
    unpinned_lock = _GOOD_LOCK.replace(
        "starlette==0.40.0 \\",
        "starlette>=0.40.0 \\",
    )
    errors = collect_lock_errors(_GOOD_IN, unpinned_lock)
    assert len(errors) == 1
    assert "starlette" in errors[0]
    assert "not an exact" in errors[0]


def test_missing_hash_fails_closed() -> None:
    lock_without_hash = _GOOD_LOCK.replace(f"    --hash=sha256:{_HASH_C}\n", "")
    errors = collect_lock_errors(_GOOD_IN, lock_without_hash)
    assert len(errors) == 1
    assert "starlette" in errors[0]
    assert "no --hash" in errors[0]


def test_multiple_simultaneous_problems_are_all_reported() -> None:
    broken_lock = (
        _GOOD_LOCK.replace("fastapi==0.115.2", "fastapi==0.116.0")
        .replace("starlette==0.40.0 \\", "starlette>=0.40.0 \\")
    )
    errors = collect_lock_errors(_GOOD_IN, broken_lock)
    assert len(errors) == 2


def test_missing_files_fail_closed(tmp_path: Path) -> None:
    errors = collect_lock_errors_from_paths(
        tmp_path / "requirements-contract.in",
        tmp_path / "requirements-contract.txt",
    )
    assert len(errors) == 1
    assert "does not exist" in errors[0]
