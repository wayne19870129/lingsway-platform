#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

validate_runtime_files() {
  local marzban_dir xray_config sqlite_db
  marzban_dir="${MARZBAN_DATA_DIR:-$DEPLOY_ROOT/data/marzban}"
  xray_config="${XRAY_RUNTIME_CONFIG_FILE:-$marzban_dir/xray_config.json}"
  sqlite_db="${MARZBAN_DB_FILE:-$marzban_dir/db.sqlite3}"
  require_cmd python3
  [[ -f "$xray_config" ]] || die "rendered Xray config is missing: $xray_config; do not mount the base skeleton directly"
  [[ -f "$sqlite_db" ]] || die "Marzban SQLite database file is missing: $sqlite_db"
  python3 - "$xray_config" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    config = json.loads(path.read_text(encoding="utf-8"))
except (OSError, ValueError) as exc:
    raise SystemExit(f"rendered Xray config is not valid JSON: {exc}")
if not isinstance(config, dict):
    raise SystemExit("rendered Xray config must be a JSON object")
if not isinstance(config.get("outbounds"), list) or not config["outbounds"]:
    raise SystemExit("rendered Xray config must contain at least one rendered outbound")
rules = config.get("routing", {}).get("rules")
if not isinstance(rules, list) or not rules:
    raise SystemExit("rendered Xray config must contain rendered routing rules")
private = {"type": "field", "ip": ["geoip:private"], "outboundTag": "BLOCK"}
fallback = {"type": "field", "network": "tcp,udp", "outboundTag": "BLOCK"}
if rules[0] != private or rules[-1] != fallback:
    raise SystemExit("rendered Xray config routing BLOCK invariants are not satisfied")
if any(isinstance(rule, dict) and rule.get("outboundTag") == "DIRECT" for rule in rules):
    raise SystemExit("rendered Xray config must not contain a DIRECT fallback")
PY
}

ensure_marzban_internal_tls() {
  local marzban_dir="$1" certificate="$marzban_dir/internal.crt" key="$marzban_dir/internal.key"
  require_cmd openssl
  if [[ ! -e "$certificate" && ! -e "$key" ]]; then
    openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 3650 \
      -subj '/CN=localhost' \
      -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1' \
      -keyout "$key" -out "$certificate" >/dev/null 2>&1
    chmod 600 "$certificate" "$key"
    return
  fi
  [[ -f "$certificate" && -f "$key" ]] || \
    die 'Marzban internal TLS files are incomplete; refusing to overwrite an existing half-pair'
}

port_is_listening() {
  local port="$1"
  ss -ltnH | awk -v port=":$port" '$4 ~ port "$" { found = 1 } END { exit !found }'
}

verify_and_promote_xray() {
  local expected_sha="$1" baseline_path container_id state health deadline
  baseline_path="${XRAY_CONTAINER_BASELINE_PATH:-/app/data/marzban/xray_config.last_applied.json}"
  deadline=$((SECONDS + ${XRAY_APPLY_VERIFY_TIMEOUT_SECONDS:-120}))
  require_cmd ss

  while (( SECONDS < deadline )); do
    container_id="$(compose ps -q marzban 2>/dev/null | head -n 1)" || container_id=''
    if [[ -z "$container_id" ]]; then
      sleep 1
      continue
    fi
    state="$(docker inspect --format '{{.State.Status}}' "$container_id")" || state=''
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
      "$container_id")" || health=''
    if [[ "$state" != running || "$health" != healthy ]]; then
      sleep 1
      continue
    fi
    if ! port_is_listening 8443; then
      sleep 1
      continue
    fi
    if ! compose exec -T backend-api xray run -test \
      -config "${XRAY_CONTAINER_CONFIG_FILE:-/app/data/marzban/xray_config.json}"; then
      sleep 1
      continue
    fi
    compose run --rm --no-deps \
      -e "XRAY_APPLIED_STATE_PATH=$baseline_path" \
      backend-api python -m ops.gateway.promote_xray_baseline \
      --expected-sha "$expected_sha" \
      --config "${XRAY_CONTAINER_CONFIG_FILE:-/app/data/marzban/xray_config.json}" \
      --baseline "$baseline_path" || \
      die 'verified Xray config differs from the rendered candidate; baseline not promoted'
    log "Xray apply verified; promoted APPLIED baseline sha=$expected_sha"
    return 0
  done
  die 'Xray/Marzban startup verification timed out; baseline was not promoted'
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
    log 'DRY_RUN: would validate and start the selected Compose profiles'
    return 0
  fi
  require_cmd docker
  local services
  services="$(compose config --services)"
  [[ -n "$services" ]] || die 'Compose stack has no services; deployment cannot start an empty skeleton'
  compose config --quiet
  local marzban_dir sqlite_db
  marzban_dir="${MARZBAN_DATA_DIR:-$DEPLOY_ROOT/data/marzban}"
  sqlite_db="${MARZBAN_DB_FILE:-$marzban_dir/db.sqlite3}"
  install --directory "$marzban_dir"
  # Docker creates a directory when a bind-mounted file is absent. Create
  # the SQLite file explicitly before the one-shot renderer and final stack.
  if [[ ! -e "$sqlite_db" ]]; then
    install -m 600 /dev/null "$sqlite_db"
  fi
  [[ -f "$sqlite_db" ]] || die "Marzban SQLite database path is not a file: $sqlite_db"
  ensure_marzban_internal_tls "$marzban_dir"
  log 'rendering Xray runtime config from the migrated database before Marzban'
  compose build backend-api
  local baseline_path render_output expected_sha
  baseline_path="${XRAY_CONTAINER_BASELINE_PATH:-/app/data/marzban/xray_config.last_applied.json}"
  render_output="$(compose run --rm --no-deps \
    -e "XRAY_APPLIED_STATE_PATH=$baseline_path" \
    backend-api python -m ops.gateway.render_xray_routes)" || \
    die 'Xray runtime rendering failed; baseline was not promoted'
  expected_sha="$(printf '%s\n' "$render_output" | \
    sed -n 's/.*expected_repo_owned_sha=\([0-9a-f]\{64\}\).*/\1/p' | tail -n 1)"
  [[ "$expected_sha" =~ ^[0-9a-f]{64}$ ]] || \
    die 'renderer did not return a valid expected repo-owned Xray fingerprint'
  validate_runtime_files
  compose up --detach
  if is_true "${RESTART_TRANSPORT:-false}"; then
    compose restart marzban mihomo
  else
    compose restart marzban
  fi
  verify_and_promote_xray "$expected_sha"
  log 'Compose stack started with the configured profiles'
}

main "$@"
