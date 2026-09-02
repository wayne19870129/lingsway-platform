#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  printf 'Usage: %s -i inventory.yml [--dry-run]\n' "$0"
}

main() {
  local inventory='' 
  while (($# > 0)); do
    case "$1" in
      -i|--inventory) inventory="$2"; shift 2 ;;
      --dry-run) DRY_RUN=true; shift ;;
      -h|--help) usage; return 0 ;;
      *) usage >&2; exit 2 ;;
    esac
  done
  [[ -n "$inventory" ]] || { usage >&2; exit 2; }
  [[ -f "$inventory" ]] || { printf 'inventory file does not exist: %s\n' "$inventory" >&2; exit 1; }
  export INVENTORY_FILE="$inventory"
  export DRY_RUN="${DRY_RUN:-false}"
  export DEPLOY_ROOT="${DEPLOY_ROOT:-/opt/lingsway}"

  local step
  # Stage-two finding (2026-09-02): the supplied temporary host had 17GiB free
  # and 679MiB available, below the conservative 20GiB/2GiB preflight defaults.
  # Keep this fail-closed; provision a larger host or explicitly review the
  # threshold overrides instead of silently weakening the guard.
  # Stage-two finding (2026-09-02): 70_verify must report every check even when
  # a prerequisite such as Docker is absent; its checks therefore return 1
  # instead of calling the terminating common require_cmd helper.
  # Stage-two finding (2026-09-02): Debian 12's configured repositories exposed
  # docker-compose (v1), not docker-compose-v2; 10_system and common compose
  # support both package layouts without weakening the deployment checks.
  # Stage-two finding (2026-09-02): docker-compose v1 interprets Compose v2's
  # top-level name as a service. The project name is now supplied with -p and
  # the base file stays valid for both Compose generations.
  # Stage-two finding (2026-09-02): Compose v1 also needs an explicit version
  # before it recognizes an empty services map as the top-level services key.
  # Stage-two finding (2026-09-02): stat -c '%a' reports a 0600 file as "600";
  # 20_secrets compares that exact representation so valid secrets are not
  # rejected by a formatting mismatch.
  # Stage-two finding (2026-09-02): an empty Compose skeleton made exec/up
  # appear successful under some Compose versions; stack and verifier steps
  # now require the expected services before claiming success.
  # 80_schedule precedes 70_verify because the verifier intentionally checks
  # the backup schedule that must exist on a completed target.
  # Stage-two finding (2026-09-02): a fresh bind-mounted SQLite/Xray path that
  # does not exist is created by Docker as a directory, not a file. The target
  # must create these runtime files before stack-up; the base Xray skeleton is
  # not a runnable production configuration until database rendering supplies
  # outbounds and routing.
  # Stage-two finding (2026-09-02): the backend image previously copied only
  # source files, so a container could build while missing FastAPI/SQLAlchemy
  # and other pyproject dependencies at runtime. The Dockerfile now installs
  # the project package during image build.
  # Stage-two finding (2026-09-02): docker-compose exec may return success for
  # a declared service with no running container under Compose v1. The verifier
  # now checks the backend-api container explicitly before Alembic/Xray tests.
  # Stage-two audit (2026-09-02): the verifier also validates every Compose
  # service container by label, keeps optional profile services excluded unless
  # enabled, checks command-substitution exit statuses, and separates UFW
  # active-state validation from its default-policy validation. These guards
  # prevent missing commands, files, or containers from becoming PASS values.
  # Stage-two finding (2026-09-02): the checked-in Xray file is a template,
  # not a runtime config. Marzban rejects it without rendered outbounds, so
  # stack-up now refuses missing/file-typed-but-unrendered runtime config and
  # never adds an arbitrary outbound merely to make the container start.
  for step in 00_preflight 10_system 20_secrets 30_dns_verify 40_stack_up 50_migrate 60_seed 80_schedule 70_verify; do
    printf '[lingsway-deploy] running %s\n' "$step"
    bash "$SCRIPT_DIR/lib/$step.sh" --inventory "$INVENTORY_FILE"
  done
  printf '[lingsway-deploy] bootstrap completed\n'
}

main "$@"
