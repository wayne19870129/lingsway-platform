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
    log 'DRY_RUN: would install encrypted backup cron and binlog guard timer'
    return 0
  fi
  [[ "$(id -u)" -eq 0 ]] || die '80_schedule.sh must run as root on the target host'
  require_cmd install
  require_cmd systemctl
  local cron_path service_path timer_path tmp
  cron_path="${BACKUP_CRON_PATH:-/etc/cron.d/lingsway-backup}"
  service_path="${BINLOG_GUARD_SERVICE_PATH:-/etc/systemd/system/lingsway-binlog-guard.service}"
  timer_path="${BINLOG_GUARD_TIMER_PATH:-/etc/systemd/system/lingsway-binlog-guard.timer}"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  printf '%s\n' \
    "17 3 * * * deploy ${BACKUP_SCRIPT:-$DEPLOY_ROOT/ops/backup/backup.sh} >>/var/log/lingsway-backup.log 2>&1" \
    > "$tmp/backup.cron"
  printf '%s\n' \
    '[Unit]' \
    'Description=Lingsway binlog guard' \
    '' \
    '[Service]' \
    'Type=oneshot' \
    "ExecStart=${BINLOG_GUARD_COMMAND:-/usr/local/sbin/lingsway-binlog-guard}" \
    > "$tmp/binlog.service"
  printf '%s\n' \
    '[Unit]' \
    'Description=Run Lingsway binlog guard' \
    '' \
    '[Timer]' \
    'OnCalendar=*:0/5' \
    'Persistent=true' \
    '' \
    '[Install]' \
    'WantedBy=timers.target' \
    > "$tmp/binlog.timer"
  install --owner root --group root --mode 0644 "$tmp/backup.cron" "$cron_path"
  install --owner root --group root --mode 0644 "$tmp/binlog.service" "$service_path"
  install --owner root --group root --mode 0644 "$tmp/binlog.timer" "$timer_path"
  systemctl daemon-reload
  systemctl enable --now lingsway-binlog-guard.timer
  log 'backup cron and binlog guard timer installed'
}

main "$@"
