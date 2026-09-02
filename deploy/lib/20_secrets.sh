#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

check_secret_file() {
  local path="$1"
  [[ -f "$path" ]] || die "required secret file is missing: $path"
  local mode owner group
  mode="$(stat -c '%a' "$path")"
  owner="$(stat -c '%U' "$path")"
  group="$(stat -c '%G' "$path")"
  # stat(1) prints this mode as "600", not the octal-looking "0600".
  [[ "$mode" == 600 && "$owner" == root && "$group" == root ]] || \
    die "secret file must be root:root mode 0600: $path"
}

require_name() {
  local name="$1" path="$2"
  grep -qE "^${name}=" "$path" || die "${name} is absent from $path"
}

main() {
  while (($# > 0)); do
    case "$1" in
      -i|--inventory) INVENTORY_FILE="$2"; shift 2 ;;
      *) die "unknown option: $1" ;;
    esac
  done
  load_inventory_defaults
  if is_true "$DRY_RUN"; then
    log 'DRY_RUN: would validate /opt/lingsway/.env and /etc/lingsway/*.conf without printing values'
    return 0
  fi
  local app_env backup_conf app_secrets
  app_env="${APP_ENV_FILE:-$DEPLOY_ROOT/.env}"
  backup_conf="${BACKUP_CONF_FILE:-/etc/lingsway/backup.conf}"
  app_secrets="${APP_SECRETS_FILE:-/etc/lingsway/app-secrets.conf}"
  check_secret_file "$app_env"
  check_secret_file "$backup_conf"
  check_secret_file "$app_secrets"
  require_name SECRET_ENCRYPTION_KEY "$app_env"
  require_name JWT_SECRET "$app_env"
  require_name DATABASE_URL "$app_env"
  require_name BACKUP_GPG_RECIPIENT "$backup_conf"
  log 'secret files passed ownership, mode, and required-name checks'
}

main "$@"
