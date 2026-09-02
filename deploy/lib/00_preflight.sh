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
  require_cmd awk
  require_cmd df
  require_cmd free
  require_cmd getent
  require_cmd ss

  if [[ "${REQUIRE_DEBIAN:-true}" == true ]]; then
    [[ -r /etc/os-release ]] || die '/etc/os-release is unavailable'
    # shellcheck disable=SC1091
    source /etc/os-release
    [[ "${ID:-}" == debian && "${VERSION_ID:-}" == 12 ]] || die 'Debian 12 is required'
  fi

  local disk_kb memory_kb min_disk_gb min_memory_mb
  disk_kb="$(df -Pk / | awk 'NR == 2 { print $4 }')"
  min_disk_gb="${MIN_DISK_GB:-20}"
  (( disk_kb >= min_disk_gb * 1024 * 1024 )) || die "at least ${min_disk_gb}GiB free disk is required"
  memory_kb="$(free -k | awk '/^Mem:/ { print $7 }')"
  min_memory_mb="${MIN_MEMORY_MB:-2048}"
  (( memory_kb >= min_memory_mb * 1024 )) || die "at least ${min_memory_mb}MiB available memory is required"

  local port
  for port in 80 443 8443; do
    if ss -ltnH | awk -v port=":$port" '$4 ~ port "$" { found = 1 } END { exit !found }'; then
      die "required deployment port is already occupied: $port"
    fi
  done
  log "preflight passed for target ${TARGET_HOST:-local}"
}

main "$@"
