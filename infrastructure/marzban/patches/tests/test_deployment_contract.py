"""Deployment contract tests for TASK-T16 Phase 2C2.

The resolver is exercised through the real shell function with fake
``docker``/``docker-compose`` executables. The fakes model the supported
Compose v2 and v1 paths and only return a canned provider sentinel; they
never contact Docker, Marzban, a registry, or a backend image.
"""

from __future__ import annotations

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
_SENTINEL_PREFIX = "__LINGSWAY_ACCOUNTING_PROVIDER__="

_FAKE_DOCKER_SCRIPT = """#!/usr/bin/env bash
set -eo pipefail
if [[ "$1" == "compose" && "$2" == "version" ]]; then
  [[ "${FAKE_COMPOSE_V2_AVAILABLE:-1}" == "1" ]] && exit 0 || exit 1
fi
if [[ "$1" == "compose" ]]; then
  shift
  for arg in "$@"; do
    if [[ "$arg" == "run" ]]; then
      if [[ -n "${FAKE_LOG:-}" ]]; then
        printf '%s' "$LINGSWAY_ENV_FILE" > "$FAKE_LOG"
      fi
      [[ "${FAKE_RUN_FAIL:-0}" == "1" ]] && exit 7
      printf '%s' "$FAKE_RUN_OUTPUT"
      exit 0
    fi
  done
fi
exit 0
"""

_FAKE_DOCKER_COMPOSE_SCRIPT = """#!/usr/bin/env bash
set -eo pipefail
for arg in "$@"; do
  if [[ "$arg" == "run" ]]; then
    if [[ -n "${FAKE_LOG:-}" ]]; then
      printf '%s' "$LINGSWAY_ENV_FILE" > "$FAKE_LOG"
    fi
    [[ "${FAKE_RUN_FAIL:-0}" == "1" ]] && exit 7
    printf '%s' "$FAKE_RUN_OUTPUT"
    exit 0
  fi
done
exit 0
"""


def _run_resolver(
    tmp_path: Path,
    run_output: str,
    *,
    compose_v2_available: bool = True,
    run_should_fail: bool = False,
    custom_env_file: bool = False,
    log_env_file: bool = False,
) -> subprocess.CompletedProcess[str]:
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(_FAKE_DOCKER_SCRIPT, encoding="utf-8")
    fake_docker.chmod(0o755)

    fake_docker_compose = tmp_path / "docker-compose"
    fake_docker_compose.write_text(_FAKE_DOCKER_COMPOSE_SCRIPT, encoding="utf-8")
    fake_docker_compose.chmod(0o755)

    custom_file = tmp_path / "custom.env"
    custom_file.write_text('ACCOUNTING_PROVIDER="marzban"\n', encoding="utf-8")

    env = os.environ.copy()
    env.pop("LINGSWAY_ENV_FILE", None)
    env.update(
        {
            "DEPLOY_ROOT": str(_REPO_ROOT),
            "FAKE_COMPOSE_V2_AVAILABLE": "1" if compose_v2_available else "0",
            "FAKE_RUN_OUTPUT": run_output,
            "FAKE_RUN_FAIL": "1" if run_should_fail else "0",
            "FAKE_LOG": str(tmp_path / "compose-env-file.log") if log_env_file else "",
            "LINGSWAY_ENV_FILE": str(custom_file) if custom_env_file else "",
            "PATH": str(tmp_path) + os.pathsep + env["PATH"],
        }
    )
    return subprocess.run(
        ["bash", "-c", 'source "$1"; resolve_accounting_provider', "_", str(_STACK_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("provider", ["mock", "marzban"])
def test_resolver_returns_single_effective_backend_sentinel(
    tmp_path: Path, provider: str
) -> None:
    result = _run_resolver(tmp_path, _SENTINEL_PREFIX + provider)

    assert result.returncode == 0
    assert result.stdout.strip() == provider


@pytest.mark.parametrize("provider", ["mock", "marzban"])
def test_docker_compose_v1_fallback_used_when_v2_unavailable(
    tmp_path: Path, provider: str
) -> None:
    result = _run_resolver(
        tmp_path, _SENTINEL_PREFIX + provider, compose_v2_available=False
    )

    assert result.returncode == 0
    assert result.stdout.strip() == provider


@pytest.mark.parametrize(
    "run_output",
    [
        "",
        _SENTINEL_PREFIX + "mock\n" + _SENTINEL_PREFIX + "marzban",
        _SENTINEL_PREFIX + "bogus",
        _SENTINEL_PREFIX + "marzban ",
        "compose noise\n" + _SENTINEL_PREFIX + "mock",
        "not-a-sentinel",
    ],
)
def test_zero_multiple_unsupported_or_noisy_sentinel_fails_closed(
    tmp_path: Path, run_output: str
) -> None:
    result = _run_resolver(tmp_path, run_output)

    assert result.returncode != 0
    assert _SENTINEL_PREFIX + "bogus" not in result.stdout
    assert "not-a-sentinel" not in result.stdout


def test_compose_run_failure_fails_closed(tmp_path: Path) -> None:
    result = _run_resolver(tmp_path, _SENTINEL_PREFIX + "mock", run_should_fail=True)

    assert result.returncode != 0


def test_custom_quoted_lingsway_env_file_reaches_compose_subprocess(
    tmp_path: Path,
) -> None:
    result = _run_resolver(
        tmp_path,
        _SENTINEL_PREFIX + "marzban",
        custom_env_file=True,
        log_env_file=True,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "marzban"
    assert (tmp_path / "compose-env-file.log").read_text(encoding="utf-8") == str(
        tmp_path / "custom.env"
    )


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


def test_probe_uses_settings_from_env_and_exact_sentinel_contract() -> None:
    script = _STACK_SCRIPT.read_text(encoding="utf-8")
    resolver = script.split("resolve_accounting_provider() {", 1)[1].split(
        "verify_marzban_image_identity() {", 1
    )[0]
    assert "from backend.app.core.config import Settings" in script
    assert "Settings.from_env().accounting_provider" in script
    assert _SENTINEL_PREFIX + "mock" in script
    assert _SENTINEL_PREFIX + "marzban" in script
    assert "tail -n 1" not in resolver
    assert "config --format json" not in resolver
    assert "compose run --rm --no-deps backend-api" in resolver


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


def test_stack_script_is_sourceable_without_running_main() -> None:
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; echo SOURCED_OK', "_", str(_STACK_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "SOURCED_OK" in result.stdout

