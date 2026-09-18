#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

fail() {
  printf '[lingsway-backup-cron] ERROR: %s\n' "$1" >&2
  exit 1
}

main() {
  [[ "$(id -u)" -eq 0 ]] || fail 'backup cron installer must run as root'
  command -v install >/dev/null 2>&1 || fail 'required command is unavailable: install'
  command -v runuser >/dev/null 2>&1 || fail 'required command is unavailable: runuser'
  command -v stat >/dev/null 2>&1 || fail 'required command is unavailable: stat'
  local script="${BACKUP_SCRIPT:-/opt/lingsway/ops/backup/backup.sh}"
  local run_as="${BACKUP_RUN_AS:-deploy}"
  local wrapper="${BACKUP_WRAPPER_PATH:-/usr/local/sbin/lingsway-backup}"
  local cron_path="${BACKUP_CRON_PATH:-/etc/cron.d/lingsway-backup}"
  local backup_conf="${BACKUP_CONF_FILE:-/etc/lingsway/backup.conf}"
  local app_env="${BACKUP_APP_ENV_FILE:-/opt/lingsway/.env}"
  local log_path="${BACKUP_LOG_PATH:-/var/log/lingsway-backup.log}"
  local tmp wrapper_tmp cron_tmp
  [[ -x "$script" ]] || fail 'backup script is missing or not executable'
  id "$run_as" >/dev/null 2>&1 || fail 'backup execution user is missing'
  [[ -r "$backup_conf" ]] || fail 'backup configuration is not readable by root'
  [[ "$(stat -c '%a' "$backup_conf")" == 600 ]] || fail 'backup configuration must remain mode 0600'
  [[ "$(stat -c '%U' "$backup_conf")" == root ]] || fail 'backup configuration must remain root-owned'
  [[ "$(stat -c '%G' "$backup_conf")" == root ]] || fail 'backup configuration group must remain root'
  tmp="$(mktemp -d)"
  trap 'rm -rf -- "${tmp:-}"' EXIT
  wrapper_tmp="$tmp/wrapper"
  cron_tmp="$tmp/cron"
  printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -Eeuo pipefail' \
    'umask 077' \
    "backup_conf=$(printf '%q' "$backup_conf")" \
    "app_env=$(printf '%q' "$app_env")" \
    "backup_script=$(printf '%q' "$script")" \
    '[[ -r "$backup_conf" ]] || exit 1' \
    'set -a' \
    '[[ -r "$app_env" ]] && source "$app_env"' \
    'source "$backup_conf"' \
    'set +a' \
    'export BACKUP_CONF_LOADED=true' \
    "exec runuser --preserve-environment --user $(printf '%q' "$run_as") -- \"\$backup_script\"" \
    > "$wrapper_tmp"
  printf '%s\n' "17 3 * * * root $wrapper >>$log_path 2>&1" > "$cron_tmp"
  install --directory --owner root --group root --mode 0755 "$(dirname -- "$wrapper")"
  install --directory --owner root --group root --mode 0755 "$(dirname -- "$cron_path")"
  install --owner root --group root --mode 0700 "$wrapper_tmp" "$wrapper"
  install --owner root --group root --mode 0644 "$cron_tmp" "$cron_path"
  printf '[lingsway-backup-cron] installed root reader wrapper and cron schedule\n'
}

main "$@"
