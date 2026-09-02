"""Render the complete Xray runtime configuration from database state."""

from __future__ import annotations

import copy
import json
import os
import secrets
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import select

from backend.app.core.config import get_settings
from backend.app.core.database import SessionLocal
from backend.app.core.secrets import reveal_secret
from backend.app.models import EgressBinding, EgressEndpoint, GatewayRouteBinding


class XrayRenderError(RuntimeError):
    """Raised when a complete safe runtime configuration cannot be rendered."""


BASE_CONFIG = Path(
    os.getenv("XRAY_BASE_CONFIG_PATH", "/app/infrastructure/marzban/xray_config.base.json")
)
OUTPUT_CONFIG = Path(
    os.getenv("XRAY_RUNTIME_CONFIG_PATH", "/app/data/marzban/xray_config.json")
)


def _x25519_private_key(binary: str) -> str:
    result = subprocess.run(
        [binary, "x25519"], check=False, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise XrayRenderError("xray x25519 key generation failed")
    for line in result.stdout.splitlines():
        label, separator, value = line.partition(":")
        if separator and label.strip().lower() == "private key" and value.strip():
            return value.strip()
    raise XrayRenderError("xray x25519 output did not contain a private key")


def _reality_settings(config: dict[str, Any]) -> dict[str, Any]:
    for inbound in config.get("inbounds", []):
        if not isinstance(inbound, dict):
            continue
        stream = inbound.get("streamSettings")
        if not isinstance(stream, dict) or stream.get("security") != "reality":
            continue
        reality = stream.get("realitySettings")
        if not isinstance(reality, dict):
            raise XrayRenderError("Reality inbound has no realitySettings object")
        reality.setdefault("show", False)
        reality.setdefault("xver", 0)
        reality["dest"] = reality.get("dest") or os.getenv("XRAY_REALITY_DEST", "")
        server_names = reality.get("serverNames") or []
        if not server_names:
            configured = os.getenv("XRAY_REALITY_SERVER_NAME", "")
            server_names = [configured] if configured else []
        reality["serverNames"] = server_names
        if not reality["dest"] or not reality["serverNames"]:
            raise XrayRenderError(
                "XRAY_REALITY_DEST and XRAY_REALITY_SERVER_NAME are required"
            )
        if not reality.get("privateKey"):
            reality["privateKey"] = _x25519_private_key(
                os.getenv("XRAY_BINARY", "/usr/local/bin/xray")
            )
        reality["shortIds"] = reality.get("shortIds") or [secrets.token_hex(8)]
        return reality
    raise XrayRenderError("base config has no Reality inbound")


def _credential(db: Any, secret_ref: str) -> dict[str, str]:
    try:
        value = json.loads(reveal_secret(db, secret_ref))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise XrayRenderError(f"credential secret is not valid JSON: {secret_ref}") from exc
    if not isinstance(value, dict):
        raise XrayRenderError(f"credential secret is not an object: {secret_ref}")
    username = value.get("username")
    password = value.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        raise XrayRenderError(f"credential secret has no username/password: {secret_ref}")
    return {"username": username, "password": password}


def _active_routes(db: Any) -> list[tuple[GatewayRouteBinding, EgressEndpoint, str]]:
    rows = db.execute(
        select(GatewayRouteBinding, EgressEndpoint)
        .join(EgressEndpoint, EgressEndpoint.id == GatewayRouteBinding.egress_id)
        .where(
            GatewayRouteBinding.enabled.is_(True),
            GatewayRouteBinding.released_at.is_(None),
        )
        .order_by(GatewayRouteBinding.id)
    ).all()
    result: list[tuple[GatewayRouteBinding, EgressEndpoint, str]] = []
    for route, endpoint in rows:
        binding = db.scalar(
            select(EgressBinding).where(
                EgressBinding.subscription_id == route.subscription_id,
                EgressBinding.egress_id == route.egress_id,
                EgressBinding.released_at.is_(None),
            )
        )
        secret_ref = (
            binding.credential_secret_ref
            if binding and binding.credential_secret_ref
            else endpoint.credential_secret_ref
        )
        result.append((route, endpoint, secret_ref))
    return result


def render_config(
    base_config: Mapping[str, Any],
    routes: Sequence[tuple[str, str, str, int, str, dict[str, str]]],
) -> dict[str, Any]:
    config = copy.deepcopy(dict(base_config))
    config["outbounds"] = [{"tag": "BLOCK", "protocol": "blackhole"}]
    user_rules: list[dict[str, Any]] = []
    for principal, outbound_tag, protocol, port, host, credential in routes:
        if protocol.lower() not in {"socks", "socks5"}:
            raise XrayRenderError(f"unsupported egress protocol: {protocol}")
        config["outbounds"].append(
            {
                "tag": outbound_tag,
                "protocol": "socks",
                "settings": {
                    "servers": [
                        {
                            "address": host,
                            "port": port,
                            "users": [credential],
                        }
                    ]
                },
            }
        )
        user_rules.append(
            {"type": "field", "user": [principal], "outboundTag": outbound_tag}
        )
    config["routing"] = dict(config.get("routing", {}))
    config["routing"]["rules"] = [
        {"type": "field", "ip": ["geoip:private"], "outboundTag": "BLOCK"},
        *user_rules,
        {"type": "field", "network": "tcp,udp", "outboundTag": "BLOCK"},
    ]
    return config


def render_from_database() -> Path:
    try:
        base_config = json.loads(BASE_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise XrayRenderError(f"cannot read base Xray config: {BASE_CONFIG}") from exc
    if not isinstance(base_config, dict):
        raise XrayRenderError("base Xray config must be a JSON object")
    _reality_settings(base_config)
    with SessionLocal() as db:
        rows = []
        for route, endpoint, secret_ref in _active_routes(db):
            rows.append(
                (
                    route.gateway_principal,
                    route.outbound_tag,
                    endpoint.protocol,
                    endpoint.port,
                    endpoint.host,
                    _credential(db, secret_ref),
                )
            )
        rendered = render_config(base_config, rows)
    OUTPUT_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=OUTPUT_CONFIG.parent, prefix=".xray_config.", suffix=".json", delete=False
    ) as stream:
        json.dump(rendered, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        temporary = Path(stream.name)
    temporary.chmod(0o600)
    temporary.replace(OUTPUT_CONFIG)
    return OUTPUT_CONFIG


def main() -> None:
    output = render_from_database()
    settings = get_settings()
    print(f"rendered Xray config to {output} using {settings.app_env} settings")


if __name__ == "__main__":
    main()
