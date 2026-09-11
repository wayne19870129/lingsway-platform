"""Fail-closed consistency guard for the Marzban contract dependency lock
(TASK-T16 Phase 2B4, Path B).

`requirements-contract.txt` is a `pip-compile --generate-hashes` output:
the CI-authoritative, fully resolved (top-level + transitive), hash-
verified dependency set for `infrastructure/marzban/patches/`. A
committed lock file that nobody checks for staleness is worse than no
lock at all -- this module proves, mechanically, that the lock still:

1. Contains every top-level pin recorded in `requirements-contract.in`,
   at the exact same version (missing or drifted -> fail).
2. Pins every single resolved package (top-level and transitive alike)
   to an exact `==` version -- no range, no unpinned line (any other
   pip requirement-line operator, or a bare name -> fail).
3. Records at least one `--hash=` entry for every resolved package
   (missing hashes -> fail, since `--require-hashes` in CI would then
   silently accept an unverified download for that one package).

See ../README.md's "Reproducible dependency lock" section for the
generation command/tool version and why `--require-hashes` matters.
"""

from __future__ import annotations

import re
from pathlib import Path

_PIN_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)")
_ANY_REQUIREMENT_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(==|>=|<=|~=|!=|>|<)?")
_HASH_LINE = re.compile(r"--hash=sha256:[0-9a-f]{64}")


def _normalize(name: str) -> str:
    """PEP 503 normalization: pip-compile/pip treat `Foo_Bar` and
    `foo-bar` as the same distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_top_level_pins(text: str) -> dict[str, str]:
    """Parse a `requirements-contract.in`-shaped file: one `name==version`
    pin per non-comment, non-blank line. Raises ValueError on any line
    that isn't an exact `==` pin, since a top-level requirement this
    patch's contract depends on must never be left unpinned."""
    pins: dict[str, str] = {}
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        match = _PIN_LINE.match(line)
        if not match:
            raise ValueError(
                f"requirements-contract.in line {lineno} is not an exact "
                f"'name==version' pin: {raw_line!r}"
            )
        pins[_normalize(match.group(1))] = match.group(2)
    return pins


def parse_lock(text: str) -> dict[str, dict[str, object]]:
    """Parse a `pip-compile --generate-hashes` output into
    `{normalized_name: {"version": str, "raw_name": str, "hash_count": int}}`.

    Deliberately hand-rolled rather than reusing pip's own internal
    requirements parser (private API, not a stable contract to depend
    on) -- this only needs to understand the specific line shapes
    pip-compile emits, and it's more useful for this guard to fail loudly
    on an unrecognized shape than to silently accept it.
    """
    packages: dict[str, dict[str, object]] = {}
    current: str | None = None

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if _HASH_LINE.search(stripped):
            if current is None:
                raise ValueError(f"hash line with no preceding package entry: {raw_line!r}")
            packages[current]["hash_count"] = int(packages[current]["hash_count"]) + len(
                _HASH_LINE.findall(stripped)
            )
            continue

        # A new package entry: "name==version \" or "name==version" (last
        # line of the file, no continuation).
        pin_match = _PIN_LINE.match(stripped)
        if pin_match:
            name, version = pin_match.group(1), pin_match.group(2)
            normalized = _normalize(name)
            packages[normalized] = {"version": version, "raw_name": name, "hash_count": 0}
            current = normalized
            continue

        # Any other non-pin, non-hash, non-comment line (e.g. an
        # unpinned "name>=1.0" or a bare "name") is a lock-integrity
        # violation -- pip-compile with --generate-hashes never emits
        # these itself, so seeing one means either hand-editing or a
        # generation mode this guard doesn't trust.
        loose_match = _ANY_REQUIREMENT_LINE.match(stripped)
        if loose_match and not stripped.startswith("# via"):
            name = loose_match.group(1)
            normalized = _normalize(name)
            packages[normalized] = {"version": None, "raw_name": name, "hash_count": 0}
            current = normalized

    return packages


def collect_lock_errors(top_level_in_text: str, lock_text: str) -> list[str]:
    """Return every lock-integrity problem found; empty means the lock
    is consistent with the `.in` file and every entry is exact-pinned
    with at least one hash."""
    errors: list[str] = []

    try:
        top_level = parse_top_level_pins(top_level_in_text)
    except ValueError as exc:
        return [str(exc)]

    try:
        locked = parse_lock(lock_text)
    except ValueError as exc:
        return [str(exc)]

    for normalized_name, expected_version in top_level.items():
        entry = locked.get(normalized_name)
        if entry is None:
            errors.append(
                f"top-level pin {normalized_name!r}=={expected_version} from "
                "requirements-contract.in is missing from requirements-contract.txt"
            )
            continue
        if entry["version"] != expected_version:
            errors.append(
                f"top-level pin {normalized_name!r} version mismatch: "
                f"requirements-contract.in pins {expected_version!r}, "
                f"requirements-contract.txt locks {entry['version']!r}"
            )

    for entry in locked.values():
        if entry["version"] is None:
            errors.append(
                f"requirements-contract.txt entry {entry['raw_name']!r} is not "
                "an exact '==' pin"
            )
        if entry["hash_count"] == 0:
            errors.append(
                f"requirements-contract.txt entry {entry['raw_name']!r} has no "
                "--hash= entries; --require-hashes would silently accept an "
                "unverified download for this package"
            )

    return errors


def collect_lock_errors_from_paths(in_path: Path, lock_path: Path) -> list[str]:
    if not in_path.is_file():
        return [f"{in_path} does not exist"]
    if not lock_path.is_file():
        return [f"{lock_path} does not exist"]
    return collect_lock_errors(in_path.read_text(), lock_path.read_text())


if __name__ == "__main__":
    import sys

    patch_dir = Path(__file__).resolve().parent
    problems = collect_lock_errors_from_paths(
        patch_dir / "requirements-contract.in",
        patch_dir / "requirements-contract.txt",
    )
    if problems:
        for message in problems:
            print(f"FAIL-CLOSED: {message}", file=sys.stderr)
        print(
            f"\nlock consistency check FAILED ({len(problems)} problem(s)).",
            file=sys.stderr,
        )
        raise SystemExit(1)
    print("OK: requirements-contract.txt is consistent with requirements-contract.in.")
