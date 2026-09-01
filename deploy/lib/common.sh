#!/usr/bin/env bash

set -euo pipefail

DEPLOY_ROOT="${DEPLOY_ROOT:-/opt/lingsway}"
INVENTORY_FILE="${INVENTORY_FILE:-}"
DRY_RUN="${DRY_RUN:-false}"

log() {
  printf '[lingsway-deploy] %s\n' "$*"
}

die() {
  printf '[lingsway-deploy] ERROR: %s\n' "$*" >&2
  exit 1
}

is_true() {
  case "${1:-}" in
    1|true|TRUE|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command is unavailable: $1"
}

require_inventory() {
  [[ -n "$INVENTORY_FILE" ]] || die 'inventory file was not supplied'
  [[ -f "$INVENTORY_FILE" ]] || die "inventory file does not exist: $INVENTORY_FILE"
}

inventory_top_level() {
  local key="$1"
  awk -v key="$key" '
    /^[[:space:]]*#/ { next }
    $0 ~ "^" key ":[[:space:]]*" {
      value = $0
      sub("^[^:]+:[[:space:]]*", "", value)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      gsub(/^\"|\"$/, "", value)
      print value
      exit
    }
  ' "$INVENTORY_FILE"
}

inventory_value() {
  local section="$1"
  local key="$2"
  awk -v section="$section" -v key="$key" '
    /^[[:space:]]*#/ { next }
    $0 ~ "^[[:space:]]*" section ":[[:space:]]*$" { inside = 1; next }
    inside && $0 ~ "^[^[:space:]]" { inside = 0 }
    inside && $0 ~ "^[[:space:]]+" key ":[[:space:]]*" {
      value = $0
      sub("^[[:space:]]*[^:]+:[[:space:]]*", "", value)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      gsub(/^\"|\"$/, "", value)
      print value
      exit
    }
  ' "$INVENTORY_FILE"
}

compose() {
  local args=(
    -f "${COMPOSE_BASE_FILE:-$DEPLOY_ROOT/infrastructure/compose/compose.base.yml}"
    -f "${COMPOSE_TRANSPORT_FILE:-$DEPLOY_ROOT/infrastructure/compose/compose.transport.yml}"
  )
  if is_true "${ENABLE_MONITORING:-false}"; then
    args+=(-f "${COMPOSE_MONITORING_FILE:-$DEPLOY_ROOT/infrastructure/compose/compose.monitoring.yml}")
    args+=(--profile monitoring)
  fi
  if is_true "${ENABLE_PROBE:-false}"; then
    args+=(-f "${COMPOSE_PROBE_FILE:-$DEPLOY_ROOT/infrastructure/compose/compose.probe.yml}")
    args+=(--profile probe)
  fi
  docker compose "${args[@]}" "$@"
}

load_inventory_defaults() {
  require_inventory
  TARGET_HOST="${TARGET_HOST:-$(inventory_top_level host)}"
  SITE_DOMAIN="${SITE_DOMAIN:-$(inventory_value domains site)}"
  API_DOMAIN="${API_DOMAIN:-$(inventory_value domains api)}"
  SUBSCRIPTION_DOMAIN="${SUBSCRIPTION_DOMAIN:-$(inventory_value domains subscription)}"
  EGRESS_PROVIDER="${EGRESS_PROVIDER:-$(inventory_value providers egress)}"
  ACCOUNTING_PROVIDER="${ACCOUNTING_PROVIDER:-$(inventory_value providers accounting)}"
  GATEWAY_PROVIDER="${GATEWAY_PROVIDER:-$(inventory_value providers gateway)}"
  FORWARDER_PROVIDER="${FORWARDER_PROVIDER:-$(inventory_value providers forwarder)}"
  PAYMENT_PROVIDER="${PAYMENT_PROVIDER:-$(inventory_value providers payment)}"
  NOTIFY_PROVIDER="${NOTIFY_PROVIDER:-$(inventory_value providers notify)}"
  EMAIL_PROVIDER="${EMAIL_PROVIDER:-$(inventory_value providers email)}"
  CAPTCHA_PROVIDER="${CAPTCHA_PROVIDER:-$(inventory_value providers captcha)}"
  STORAGE_PROVIDER="${STORAGE_PROVIDER:-$(inventory_value providers storage)}"
  export TARGET_HOST SITE_DOMAIN API_DOMAIN SUBSCRIPTION_DOMAIN
  export EGRESS_PROVIDER ACCOUNTING_PROVIDER GATEWAY_PROVIDER FORWARDER_PROVIDER
  export PAYMENT_PROVIDER NOTIFY_PROVIDER EMAIL_PROVIDER CAPTCHA_PROVIDER STORAGE_PROVIDER
}
