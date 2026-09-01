#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
  printf 'Usage: %s --release-dir /opt/lingsway/releases/<release> [--root /opt/lingsway]\n' "$0"
}

main() {
  local root="$DEPLOY_ROOT" release_dir=''
  while (($# > 0)); do
    case "$1" in
      --root) root="$2"; shift 2 ;;
      --release-dir) release_dir="$2"; shift 2 ;;
      -h|--help) usage; return 0 ;;
      *) usage >&2; die "unknown option: $1" ;;
    esac
  done
  [[ -n "$release_dir" ]] || { usage >&2; die '--release-dir is required'; }
  [[ "$root" = /* && "$release_dir" = /* ]] || die 'rollback paths must be absolute'
  [[ "$release_dir" == "$root/releases/"* ]] || die 'release must be below the deployment releases directory'
  [[ -d "$release_dir" ]] || die "release directory does not exist: $release_dir"
  local current_link="$root/current"
  if [[ -e "$current_link" || -L "$current_link" ]]; then
    local previous
    previous="$(readlink "$current_link")"
    log "current release before rollback: $previous"
  fi
  ln -sfn "$release_dir" "$current_link"
  log "current release now points to $release_dir"
  log 'transport services were not restarted by rollback; use the approved provider protection path if required'
}

main "$@"
