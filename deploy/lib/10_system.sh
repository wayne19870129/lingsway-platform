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
  log 'deploy user and minimal sudoers installed; sshd and root access were not modified'
}

main "$@"
