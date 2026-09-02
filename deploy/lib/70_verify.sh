#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

failures=0

check_01_compose() {
  command -v docker >/dev/null 2>&1 || return 1
  local rows service state health
  rows="$(compose ps --all --format '{{.Service}} {{.State}} {{.Health}}')"
  [[ -n "$rows" ]] || return 1
  while read -r service state health; do
    [[ -n "$service" && "$state" == running ]] || return 1
    [[ -z "$health" || "$health" == healthy ]] || return 1
  done <<< "$rows"
}

check_02_backend_health() {
  command -v curl >/dev/null 2>&1 || return 1
  local code
  code="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --max-time "${HEALTH_TIMEOUT_SECONDS:-10}" \
    "${BACKEND_HEALTH_URL:-http://127.0.0.1:8000/health}")"
  [[ "$code" == 200 ]]
}

check_03_alembic_head() {
  command -v docker >/dev/null 2>&1 || return 1
  local current head
  current="$(compose exec --no-TTY backend-api alembic current)"
  head="$(compose exec --no-TTY backend-api alembic heads | awk 'NF { print $1; exit }')"
  [[ -n "$head" ]] || return 1
  grep -Fq "$head" <<< "$current" && grep -Fq '(head)' <<< "$current"
}

check_04_dns() {
  command -v getent >/dev/null 2>&1 || return 1
  [[ -n "${TARGET_HOST:-}" ]] || return 1
  local domain resolved
  for domain in "$SITE_DOMAIN" "$API_DOMAIN" "$SUBSCRIPTION_DOMAIN"; do
    [[ -n "$domain" ]] || return 1
    resolved="$(getent ahostsv4 "$domain" | awk '{ print $1 }' | sort -u)"
    printf '%s\n' "$resolved" | grep -Fxq "$TARGET_HOST" || return 1
  done
}

check_05_certificate() {
  command -v openssl >/dev/null 2>&1 || return 1
  local certificate="${CERT_FILE:-/var/lib/caddy/.local/share/caddy/certificates/acme-v02.api.letsencrypt.org-directory/${SITE_DOMAIN}/${SITE_DOMAIN}.crt}"
  [[ -s "$certificate" ]] || return 1
  openssl x509 -in "$certificate" -noout -checkend "$((14 * 86400))"
}

port_is_listening() {
  local port="$1"
  ss -ltnH | awk -v port=":$port" '$4 ~ port "$" { found = 1 } END { exit !found }'
}

check_06_ports() {
  command -v ss >/dev/null 2>&1 || return 1
  local port
  for port in 80 443 8443; do
    port_is_listening "$port" || return 1
  done
  if is_true "${ENABLE_SOCKS_1080:-false}"; then
    port_is_listening 1080 || return 1
  elif port_is_listening 1080; then
    return 1
  fi
}

check_07_ufw() {
  command -v ufw >/dev/null 2>&1 || return 1
  ufw status | grep -Fq 'Status: active' || return 1
  ufw status verbose | grep -Eiq 'Default: deny \(incoming\)' \
    || ufw status verbose | grep -Eiq 'Default: deny \(incoming\), allow \(outgoing\)'
}

check_08_ssh_root_password() {
  command -v sshd >/dev/null 2>&1 || return 1
  local effective
  effective="$(sshd -T)"
  grep -Eiq '^permitrootlogin (no|prohibit-password)$' <<< "$effective"
}

check_09_xray_test() {
  if [[ -n "${XRAY_TEST_COMMAND:-}" ]]; then
    bash -c "$XRAY_TEST_COMMAND"
    return
  fi
  command -v docker >/dev/null 2>&1 || return 1
  compose exec --no-TTY backend-api xray run -test \
    -config "${XRAY_CONFIG_FILE:-/etc/xray/config.json}"
}

check_10_routing_invariants() {
  command -v python3 >/dev/null 2>&1 || return 1
  local config="${XRAY_CONFIG_FILE:-/etc/xray/config.json}"
  [[ -s "$config" ]] || return 1
  python3 - "$config" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    config = json.load(stream)
rules = config.get("routing", {}).get("rules", [])
private = {"type": "field", "ip": ["geoip:private"], "outboundTag": "BLOCK"}
fallback = {"type": "field", "network": "tcp,udp", "outboundTag": "BLOCK"}
if not rules or rules[0] != private or rules[-1] != fallback:
    raise SystemExit(1)
if any(isinstance(rule, dict) and rule.get("outboundTag") == "DIRECT" for rule in rules):
    raise SystemExit(1)
PY
}

check_11_listener_count() {
  [[ -n "${EGRESS_COUNT_COMMAND:-}" && -n "${MIHOMO_LISTENER_COUNT_COMMAND:-}" ]] || return 1
  local expected actual
  expected="$(bash -c "$EGRESS_COUNT_COMMAND")"
  actual="$(bash -c "$MIHOMO_LISTENER_COUNT_COMMAND")"
  [[ "$expected" =~ ^[0-9]+$ && "$actual" =~ ^[0-9]+$ && "$expected" == "$actual" ]]
}

check_12_listener_ips() {
  [[ -n "${MIHOMO_IP_MATCH_COMMAND:-}" ]] || return 1
  bash -c "$MIHOMO_IP_MATCH_COMMAND"
}

check_13_backup() {
  local cron_path="${BACKUP_CRON_PATH:-/etc/cron.d/lingsway-backup}"
  [[ -s "$cron_path" ]] || return 1
  grep -Eq '(^|[[:space:]])(backup\.sh|BACKUP_SCRIPT)' "$cron_path" || return 1
  if [[ -n "${BACKUP_VERIFY_COMMAND:-}" ]]; then
    bash -c "$BACKUP_VERIFY_COMMAND"
  elif [[ -x "${BACKUP_SCRIPT:-$DEPLOY_ROOT/ops/backup/backup.sh}" ]]; then
    "${BACKUP_SCRIPT:-$DEPLOY_ROOT/ops/backup/backup.sh}" --verify
  else
    return 1
  fi
}

check_14_telegram() {
  [[ -n "${TELEGRAM_VERIFY_COMMAND:-}" ]] || return 1
  bash -c "$TELEGRAM_VERIFY_COMMAND"
}

check_15_capacity() {
  [[ -n "${CAPACITY_VERIFY_COMMAND:-}" ]] || return 1
  bash -c "$CAPACITY_VERIFY_COMMAND"
}

run_check() {
  local number="$1" description="$2" function_name="$3"
  if "$function_name"; then
    printf '[PASS] %02d %s\n' "$number" "$description"
  else
    printf '[FAIL] %02d %s\n' "$number" "$description" >&2
    failures=$((failures + 1))
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
  run_check 1 'Docker Compose services are running and healthy' check_01_compose
  run_check 2 'backend /health returns HTTP 200' check_02_backend_health
  run_check 3 'Alembic current revision equals head' check_03_alembic_head
  run_check 4 'all three domains resolve to this host' check_04_dns
  run_check 5 'certificate exists and is valid for more than 14 days' check_05_certificate
  run_check 6 '80/443/8443 listen and 1080 follows its explicit switch' check_06_ports
  run_check 7 'UFW is active with default deny incoming' check_07_ufw
  run_check 8 'SSH root password login is disabled' check_08_ssh_root_password
  run_check 9 'xray run -test succeeds' check_09_xray_test
  run_check 10 'Xray routing invariants hold with no DIRECT fallback' check_10_routing_invariants
  run_check 11 'Mihomo listener count equals database egress count' check_11_listener_count
  run_check 12 'every Mihomo listener IP equals its database record' check_12_listener_ips
  run_check 13 'encrypted backup cron, R2 upload, and SHA-256 verification succeed' check_13_backup
  run_check 14 'Telegram test message is delivered' check_14_telegram
  run_check 15 'admin capacity matches the database calculation' check_15_capacity
  printf '[lingsway-deploy] verification complete: %d failed\n' "$failures"
  (( failures == 0 ))
}

main "$@"
