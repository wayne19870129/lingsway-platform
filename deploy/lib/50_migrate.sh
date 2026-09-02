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
  # Compose reads this file for the MySQL healthcheck; the shell also needs
  # the root password for the readiness probe without ever logging its value.
  app_env="${APP_ENV_FILE:-$DEPLOY_ROOT/.env}"
  if [[ -r "$app_env" ]]; then
    # shellcheck disable=SC1090
    set -a; source "$app_env"; set +a
  fi
  local services
  services="$(compose config --services)"
  grep -Fxq backend-api <<< "$services" || die 'migration refused: backend-api service is absent'
  local marker="${PRE_MIGRATION_BACKUP_MARKER:-/run/lingsway/pre-migration-backup.ok}"
  if [[ ! -f "$marker" ]]; then
    [[ -n "${PRE_MIGRATION_BACKUP_COMMAND:-}" ]] || \
      die 'migration refused: PRE_MIGRATION_BACKUP_COMMAND or a valid marker is required'
    bash -c "$PRE_MIGRATION_BACKUP_COMMAND"
    install --directory "$(dirname "$marker")"
    touch "$marker"
  fi
  # Migration is deliberately isolated from the full stack: backend-api
  # depends on Marzban, while Marzban must not start before the rendered Xray
  # file exists. Start only MySQL, then run Alembic in a disposable backend
  # container with its dependencies already built.
  compose up --detach mysql
  local mysql_password ready attempt
  mysql_password="${MYSQL_ROOT_PASSWORD:-}"
  [[ -n "$mysql_password" ]] || die 'migration refused: MYSQL_ROOT_PASSWORD is not configured'
  ready=false
  for attempt in $(seq 1 60); do
    if compose exec --no-TTY mysql mysqladmin ping -h 127.0.0.1 -u root \
      -p"$mysql_password" --silent >/dev/null 2>&1; then
      ready=true
      break
    fi
    sleep 2
  done
  [[ "$ready" == true ]] || die 'migration refused: MySQL did not become ready'
  # Compose v1 has no `run --build` option and may print usage with a zero
  # status. Build explicitly so both Compose generations fail closed, then
  # run the disposable migration container without the unsupported option.
  compose build backend-api
  compose run --rm --no-deps backend-api alembic upgrade head
  log 'alembic upgrade head completed after backup gate'
}

main "$@"
