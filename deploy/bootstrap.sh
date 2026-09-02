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
  # 80_schedule precedes 70_verify because the verifier intentionally checks
  # the backup schedule that must exist on a completed target.
  for step in 00_preflight 10_system 20_secrets 30_dns_verify 40_stack_up 50_migrate 60_seed 80_schedule 70_verify; do
    printf '[lingsway-deploy] running %s\n' "$step"
    bash "$SCRIPT_DIR/lib/$step.sh" --inventory "$INVENTORY_FILE"
  done
  printf '[lingsway-deploy] bootstrap completed\n'
}

main "$@"
