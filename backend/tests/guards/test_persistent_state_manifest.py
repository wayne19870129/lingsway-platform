from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]
REPOSITORY_ROOT = ROOT.parent
MANIFEST = REPOSITORY_ROOT / "docs/90-migration/persistent-state-manifest.md"
COMPOSE_DIR = REPOSITORY_ROOT / "infrastructure/compose"


def test_persistent_manifest_covers_compose_state_and_separate_sqlite_db() -> None:
    manifest = MANIFEST.read_text(encoding="utf-8")
    compose = "\n".join(path.read_text(encoding="utf-8") for path in COMPOSE_DIR.glob("*.yml"))

    for required in (
        "Marzban SQLite DB",
        "${MARZBAN_DB_FILE}",
        "data/marzban/db.sqlite3",
        "account/user state",
        "SQLite integrity",
        "Xray applied baseline",
        "Reality identity",
        "data/attribution",
        "uptime_kuma_data",
    ):
        assert required in manifest

    for persistent_mount in (
        "mysql_data:/var/lib/mysql",
        "marzban_data:/var/lib/marzban",
        "../../data/marzban/db.sqlite3:/code/db.sqlite3",
        "../../data/marzban/xray_config.json:/code/xray_config.json",
        "../../data/marzban/internal.crt:/var/lib/marzban/internal.crt",
        "../../data/marzban/internal.key:/var/lib/marzban/internal.key",
        "../../data/mihomo:",
        "../../data/xray-reload:",
        "../../data/gateway-probe:",
        "../../data/attribution:",
        "caddy_data:/data",
        "caddy_config:/config",
        "uptime_kuma_data:/app/data",
    ):
        assert persistent_mount in compose
