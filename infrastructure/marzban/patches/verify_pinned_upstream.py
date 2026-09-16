#!/usr/bin/env python3
"""Fail-closed verifier for the pinned Marzban upstream contract this
patch depends on (TASK-T16 Phase 2B4).

`apply_patch.sh --check` (see ../apply_patch.sh) only proves the patch's
own context lines still apply cleanly to whatever `app/models/user.py`
it is pointed at -- it says nothing about whether that file (or its
neighbors) still forms the same *contract* this repository actually
depends on. This script is the other, independent layer: it fetches the
pinned commit's specific files fresh over the network and verifies,
fail-closed (non-zero exit on any mismatch, never a skip or a stale
fallback):

1. `app/models/user.py` content hash -- the patch's own target file.
2. `app/xray/operations.py` content hash AND that it still contains the
   exact Xray client email formula (`f"{dbuser.id}.{dbuser.username}"`)
   this repository's `routing_principal` patch must keep matching.
3. `requirements.txt` content hash AND that it still pins the exact
   pydantic/fastapi/starlette/SQLAlchemy versions this patch and its
   contract tests were verified against.

Run via `python infrastructure/marzban/patches/verify_pinned_upstream.py`
(see .github/workflows/ci.yml's `marzban-contract` job, which runs this
before the pytest contract suite).
"""

from __future__ import annotations

import sys

from pinned_upstream_manifest import (
    PINNED_FILES,
    PinnedUpstreamFetchError,
    check_pinned_content,
    fetch_pinned_file,
)
from pinned_upstream_manifest import (
    check_dependency_versions as check_dependency_versions,  # noqa: PLC0414
)


def collect_errors() -> list[str]:
    """Return human-readable problem descriptions for every pinned-
    upstream contract check that failed. Empty means the contract holds.

    Never raises for an expected failure mode (fetch error, hash
    mismatch, missing formula, dependency drift) -- those are collected
    as strings so callers (the CLI `main()` below, and the test suite)
    can report every problem at once instead of stopping at the first.
    """
    errors: list[str] = []

    for path in PINNED_FILES:
        try:
            content = fetch_pinned_file(path)
        except PinnedUpstreamFetchError as exc:
            errors.append(str(exc))
            continue

        # fetch_pinned_file() already verified the hash before returning;
        # check_pinned_content() re-checks it (always matches here) plus
        # the formula/dependency checks that are the actual point of
        # calling it -- shared with verify_local_source_tree.py so this
        # logic lives in exactly one place.
        errors.extend(check_pinned_content(path, content))

    return errors


def main() -> int:
    errors = collect_errors()
    if errors:
        for message in errors:
            print(f"FAIL-CLOSED: {message}", file=sys.stderr)
        print(
            f"\npinned-upstream contract verification FAILED ({len(errors)} "
            "problem(s)); refusing to treat this commit as contract-ready.",
            file=sys.stderr,
        )
        return 1

    print(
        "OK: pinned Marzban upstream contract verified (file hashes, Xray "
        "email formula, pinned dependency versions)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
