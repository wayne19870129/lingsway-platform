#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

main() {
  while (($# > 0)); do
    case "$1" in
      -i|--inventory) INVENTORY_FILE="$2"; shift 2 ;;
      *) die "unknown option: $1" ;;
    esac
  done
  load_inventory_defaults
  if is_true "$DRY_RUN"; then
    log 'DRY_RUN: would seed the administrator, four plans, and the configured egress pool'
    return 0
  fi
  [[ -n "${SEED_COMMAND:-}" ]] || die 'SEED_COMMAND must be explicitly configured; no implicit seed data is accepted'
  bash -c "$SEED_COMMAND"
  log 'initial seed command completed'
}

main "$@"
