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
    log 'DRY_RUN: would validate and start the selected Compose profiles'
    return 0
  fi
  require_cmd docker
  compose config --quiet
  compose up --detach
  if is_true "${RESTART_TRANSPORT:-false}"; then
    compose up --detach marzban mihomo
  fi
  log 'Compose stack started with the configured profiles'
}

main "$@"
