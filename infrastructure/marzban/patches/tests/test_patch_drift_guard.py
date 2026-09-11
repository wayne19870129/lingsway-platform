"""Fail-closed drift-guard contract for apply_patch.sh (ADR-016 Decision 3).

Every scenario here runs the real script against real files with a real
`git apply` underneath -- nothing here is simulated/mocked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

PATCH_DIR = Path(__file__).resolve().parent.parent
APPLY_SCRIPT = PATCH_DIR / "apply_patch.sh"
VENDORED_UPSTREAM_USER_PY = PATCH_DIR / "vendor" / "upstream" / "app" / "models" / "user.py"

# The exact context block the patch's second hunk depends on -- copied
# verbatim from the vendored pinned source so a targeted mutation of it
# (below) reproduces a realistic "upstream drifted around this exact
# code" scenario, not just corrupting arbitrary bytes.
_PATCH_DEPENDENT_CONTEXT = (
    "    admin: Optional[Admin] = None\n"
    "    model_config = ConfigDict(from_attributes=True)\n"
    "\n"
    '    @model_validator(mode="after")\n'
    "    def validate_links(self):"
)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(APPLY_SCRIPT), *args],
        capture_output=True,
        text=True,
    )


def _seed_pristine_source(root: Path) -> Path:
    target = root / "app" / "models" / "user.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(VENDORED_UPSTREAM_USER_PY.read_bytes())
    return target


def test_patch_applies_cleanly_to_correct_pinned_source(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    target = _seed_pristine_source(source_root)

    check_result = _run("--check", str(source_root))
    assert check_result.returncode == 0, check_result.stderr
    # --check must never mutate the source tree.
    assert VENDORED_UPSTREAM_USER_PY.read_bytes() == target.read_bytes()

    apply_result = _run(str(source_root))
    assert apply_result.returncode == 0, apply_result.stderr
    assert "id: int = Field(exclude=True)" in target.read_text()
    assert "def routing_principal(self) -> str:" in target.read_text()


def test_drift_in_the_patch_dependent_context_fails_closed(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    target = _seed_pristine_source(source_root)

    original = target.read_text()
    assert _PATCH_DEPENDENT_CONTEXT in original, (
        "test fixture assumption broken: the vendored pinned source no "
        "longer contains the exact context block this test mutates -- "
        "update _PATCH_DEPENDENT_CONTEXT to match the current vendored file"
    )
    mutated = original.replace(
        _PATCH_DEPENDENT_CONTEXT,
        _PATCH_DEPENDENT_CONTEXT.replace(
            "model_config = ConfigDict(from_attributes=True)",
            "owner_note: Optional[str] = None  # simulated upstream drift\n"
            "    model_config = ConfigDict(from_attributes=True)",
        ),
        1,
    )
    assert mutated != original
    target.write_text(mutated)

    check_result = _run("--check", str(source_root))
    assert check_result.returncode != 0
    assert "FAIL-CLOSED" in check_result.stderr
    # The drifted file must be left exactly as the (already-drifted) input
    # was -- --check never mutates anything, successfully or not.
    assert target.read_text() == mutated

    apply_result = _run(str(source_root))
    assert apply_result.returncode != 0
    assert "FAIL-CLOSED" in apply_result.stderr
    # A rejected `git apply` must never partially write the target file.
    assert target.read_text() == mutated
    assert "id: int = Field(exclude=True)" not in target.read_text()


def test_missing_target_file_fails_closed(tmp_path: Path) -> None:
    source_root = tmp_path / "empty-source"
    (source_root / "app" / "models").mkdir(parents=True)

    check_result = _run("--check", str(source_root))
    assert check_result.returncode != 0
    assert "FAIL-CLOSED" in check_result.stderr

    apply_result = _run(str(source_root))
    assert apply_result.returncode != 0
    assert "FAIL-CLOSED" in apply_result.stderr


def test_missing_patch_file_fails_closed(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    _seed_pristine_source(source_root)

    # Point the script at a copy of itself in an empty directory so its own
    # sibling patch file cannot be found -- proves the "patch file itself
    # is missing" branch, distinct from "target file is missing".
    isolated_script = tmp_path / "apply_patch.sh"
    isolated_script.write_bytes(APPLY_SCRIPT.read_bytes())
    isolated_script.chmod(0o755)

    result = subprocess.run(
        [str(isolated_script), "--check", str(source_root)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "FAIL-CLOSED" in result.stderr
    assert "patch file not found" in result.stderr
