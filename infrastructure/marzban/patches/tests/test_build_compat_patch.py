"""Tests for `0002-pin-build-setuptools.patch` (TASK-T16 Phase 2C2).

Unlike `0001-expose-routing-principal.patch`, this patch is a build-
environment compatibility fix, not part of the routing_principal/ADR-016
contract: the pinned commit's own `Dockerfile` runs
`pip install --upgrade pip setuptools` with no version pin, which
resolves whatever setuptools is newest on the day the image is built.
setuptools >=81 dropped `pkg_resources`, which the pinned
`apscheduler==3.9.1.post1` dependency still imports unconditionally
(via `marzban-cli`'s import chain), breaking the final
`marzban-cli completion install` build step. `build_patched_image.sh`
applies this patch the same fail-closed way as the routing_principal
patch (via a direct `git apply --check`/`git apply`, not
`apply_patch.sh`, since that script is hardcoded to the routing_principal
patch's own target file).

These tests fetch the pinned commit's real `Dockerfile` fresh over the
network (the same non-hermetic tradeoff `patches/README.md`'s "Upstream
source: fetched, not vendored" section already documents for
`app/models/user.py`) and drive real `git apply` runs against it -- not a
hand-edited fixture.
"""

from __future__ import annotations

import subprocess
import urllib.request
from pathlib import Path

import pytest
from pinned_upstream_manifest import PINNED_COMMIT

_PATCH_FILE = Path(__file__).resolve().parent.parent / "0002-pin-build-setuptools.patch"
_DOCKERFILE_URL = f"https://raw.githubusercontent.com/Gozargah/Marzban/{PINNED_COMMIT}/Dockerfile"


def _fetch_pinned_dockerfile_or_fail() -> bytes:
    try:
        with urllib.request.urlopen(_DOCKERFILE_URL, timeout=20) as response:  # noqa: S310
            return response.read()
    except OSError as exc:
        pytest.fail(f"failed to fetch pinned Dockerfile from {_DOCKERFILE_URL}: {exc}")


def _write_dockerfile(root: Path, content: bytes) -> Path:
    dockerfile = root / "Dockerfile"
    dockerfile.write_bytes(content)
    return dockerfile


def test_patch_applies_cleanly_to_the_real_pinned_dockerfile(tmp_path: Path) -> None:
    dockerfile = _write_dockerfile(tmp_path, _fetch_pinned_dockerfile_or_fail())

    check = subprocess.run(
        ["git", "apply", "--check", str(_PATCH_FILE)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stderr

    apply = subprocess.run(
        ["git", "apply", str(_PATCH_FILE)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert apply.returncode == 0, apply.stderr

    patched_text = dockerfile.read_text(encoding="utf-8")
    assert 'pip install --upgrade pip "setuptools<81"' in patched_text
    assert "pip install --upgrade pip setuptools \\" not in patched_text


def test_patch_fails_closed_on_drifted_dockerfile(tmp_path: Path) -> None:
    original = _fetch_pinned_dockerfile_or_fail().decode("utf-8")
    drifted = original.replace(
        "RUN python3 -m pip install --upgrade pip setuptools \\",
        "RUN python3 -m pip install --upgrade pip setuptools wheel \\",
    )
    assert drifted != original, "fixture did not actually change the target line"
    _write_dockerfile(tmp_path, drifted.encode("utf-8"))

    check = subprocess.run(
        ["git", "apply", "--check", str(_PATCH_FILE)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert check.returncode != 0

    # Nothing must be written on a failed --check.
    assert dockerfile_unchanged(tmp_path, drifted)


def dockerfile_unchanged(root: Path, expected_text: str) -> bool:
    return (root / "Dockerfile").read_text(encoding="utf-8") == expected_text
