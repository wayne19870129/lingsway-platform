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
  log 'rendering Xray runtime config from the migrated database before Marzban'
  compose build backend-api
  compose run --rm --no-deps backend-api \
    python -m ops.gateway.render_xray_routes
  validate_runtime_files
  compose up --detach
  if is_true "${RESTART_TRANSPORT:-false}"; then
    compose up --detach marzban mihomo
  fi
  log 'Compose stack started with the configured profiles'
}

main "$@"
