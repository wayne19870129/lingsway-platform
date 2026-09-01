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
    log 'DRY_RUN: would require a successful encrypted pre-migration backup, then run alembic upgrade head'
    return 0
  fi
  require_cmd docker
  local marker="${PRE_MIGRATION_BACKUP_MARKER:-/run/lingsway/pre-migration-backup.ok}"
  if [[ ! -f "$marker" ]]; then
    [[ -n "${PRE_MIGRATION_BACKUP_COMMAND:-}" ]] || \
      die 'migration refused: PRE_MIGRATION_BACKUP_COMMAND or a valid marker is required'
    bash -c "$PRE_MIGRATION_BACKUP_COMMAND"
    install --directory "$(dirname "$marker")"
    touch "$marker"
  fi
  compose exec --no-TTY backend-api alembic upgrade head
  log 'alembic upgrade head completed after backup gate'
}

main "$@"
