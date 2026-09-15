"""Tests for the patched-Marzban image identity contract (TASK-T16 Phase 2C2):
`pinned_upstream_manifest.validate_image_labels()` (the pure label-matching
logic shared by build and verification) and
`verify_patched_image.read_image_labels()`/`main()` (the docker-inspect-driven
CLI, exercised here with a stubbed `docker` binary so these tests need no
Docker daemon).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pinned_upstream_manifest import REQUIRED_IMAGE_LABELS, validate_image_labels

_MARZBAN_DIR = Path(__file__).resolve().parent.parent.parent
if str(_MARZBAN_DIR) not in sys.path:
    sys.path.insert(0, str(_MARZBAN_DIR))

import verify_patched_image  # noqa: E402


def test_correct_labels_pass() -> None:
    assert validate_image_labels(dict(REQUIRED_IMAGE_LABELS)) == []


def test_none_labels_fail_closed_for_every_key() -> None:
    problems = validate_image_labels(None)
    assert len(problems) == len(REQUIRED_IMAGE_LABELS)


def test_empty_labels_fail_closed_for_every_key() -> None:
    problems = validate_image_labels({})
    assert len(problems) == len(REQUIRED_IMAGE_LABELS)


def test_wrong_upstream_commit_label_fails() -> None:
    labels = dict(REQUIRED_IMAGE_LABELS)
    labels["org.lingsway.marzban.upstream-commit"] = "0" * 40
    problems = validate_image_labels(labels)
    assert len(problems) == 1
    assert "upstream-commit" in problems[0]


def test_wrong_patch_marker_fails() -> None:
    labels = dict(REQUIRED_IMAGE_LABELS)
    labels["org.lingsway.marzban.routing-principal-patch"] = "unpatched"
    problems = validate_image_labels(labels)
    assert len(problems) == 1
    assert "routing-principal-patch" in problems[0]


def test_extra_unrelated_labels_do_not_affect_validation() -> None:
    labels = dict(REQUIRED_IMAGE_LABELS)
    labels["org.opencontainers.image.title"] = "marzban"
    assert validate_image_labels(labels) == []


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_read_image_labels_parses_docker_inspect_json(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(dict(REQUIRED_IMAGE_LABELS))

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003 - subprocess.run stand-in
        assert cmd[0] == "docker"
        assert cmd[1] == "inspect"
        return _FakeCompletedProcess(0, stdout=payload + "\n")

    monkeypatch.setattr(verify_patched_image.subprocess, "run", fake_run)
    labels = verify_patched_image.read_image_labels("lingsway/marzban:v0.8.4-routing-principal")
    assert labels == dict(REQUIRED_IMAGE_LABELS)


def test_read_image_labels_treats_null_as_no_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
        return _FakeCompletedProcess(0, stdout="null\n")

    monkeypatch.setattr(verify_patched_image.subprocess, "run", fake_run)
    assert verify_patched_image.read_image_labels("some-image") == {}


def test_read_image_labels_fails_closed_when_docker_inspect_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
        return _FakeCompletedProcess(1, stderr="Error: No such object: does-not-exist")

    monkeypatch.setattr(verify_patched_image.subprocess, "run", fake_run)
    with pytest.raises(verify_patched_image.ImageInspectionError):
        verify_patched_image.read_image_labels("does-not-exist")


def test_read_image_labels_fails_closed_on_malformed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
        return _FakeCompletedProcess(0, stdout="not-json")

    monkeypatch.setattr(verify_patched_image.subprocess, "run", fake_run)
    with pytest.raises(verify_patched_image.ImageInspectionError):
        verify_patched_image.read_image_labels("some-image")


def test_read_image_labels_fails_closed_when_docker_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
        raise FileNotFoundError("docker")

    monkeypatch.setattr(verify_patched_image.subprocess, "run", fake_run)
    with pytest.raises(verify_patched_image.ImageInspectionError):
        verify_patched_image.read_image_labels("some-image")


def test_main_succeeds_for_correctly_labeled_image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        verify_patched_image,
        "read_image_labels",
        lambda image_ref, **_: dict(REQUIRED_IMAGE_LABELS),
    )
    assert verify_patched_image.main(["lingsway/marzban:v0.8.4-routing-principal"]) == 0


def test_main_fails_closed_for_unpatched_image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        verify_patched_image,
        "read_image_labels",
        lambda image_ref, **_: {},
    )
    assert verify_patched_image.main(["gozargah/marzban:v0.8.4"]) == 1


def test_main_fails_closed_when_inspection_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_error(image_ref, **_):  # noqa: ANN001
        raise verify_patched_image.ImageInspectionError("boom")

    monkeypatch.setattr(verify_patched_image, "read_image_labels", raise_error)
    assert verify_patched_image.main(["some-image"]) == 1


def test_main_rejects_wrong_argument_count() -> None:
    assert verify_patched_image.main([]) == 2
    assert verify_patched_image.main(["a", "b"]) == 2


def test_custom_marzban_image_override_without_correct_identity_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item J: a MARZBAN_IMAGE override that isn't the verified patched
    build must fail closed exactly like the compose default would --
    there is no bypass for a custom image reference."""
    monkeypatch.setattr(
        verify_patched_image,
        "read_image_labels",
        lambda image_ref, **_: {"some.other.label": "value"},
    )
    assert verify_patched_image.main(["registry.example.com/custom/marzban:latest"]) == 1
