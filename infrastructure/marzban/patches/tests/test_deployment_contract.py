"""Deployment contract tests for TASK-T16 Phase 2C2.

`resolve_accounting_provider()` (deploy/lib/40_stack_up.sh) is tested
through the real shell function with fake `docker`/`docker-compose`
executables standing in for both Docker Compose generations this
repository supports (see deploy/lib/common.sh's `compose()`: Compose v2
via `docker compose`, falling back to the standalone `docker-compose` v1
binary when the v2 plugin is unavailable). The fakes intercept `compose
run --rm --no-deps backend-api ...` and print a canned value simulating
`get_settings().accounting_provider`; they never contact Docker, Marzban,
or a registry, and no real backend-api image is built or run.
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

# `docker compose ...` (Compose v2 plugin). `compose version` reports
# availability per FAKE_COMPOSE_V2_AVAILABLE; any invocation containing a
# bare `run` argument is treated as the resolver's `compose run --rm
# --no-deps backend-api ...` call and answered from FAKE_RUN_OUTPUT/
# FAKE_RUN_FAIL, the same contract the v1 fake below implements directly.
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
      printf '%s\\n' "$FAKE_RUN_OUTPUT"
      exit 0
    fi
  done
fi
exit 0
"""

# `docker-compose ...` (Compose v1 compatibility binary) -- common.sh only
# falls back to this when `docker compose version` fails. No `compose`
# prefix or `version` preflight here; args go straight to `run`/etc.
_FAKE_DOCKER_COMPOSE_SCRIPT = """#!/usr/bin/env bash
set -eo pipefail
for arg in "$@"; do
  if [[ "$arg" == "run" ]]; then
    if [[ -n "${FAKE_LOG:-}" ]]; then
      printf '%s' "$LINGSWAY_ENV_FILE" > "$FAKE_LOG"
    fi
    [[ "${FAKE_RUN_FAIL:-0}" == "1" ]] && exit 7
    printf '%s\\n' "$FAKE_RUN_OUTPUT"
    exit 0
  fi
done
exit 0
"""


def _run_resolver(
    tmp_path: Path,
    run_output: str | None,
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

    inventory = tmp_path / "inventory.yml"
    inventory.write_text("host: localhost\n", encoding="utf-8")
    custom_file = tmp_path / "custom.env"
    custom_file.write_text('ACCOUNTING_PROVIDER="marzban"\n', encoding="utf-8")

    env = os.environ.copy()
    env.pop("LINGSWAY_ENV_FILE", None)
    env.update(
        {
            "DEPLOY_ROOT": str(_REPO_ROOT),
            "DRY_RUN": "true",
            "INVENTORY_FILE": str(inventory),
            "PATH": str(tmp_path) + os.pathsep + env["PATH"],
            "FAKE_COMPOSE_V2_AVAILABLE": "1" if compose_v2_available else "0",
            "FAKE_RUN_OUTPUT": "" if run_output is None else run_output,
            "FAKE_RUN_FAIL": "1" if run_should_fail else "0",
            "FAKE_LOG": str(tmp_path / "compose-env-file.log") if log_env_file else "",
            "LINGSWAY_ENV_FILE": str(custom_file) if custom_env_file else "",
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
def test_resolver_returns_effective_backend_provider(tmp_path: Path, provider: str) -> None:
    result = _run_resolver(tmp_path, provider)

    assert result.returncode == 0
    assert result.stdout.strip() == provider


@pytest.mark.parametrize("provider", ["mock", "marzban"])
def test_docker_compose_v1_fallback_used_when_v2_unavailable(
    tmp_path: Path, provider: str
) -> None:
    """Regression test: common.sh's compose() falls back to the standalone
    `docker-compose` v1 binary when `docker compose version` fails (the
    supported path for hosts without the Compose v2 plugin -- see
    deploy/bootstrap.sh's Debian 12 stage-two support). The resolver must
    not hard-depend on a Compose v2-only feature (e.g. `config --format
    json`, which v1 doesn't support at all) or it breaks deployment on a
    v1-only host even for the default ACCOUNTING_PROVIDER=mock path."""
    result = _run_resolver(tmp_path, provider, compose_v2_available=False)

    assert result.returncode == 0
    assert result.stdout.strip() == provider


@pytest.mark.parametrize("provider", ["", "bogus", "marzban "])
def test_ambiguous_provider_fails_closed(tmp_path: Path, provider: str) -> None:
    result = _run_resolver(tmp_path, provider)

    assert result.returncode != 0
    assert "bogus" not in result.stdout
    assert "bogus" not in result.stderr


def test_compose_run_failure_fails_closed(tmp_path: Path) -> None:
    result = _run_resolver(tmp_path, "mock", run_should_fail=True)

    assert result.returncode != 0


def test_custom_lingsway_env_file_reaches_compose_subprocess(tmp_path: Path) -> None:
    """The resolver itself never reads LINGSWAY_ENV_FILE -- it only needs
    to let Compose's own env_file resolution see it unchanged, since
    `compose run` is what actually resolves backend-api's environment
    for either Compose generation."""
    result = _run_resolver(tmp_path, "marzban", custom_env_file=True, log_env_file=True)

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


def test_stack_script_is_sourceable_without_running_main(tmp_path: Path) -> None:
    """Regression test: 40_stack_up.sh must guard its trailing `main "$@"`
    call so sourcing it (as every test above does, to reach
    resolve_accounting_provider()/verify_marzban_image_identity() in
    isolation) does not also execute the real deployment flow with the
    sourcing shell's own argv."""
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; echo SOURCED_OK', "_", str(_STACK_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "SOURCED_OK" in result.stdout
