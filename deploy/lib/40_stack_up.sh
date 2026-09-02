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
  validate_runtime_files
  local services
  services="$(compose config --services)"
  [[ -n "$services" ]] || die 'Compose stack has no services; deployment cannot start an empty skeleton'
  compose config --quiet
  compose up --detach
  if is_true "${RESTART_TRANSPORT:-false}"; then
    compose up --detach marzban mihomo
  fi
  log 'Compose stack started with the configured profiles'
}

main "$@"
