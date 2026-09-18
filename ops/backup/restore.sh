#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

BACKUP_CONF_FILE="${BACKUP_CONF_FILE:-/etc/lingsway/backup.conf}"
STAGING_DIR=''

cleanup() {
  [[ -z "$STAGING_DIR" ]] || rm -rf -- "$STAGING_DIR"
}

trap cleanup EXIT INT TERM

fail() {
  printf '[lingsway-restore] ERROR: %s\n' "$1" >&2
  exit 1
}

usage() {
  printf 'Usage: %s --archive FILE --target-database NAME --confirm-empty-target\n' "$0"
}

load_backup_config() {
  if [[ "${BACKUP_CONF_LOADED:-false}" == true ]]; then
    return 0
  fi
  [[ -r "$BACKUP_CONF_FILE" ]] || fail 'backup configuration is not readable'
  set -a
  # shellcheck disable=SC1090
  source "$BACKUP_CONF_FILE"
  set +a
}

verify_digest() {
  local archive="$1" evidence="$2" expected recorded_name actual
  [[ -s "$evidence" ]] || fail 'SHA-256 evidence is missing'
  read -r expected recorded_name < "$evidence" || fail 'SHA-256 evidence is invalid'
  [[ "$expected" =~ ^[[:xdigit:]]{64}$ ]] || fail 'SHA-256 evidence is invalid'
  [[ "$recorded_name" == "$(basename -- "$archive")" ]] || fail 'SHA-256 evidence names a different backup'
  actual="$(sha256sum -- "$archive" | awk '{print $1}')" || fail 'SHA-256 calculation failed'
  [[ "$actual" == "$expected" ]] || fail 'SHA-256 verification failed; restore refused'
}

target_is_empty() {
  local target="$1" count
  if [[ -n "${MYSQL_TABLE_COUNT_COMMAND:-}" ]]; then
    count="$(bash -c "$MYSQL_TABLE_COUNT_COMMAND" -- "$target")" || fail 'target database emptiness check failed'
  else
    MYSQL_PWD="${MYSQL_PASSWORD:-}" mysql \
      --host="${MYSQL_HOST:-mysql}" --port="${MYSQL_PORT:-3306}" \
      --user="${MYSQL_USER}" --batch --skip-column-names --raw \
      --database="$target" \
      --execute='SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE();' \
      2>/dev/null || fail 'target database emptiness check failed'
  fi
  count="$(printf '%s' "$count" | tr -d '[:space:]')"
  [[ "$count" =~ ^[0-9]+$ && "$count" == 0 ]] || fail 'target database is not empty; restore refused'
}

restore_archive() {
  local archive="$1" target="$2" staging plain evidence error_file
  [[ -f "$archive" ]] || fail 'encrypted archive does not exist'
  [[ "$target" =~ ^[A-Za-z0-9_]+$ ]] || fail 'target database name is invalid'
  [[ "${CONFIRM_EMPTY_TARGET:-false}" == true ]] || fail 'explicit empty-target confirmation is required'
  [[ -n "${MYSQL_USER:-}" ]] || fail 'MySQL user is not configured'
  evidence="${archive}.sha256"
  verify_digest "$archive" "$evidence"
  target_is_empty "$target"
  staging="$(mktemp -d)"
  STAGING_DIR="$staging"
  plain="$staging/restore.sql"
  error_file="$staging/command.err"
  if [[ -n "${GPG_DECRYPT_COMMAND:-}" ]]; then
    bash -c "$GPG_DECRYPT_COMMAND" -- "$archive" "$plain" >"$error_file" 2>&1 || \
      fail 'GPG decryption failed; restore refused'
  else
    command -v gpg >/dev/null 2>&1 || fail 'required restore command is unavailable: gpg'
    gpg --batch --no-tty --output "$plain" --decrypt "$archive" >"$error_file" 2>&1 || \
      fail 'GPG decryption failed; restore refused'
  fi
  [[ -s "$plain" ]] || fail 'GPG decryption produced no SQL; restore refused'
  if [[ -n "${MYSQL_RESTORE_COMMAND:-}" ]]; then
    bash -c "$MYSQL_RESTORE_COMMAND" -- "$plain" "$target" >"$error_file" 2>&1 || \
      fail 'database restore failed'
  else
    command -v mysql >/dev/null 2>&1 || fail 'required restore command is unavailable: mysql'
    MYSQL_PWD="${MYSQL_PASSWORD:-}" mysql \
      --host="${MYSQL_HOST:-mysql}" --port="${MYSQL_PORT:-3306}" \
      --user="$MYSQL_USER" --database="$target" < "$plain" >"$error_file" 2>&1 || \
      fail 'database restore failed'
  fi
  rm -rf -- "$staging"
  staging=''
  STAGING_DIR=''
  printf '[lingsway-restore] restore completed for target database: %s\n' "$target"
}

main() {
  local archive='' target='' confirm=false
  while (($# > 0)); do
    case "$1" in
      --archive) (($# >= 2)) || { usage >&2; return 2; }; archive="$2"; shift 2 ;;
      --target-database) (($# >= 2)) || { usage >&2; return 2; }; target="$2"; shift 2 ;;
      --confirm-empty-target) confirm=true; shift ;;
      -h|--help) usage; return 0 ;;
      *)
        [[ -z "$archive" ]] || { usage >&2; return 2; }
        archive="$1"
        shift
        ;;
    esac
  done
  [[ -n "$archive" && -n "$target" && "$confirm" == true ]] || { usage >&2; return 2; }
  export CONFIRM_EMPTY_TARGET=true
  load_backup_config
  restore_archive "$archive" "$target"
}

main "$@"
