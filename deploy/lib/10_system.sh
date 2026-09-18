#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

SUDOERS_TMP=''

cleanup_sudoers_tmp() {
  if [[ -n "${SUDOERS_TMP:-}" ]]; then
    rm -f -- "$SUDOERS_TMP"
  fi
}

ssh_authentication_preflight() {
  local user home authorized_keys source_ip auth_log
  user="${SUDO_USER:-${USER:-root}}"
  [[ "$user" != root ]] || home=/root
  if [[ "$user" != root ]]; then
    home="$(getent passwd "$user" | cut -d: -f6)"
  fi
  authorized_keys="$home/.ssh/authorized_keys"
  [[ -s "$authorized_keys" ]] || die "public-key preflight failed: no authorized_keys for $user"
  ssh-keygen -lf "$authorized_keys" >/dev/null 2>&1 || \
    die "public-key preflight failed: authorized_keys is not parseable for $user"
  [[ -n "${SSH_CONNECTION:-}" ]] || \
    die 'public-key preflight failed: current session is not an SSH session'
  source_ip="${SSH_CONNECTION%% *}"
  auth_log="$(journalctl -u ssh -u sshd --since '10 minutes ago' --no-pager 2>/dev/null || true)"
  if [[ -z "$auth_log" && -r /var/log/auth.log ]]; then
    auth_log="$(tail -n 200 /var/log/auth.log)"
  fi
  printf '%s\n' "$auth_log" | awk -v user="$user" -v source_ip="$source_ip" '
    /Accepted publickey/ && index($0, "for " user " from " source_ip " ") { found = 1 }
    END { exit !found }
  ' || die "public-key preflight failed: current SSH session was not accepted with a public key"
}

restore_sshd_config() {
  local backup="$1"
  cp -a -- "$backup" /etc/ssh/sshd_config
  systemctl reload ssh
}

harden_ssh() {
  if ! is_true "${HARDEN_SSH:-true}"; then
    log 'harden_ssh=false; SSH hardening skipped by explicit inventory setting'
    return 0
  fi
  require_cmd sshd
  require_cmd ssh-keygen
  require_cmd journalctl
  require_cmd systemctl
  ssh_authentication_preflight

  local backup timestamp tmp effective
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup="/etc/ssh/sshd_config.lingsway.${timestamp}.bak"
  install -m 600 /etc/ssh/sshd_config "$backup"
  tmp="$(mktemp /etc/ssh/sshd_config.lingsway.XXXXXX)"
  awk '
    BEGIN {
      print "PermitRootLogin prohibit-password"
      print "PasswordAuthentication no"
    }
    /^[[:space:]]*#/ { print; next }
    /^[[:space:]]*PermitRootLogin([[:space:]]|$)/ { next }
    /^[[:space:]]*PasswordAuthentication([[:space:]]|$)/ { next }
    { print }
  ' /etc/ssh/sshd_config > "$tmp"
  chmod 600 "$tmp"
  if ! sshd -t -f "$tmp"; then
    rm -f -- "$tmp"
    die "sshd configuration validation failed; original retained at $backup"
  fi
  install -m 600 "$tmp" /etc/ssh/sshd_config
  rm -f -- "$tmp"
  if ! systemctl reload ssh; then
    restore_sshd_config "$backup"
    die "sshd reload failed; configuration restored from $backup"
  fi
  effective="$(sshd -T)" || {
    restore_sshd_config "$backup"
    die "sshd -T failed after reload; configuration restored from $backup"
  }
  if ! printf '%s\n' "$effective" | grep -Eq '^permitrootlogin (prohibit-password|without-password)$' || \
    ! printf '%s\n' "$effective" | grep -Fxq 'passwordauthentication no'; then
    restore_sshd_config "$backup"
    die "sshd effective settings are not hardened; configuration restored from $backup"
  fi
  log "SSH hardened with reload; previous configuration backed up at $backup"
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
    log 'DRY_RUN: would create deploy user, docker group membership, and minimal sudoers'
    return 0
  fi
  [[ "$(id -u)" -eq 0 ]] || die '10_system.sh must run as root on the target host'
  require_cmd apt-get
  local backup_packages=()
  command -v gpg >/dev/null 2>&1 || backup_packages+=(gnupg)
  command -v mysqldump >/dev/null 2>&1 || backup_packages+=(default-mysql-client)
  command -v aws >/dev/null 2>&1 || backup_packages+=(awscli)
  command -v runuser >/dev/null 2>&1 || backup_packages+=(util-linux)
  if ((${#backup_packages[@]} > 0)); then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install --yes "${backup_packages[@]}"
  fi
  # Stage-two finding (2026-09-02): a clean Debian 12 host did not have the
  # docker group. Install the required local packages here so bootstrap really
  # works from a blank host; do not silently assume Docker was preinstalled.
  if ! command -v docker >/dev/null 2>&1 || \
    { ! docker compose version >/dev/null 2>&1 && ! command -v docker-compose >/dev/null 2>&1; }; then
    require_cmd apt-get
    apt-get update
    local compose_package=docker-compose
    if apt-cache show docker-compose-v2 >/dev/null 2>&1; then
      compose_package=docker-compose-v2
    elif apt-cache show docker-compose-plugin >/dev/null 2>&1; then
      compose_package=docker-compose-plugin
    fi
    DEBIAN_FRONTEND=noninteractive apt-get install --yes \
      ca-certificates docker.io "$compose_package" sudo ufw fail2ban gnupg
    require_cmd systemctl
    systemctl enable --now docker
  fi
  require_cmd useradd
  require_cmd usermod
  require_cmd install
  require_cmd visudo

  if ! id deploy >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash deploy
  fi
  if getent group docker >/dev/null 2>&1; then
    usermod --append --groups docker deploy
  else
    die 'docker group does not exist; install Docker before creating deploy access'
  fi
  install --directory --owner deploy --group deploy --mode 0750 "$DEPLOY_ROOT"

  local tmp sudoers_path
  sudoers_path=/etc/sudoers.d/lingsway-deploy
  tmp="$(mktemp)"
  SUDOERS_TMP="$tmp"
  trap cleanup_sudoers_tmp EXIT
  printf '%s\n' \
    'Cmnd_Alias LINGSWAY_DEPLOY = /usr/bin/docker, /usr/bin/docker compose *, /usr/bin/systemctl restart lingsway-*' \
    'deploy ALL=(root) NOPASSWD: LINGSWAY_DEPLOY' > "$tmp"
  visudo --check --file "$tmp"
  install --owner root --group root --mode 0440 "$tmp" "$sudoers_path"
  harden_ssh
  log 'deploy user, minimal sudoers, and configured SSH hardening completed'
}

main "$@"
