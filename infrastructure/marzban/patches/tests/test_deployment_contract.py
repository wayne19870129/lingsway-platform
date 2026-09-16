"""Deployment contract tests for TASK-T16 Phase 2C2.

The resolver is tested through the real shell function with a fake Docker
Compose executable. The fake executable returns the effective Compose JSON
that a real docker compose config --format json call provides; it never
contacts Docker, Marzban, or a registry.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_STACK_SCRIPT = _REPO_ROOT / "deploy" / "lib" / "40_stack_up.sh"
_TRANSPORT_COMPOSE = _REPO_ROOT / "infrastructure" / "compose" / "compose.transport.yml"
_PROBE_COMPOSE = _REPO_ROOT / "infrastructure" / "compose" / "compose.probe.yml"
_PATCHED_IMAGE = "lingsway/marzban:v0.8.4-routing-principal"
_UPSTREAM_IMAGE = "gozargah/marzban:v0.8.4"


def _run_resolver(
    tmp_path: Path,
    effective_provider: str | None,
    *,
    custom_env_file: bool = False,
    malformed_json: bool = False,
) -> subprocess.CompletedProcess[str]:
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env bash
set -eo pipefail
if [[ "$1" == "compose" && "$2" == "version" ]]; then
  exit 0
fi
if [[ "$*" == *"config --format json"* ]]; then
  printf '%s\n' "$FAKE_JSON"
  if [[ -n "$FAKE_LOG" ]]; then
    printf '%s' "$LINGSWAY_ENV_FILE" > "$FAKE_LOG"
  fi
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    inventory = tmp_path / "inventory.yml"
    inventory.write_text("host: localhost\n", encoding="utf-8")
    custom_file = tmp_path / "custom.env"
    custom_file.write_text('ACCOUNTING_PROVIDER="marzban"\n', encoding="utf-8")

    if malformed_json:
        payload = "not-json"
    else:
        environment: dict[str, str] = {}
        if effective_provider is not None:
            environment["ACCOUNTING_PROVIDER"] = effective_provider
        payload = json.dumps(
            {"services": {"backend-api": {"environment": environment}}}
        )

    env = os.environ.copy()
    env.pop("LINGSWAY_ENV_FILE", None)
    env.update(
        {
            "DEPLOY_ROOT": str(_REPO_ROOT),
            "DRY_RUN": "true",
            "INVENTORY_FILE": str(inventory),
            "PATH": str(tmp_path) + os.pathsep + env["PATH"],
            "FAKE_JSON": payload,
            "FAKE_LOG": "",
            "LINGSWAY_ENV_FILE": "",
        }
    )
    if custom_env_file:
        env["LINGSWAY_ENV_FILE"] = str(custom_file)
        env["FAKE_LOG"] = str(tmp_path / "compose-env-file.log")

    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; resolve_accounting_provider',
            "_",
            str(_STACK_SCRIPT),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("provider", ["mock", "marzban"])
def test_resolver_uses_effective_compose_backend_environment(
    tmp_path: Path, provider: str
) -> None:
    result = _run_resolver(tmp_path, provider)

    assert result.returncode == 0
    assert result.stdout.strip() == provider


def test_missing_provider_uses_backend_default_mock(tmp_path: Path) -> None:
    result = _run_resolver(tmp_path, None)

    assert result.returncode == 0
    assert result.stdout.strip() == "mock"


@pytest.mark.parametrize("provider", ["", "unsupported", "marzban "])
def test_ambiguous_provider_fails_closed(tmp_path: Path, provider: str) -> None:
    result = _run_resolver(tmp_path, provider)

    assert result.returncode != 0
    assert "unsupported" not in result.stdout
    assert "unsupported" not in result.stderr


def test_malformed_compose_config_fails_closed(tmp_path: Path) -> None:
    result = _run_resolver(tmp_path, "mock", malformed_json=True)

    assert result.returncode != 0
    assert "not-json" not in result.stdout
    assert "not-json" not in result.stderr


def test_custom_lingsway_env_file_is_passed_to_compose(tmp_path: Path) -> None:
    result = _run_resolver(tmp_path, "marzban", custom_env_file=True)

    assert result.returncode == 0
    assert result.stdout.strip() == "marzban"
    assert (tmp_path / "compose-env-file.log").read_text(encoding="utf-8") == str(
        tmp_path / "custom.env"
    )


def test_quoted_dotenv_value_is_consumed_as_compose_effective_value(tmp_path: Path) -> None:
    result = _run_resolver(tmp_path, "marzban", custom_env_file=True)

    assert result.returncode == 0
    assert result.stdout.strip() == "marzban"


def test_compose_defaults_remain_upstream_for_mock_path() -> None:
    prefix = "image: " + "$" + "{MARZBAN_IMAGE:"
    expected = "image: " + "$" + "{MARZBAN_IMAGE:-" + _UPSTREAM_IMAGE + "}"
    for path in (_TRANSPORT_COMPOSE, _PROBE_COMPOSE):
        image_lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith(prefix)
        ]
        assert image_lines == [expected]


def test_real_marzban_selects_patched_image_and_mock_does_not_override() -> None:
    script = _STACK_SCRIPT.read_text(encoding="utf-8")
    assert f"MARZBAN_IMAGE:-{_PATCHED_IMAGE}" in script
    assert 'case "$accounting_provider" in' in script
    assert "    marzban)" in script
    assert "    mock)" in script


def test_image_verification_precedes_compose_up() -> None:
    script = _STACK_SCRIPT.read_text(encoding="utf-8")
    main = script.split("main() {", 1)[1]
    assert main.index("verify_marzban_image_identity") < main.index("compose up --detach")
    verifier = script.split("verify_marzban_image_identity() {", 1)[1]
    assert verifier.index("verify_patched_image.py") < verifier.index("compose up --detach")

