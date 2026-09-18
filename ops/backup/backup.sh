#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

BACKUP_DIR="${BACKUP_DIR:-/var/backups/lingsway}"
BACKUP_R2_PREFIX="${BACKUP_R2_PREFIX:-mysql}"
BACKUP_CONF_FILE="${BACKUP_CONF_FILE:-/etc/lingsway/backup.conf}"
STAGING_DIR=''
VERIFY_TEMP=''

cleanup() {
  [[ -z "$STAGING_DIR" ]] || rm -rf -- "$STAGING_DIR"
  [[ -z "$VERIFY_TEMP" ]] || rm -f -- "$VERIFY_TEMP" "${VERIFY_TEMP}.err"
}

trap cleanup EXIT INT TERM

fail() {
  printf '[lingsway-backup] ERROR: %s\n' "$1" >&2
  exit 1
}

usage() {
  printf 'Usage: %s [--verify]\n' "$0"
}

require_value() {
  local name="$1"
  [[ -n "${!name:-}" ]] || fail "required backup setting is missing: $name"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required backup command is unavailable: $1"
}

load_backup_config() {
  if [[ "${BACKUP_CONF_LOADED:-false}" == true ]]; then
    return 0
  fi
  [[ -r "$BACKUP_CONF_FILE" ]] || fail 'backup configuration is not readable'
  # The configuration is a root-owned deployment secret file.  It is sourced
  # only by the caller that has already passed the file-permission gate.
  set -a
  # shellcheck disable=SC1090
  source "$BACKUP_CONF_FILE"
  set +a
}

backup_prefix() {
  printf '%s' "${BACKUP_R2_PREFIX%/}"
}

r2_key() {
  local name="$1" prefix
  prefix="$(backup_prefix)"
  if [[ -n "$prefix" ]]; then
    printf '%s/%s' "$prefix" "$name"
  else
    printf '%s' "$name"
  fi
}

r2_upload() {
  local source="$1" key="$2" error_file="$3"
  if [[ -n "${R2_UPLOAD_COMMAND:-}" ]]; then
    bash -c "$R2_UPLOAD_COMMAND" -- "$source" "$key" >"$error_file" 2>&1 || \
      fail 'R2 upload failed'
    return 0
  fi
  require_command aws
  AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" \
    AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
    aws s3 cp "$source" "s3://${R2_BUCKET}/${key}" \
    --endpoint-url "$R2_ENDPOINT_URL" --only-show-errors >"$error_file" 2>&1 || \
    fail 'R2 upload failed'
}

r2_head() {
  local key="$1" error_file="$2"
  if [[ -n "${R2_HEAD_COMMAND:-}" ]]; then
    bash -c "$R2_HEAD_COMMAND" -- "$key" >"$error_file" 2>&1 || \
      fail 'R2 backup object is missing'
    return 0
  fi
  require_command aws
  AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" \
    AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
    aws s3api head-object --bucket "$R2_BUCKET" --key "$key" \
    --endpoint-url "$R2_ENDPOINT_URL" >"$error_file" 2>&1 || \
    fail 'R2 backup object is missing'
}

r2_download() {
  local key="$1" destination="$2" error_file="$3"
  if [[ -n "${R2_DOWNLOAD_COMMAND:-}" ]]; then
    bash -c "$R2_DOWNLOAD_COMMAND" -- "$key" "$destination" >"$error_file" 2>&1 || \
      fail 'R2 integrity evidence is unreadable'
    return 0
  fi
  require_command aws
  AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" \
    AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
    aws s3 cp "s3://${R2_BUCKET}/${key}" "$destination" \
    --endpoint-url "$R2_ENDPOINT_URL" --only-show-errors >"$error_file" 2>&1 || \
    fail 'R2 integrity evidence is unreadable'
}

read_local_digest() {
  local archive="$1" evidence="$2" expected recorded_name actual
  [[ -s "$evidence" ]] || fail 'local SHA-256 evidence is missing'
  read -r expected recorded_name < "$evidence" || fail 'local SHA-256 evidence is invalid'
  [[ "$expected" =~ ^[[:xdigit:]]{64}$ ]] || fail 'local SHA-256 evidence is invalid'
  [[ "$recorded_name" == "$(basename -- "$archive")" ]] || \
    fail 'local SHA-256 evidence names a different backup'
  actual="$(sha256sum -- "$archive" | awk '{print $1}')" || fail 'local SHA-256 calculation failed'
  [[ "$actual" == "$expected" ]] || fail 'local SHA-256 verification failed'
  printf '%s' "$expected"
}

latest_archive() {
  local candidate newest=''
  while IFS= read -r candidate; do
    [[ -n "$candidate" ]] || continue
    if [[ -z "$newest" || "$candidate" -nt "$newest" ]]; then
      newest="$candidate"
    fi
  done < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.sql.gpg' -print 2>/dev/null)
  [[ -n "$newest" ]] || fail 'no encrypted backup is available for verification'
  printf '%s' "$newest"
}

verify_backup() {
  local archive="${BACKUP_ARCHIVE:-}" evidence key remote_evidence remote_digest expected
  [[ -n "$archive" ]] || archive="$(latest_archive)"
  [[ -f "$archive" ]] || fail 'selected encrypted backup is missing'
  evidence="${archive}.sha256"
  key="$(r2_key "$(basename -- "$archive")")"
  require_value R2_ENDPOINT_URL
  require_value R2_ACCESS_KEY_ID
  require_value R2_SECRET_ACCESS_KEY
  require_value R2_BUCKET
  expected="$(read_local_digest "$archive" "$evidence")"
  VERIFY_TEMP="$(mktemp)"
  remote_evidence="$VERIFY_TEMP"
  r2_head "$key" "${remote_evidence}.err"
  r2_head "${key}.sha256" "${remote_evidence}.err"
  r2_download "${key}.sha256" "$remote_evidence" "${remote_evidence}.err"
  read -r remote_digest _ < "$remote_evidence" || fail 'remote SHA-256 evidence is invalid'
  [[ "$remote_digest" == "$expected" ]] || fail 'remote SHA-256 evidence does not match local evidence'
  VERIFY_TEMP=''
  printf '[lingsway-backup] verification passed: %s\n' "$(basename -- "$archive")"
}

create_backup() {
  local staging plain encrypted evidence digest timestamp basename archive key error_file
  require_value BACKUP_GPG_RECIPIENT
  require_value R2_ENDPOINT_URL
  require_value R2_ACCESS_KEY_ID
  require_value R2_SECRET_ACCESS_KEY
  require_value R2_BUCKET
  require_value MYSQL_DATABASE
  require_value MYSQL_USER
  mkdir -p -- "$BACKUP_DIR"
  staging="$(mktemp -d "$BACKUP_DIR/.staging.XXXXXX")"
  STAGING_DIR="$staging"
  plain="$staging/backup.sql"
  encrypted="$staging/backup.sql.gpg"
  evidence="$staging/backup.sql.gpg.sha256"
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  basename="mysql-${timestamp}-${BASHPID}-${RANDOM}.sql.gpg"
  archive="$BACKUP_DIR/$basename"
  error_file="$staging/command.err"

  if [[ -n "${MYSQLDUMP_COMMAND:-}" ]]; then
    bash -c "$MYSQLDUMP_COMMAND" >"$plain" 2>"$error_file" || fail 'MySQL dump failed'
  else
    require_command mysqldump
    MYSQL_PWD="${MYSQL_PASSWORD:-}" mysqldump \
      --host="${MYSQL_HOST:-mysql}" --port="${MYSQL_PORT:-3306}" \
      --user="$MYSQL_USER" --single-transaction --routines --events --triggers \
      --hex-blob --databases "$MYSQL_DATABASE" >"$plain" 2>"$error_file" || \
      fail 'MySQL dump failed'
  fi
  [[ -s "$plain" ]] || fail 'MySQL dump produced no data'

  if [[ -n "${GPG_ENCRYPT_COMMAND:-}" ]]; then
    bash -c "$GPG_ENCRYPT_COMMAND" -- "$plain" "$encrypted" >"$error_file" 2>&1 || \
      fail 'GPG encryption failed'
  else
    require_command gpg
    gpg --batch --no-tty --trust-model always --recipient "$BACKUP_GPG_RECIPIENT" \
      --output "$encrypted" --encrypt "$plain" >"$error_file" 2>&1 || \
      fail 'GPG encryption failed'
  fi
  [[ -s "$encrypted" ]] || fail 'GPG encryption produced no backup'

  digest="$(sha256sum -- "$encrypted" | awk '{print $1}')" || fail 'SHA-256 calculation failed'
  printf '%s  %s\n' "$digest" "$basename" > "$evidence"
  key="$(r2_key "$basename")"
  r2_upload "$encrypted" "$key" "$error_file"
  r2_upload "$evidence" "${key}.sha256" "$error_file"

  [[ ! -e "$archive" && ! -e "${archive}.sha256" ]] || \
    fail 'backup filename collision refused'
  mv -- "$encrypted" "$archive" || fail 'local encrypted backup installation failed'
  mv -- "$evidence" "${archive}.sha256" || fail 'local SHA-256 evidence installation failed'
  rm -f -- "$plain"
  rm -rf -- "$staging"
  staging=''
  STAGING_DIR=''
  printf '[lingsway-backup] backup completed: %s\n' "$archive"
}

main() {
  local verify=false
  while (($# > 0)); do
    case "$1" in
      --verify) verify=true; shift ;;
      -h|--help) usage; return 0 ;;
      *) usage >&2; return 2 ;;
    esac
  done
  load_backup_config
  if [[ "$verify" == true ]]; then
    verify_backup
  else
    create_backup
  fi
}

main "$@"
