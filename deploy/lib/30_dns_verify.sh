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
    log 'DRY_RUN: would resolve SITE_DOMAIN, API_DOMAIN, and SUBSCRIPTION_DOMAIN to TARGET_HOST'
    return 0
  fi
  require_cmd getent
  [[ -n "${TARGET_HOST:-}" ]] || die 'inventory target host is empty'
  local domain resolved
  for domain in "$SITE_DOMAIN" "$API_DOMAIN" "$SUBSCRIPTION_DOMAIN"; do
    [[ -n "$domain" ]] || die 'one of the three domain settings is empty'
    resolved="$(getent ahostsv4 "$domain" | awk '{ print $1 }' | sort -u)"
    printf '%s\n' "$resolved" | grep -Fxq "$TARGET_HOST" || \
      die "$domain does not resolve to target ${TARGET_HOST}"
  done
  log 'all three configured domains resolve to the target IPv4 address'
}

main "$@"
