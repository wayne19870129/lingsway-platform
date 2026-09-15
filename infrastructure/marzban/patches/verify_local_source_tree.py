#!/usr/bin/env python3
"""Fail-closed local source-tree contract verifier (TASK-T16 Phase 2C2).

`verify_pinned_upstream.py` proves the pinned commit's contract holds by
fetching files fresh over the network. That is the right check for CI,
but a patched-image build (`../build_patched_image.sh`) already has a
local, git-verified checkout of that exact pinned commit on disk before
it applies the routing_principal patch -- re-fetching the same three
files over the network a second time would only add a redundant network
dependency to the build, not a stronger guarantee. This script checks the
identical contract (file existence, sha256 hashes, the exact Xray client
email formula, and the pinned dependency versions) against that local
checkout instead, fail-closed the same way: non-zero exit and every
problem reported, never a skip or partial pass.

This script does **not** verify the checkout's git HEAD -- that is
`../build_patched_image.sh`'s own responsibility (a plain `git
rev-parse HEAD` comparison against `PINNED_COMMIT`, done before this
script runs), and is deliberately not duplicated here so a caller cannot
satisfy this script's checks with an arbitrary directory that merely
happens to contain byte-identical files.

Every constant and check this script uses (`PINNED_FILES`, the Xray email
formula, the pinned dependency versions, and the shared
`check_pinned_content()` check logic) comes from
`pinned_upstream_manifest.py` -- the single source of truth also used by
`verify_pinned_upstream.py` and the pytest contract fixtures. Nothing
here re-types or independently re-derives those facts.

Usage:
    python infrastructure/marzban/patches/verify_local_source_tree.py <source-tree-root>
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from pinned_upstream_manifest import PINNED_FILES, check_pinned_content


def collect_errors(source_root: Path) -> list[str]:
    """Return human-readable problem descriptions for every pinned-file
    contract check that failed against the local `source_root` checkout.
    Empty means the contract holds. Never raises for an expected failure
    mode (missing file, hash mismatch, missing formula, dependency
    drift) -- collected as strings so every problem is reported at once.

    Unlike `verify_pinned_upstream.py` (where `fetch_pinned_file()` already
    guarantees the hash before `check_pinned_content()` runs), this local
    checkout was never hash-verified by anything upstream of this
    function, so the hash check happens here directly -- and, on a
    mismatch, checking still continues into `check_pinned_content()`
    rather than stopping, so a source tree that is wrong in more than one
    way is reported completely.
    """
    errors: list[str] = []
    for relative_path in PINNED_FILES:
        file_path = source_root / relative_path
        if not file_path.is_file():
            errors.append(
                f"expected pinned file missing from local source tree: {file_path} "
                "(this usually means the wrong directory was passed as "
                "<source-tree-root>, or the checkout is incomplete)"
            )
            continue

        content = file_path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        expected = PINNED_FILES[relative_path]
        if digest != expected:
            errors.append(
                f"{relative_path} sha256 mismatch: expected {expected}, got {digest}. "
                "Refusing to treat unverified content as the pinned upstream source."
            )
        errors.extend(check_pinned_content(relative_path, content))
    return errors


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: verify_local_source_tree.py <source-tree-root>", file=sys.stderr)
        return 2

    source_root = Path(argv[0])
    if not source_root.is_dir():
        print(f"FAIL-CLOSED: source tree root is not a directory: {source_root}", file=sys.stderr)
        return 1

    errors = collect_errors(source_root)
    if errors:
        for message in errors:
            print(f"FAIL-CLOSED: {message}", file=sys.stderr)
        print(
            f"\nlocal source-tree contract verification FAILED ({len(errors)} "
            "problem(s)); refusing to treat this checkout as contract-ready.",
            file=sys.stderr,
        )
        return 1

    print(
        "OK: local Marzban source tree matches the pinned upstream contract "
        "(file hashes, Xray email formula, pinned dependency versions)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
