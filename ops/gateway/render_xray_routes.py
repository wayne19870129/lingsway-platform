"""Render the complete Xray runtime configuration from database state."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from backend.app.core.config import get_settings
from backend.app.core.database import SessionLocal
from backend.app.infra.credential_resolver import SqlAlchemyCredentialResolver
from backend.app.infra.provisioning_state import full_desired_routing_snapshot
from backend.app.infra.xray_reality import bootstrap_reality_identity
from backend.app.providers.base import CredentialDTO, DesiredRoutingState, XrayOutboundDTO
from backend.app.providers.gateway.xray_composition import (
    XrayCompositionError,
    XrayDeploymentConfig,
    XrayFullConfigInput,
    XrayRealityConfig,
    XrayStaticSkeleton,
    compose_xray_config,
)


class XrayRenderError(RuntimeError):
    """Raised when a complete safe runtime configuration cannot be rendered."""


BASE_CONFIG = Path(
    os.getenv("XRAY_BASE_CONFIG_PATH", "/app/infrastructure/marzban/xray_config.base.json")
)
OUTPUT_CONFIG = Path(
    os.getenv("XRAY_RUNTIME_CONFIG_PATH", "/app/data/marzban/xray_config.json")
)


def _compose_desired(
    base_config: Mapping[str, Any],
    desired: DesiredRoutingState,
    credentials: Mapping[str, CredentialDTO],
    *,
    xray_log_level: str,
    reality_dest: str,
    reality_server_name: str,
    reality_identity: XrayRealityConfig,
) -> dict[str, Any]:
    try:
        skeleton = XrayStaticSkeleton.from_template(base_config)
        deployment = XrayDeploymentConfig(
            xray_log_level=xray_log_level,
            reality_dest=reality_dest,
            reality_server_names=(reality_server_name,),
        )
        candidate = compose_xray_config(
            XrayFullConfigInput(
                skeleton,
                deployment,
                reality_identity,
                desired,
                credentials,
            )
        )
    except (XrayCompositionError, TypeError, ValueError) as exc:
        raise XrayRenderError("Xray full configuration composition failed") from exc
    return dict(candidate.content)


def render_config(
    base_config: Mapping[str, Any],
    routes: Sequence[tuple[str, str, str, int, str, dict[str, str]]],
    *,
    xray_log_level: str = "warning",
    reality_dest: str = "",
    reality_server_name: str = "",
    reality_identity: XrayRealityConfig | None = None,
) -> dict[str, Any]:
    if reality_identity is None:
        raise XrayRenderError("Xray Reality identity is required")
    desired_routes: dict[str, str] = {}
    outbounds: list[XrayOutboundDTO] = []
    credentials: dict[str, CredentialDTO] = {}
    for principal, outbound_tag, protocol, port, host, credential in routes:
        ref = f"ops-render/{outbound_tag}"
        outbounds.append(XrayOutboundDTO(outbound_tag, host, port, protocol, ref))
        desired_routes[principal] = outbound_tag
        credentials[ref] = CredentialDTO(
            credential.get("username", ""), credential.get("password", "")
        )
    return _compose_desired(
        base_config,
        DesiredRoutingState(desired_routes, tuple(outbounds)),
        credentials,
        xray_log_level=xray_log_level,
        reality_dest=reality_dest,
        reality_server_name=reality_server_name,
        reality_identity=reality_identity,
    )


def render_from_database() -> Path:
    try:
        base_config = json.loads(BASE_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise XrayRenderError(f"cannot read base Xray config: {BASE_CONFIG}") from exc
    if not isinstance(base_config, dict):
        raise XrayRenderError("base Xray config must be a JSON object")
    settings = get_settings()
    with SessionLocal() as db:
        resolver = SqlAlchemyCredentialResolver(db)
        desired = full_desired_routing_snapshot(db)
        reality = resolver.resolve_reality_identity()
        credentials = {
            outbound.credential_secret_ref: resolver.resolve(
                outbound.credential_secret_ref
            )
            for outbound in desired.outbounds
        }
        rendered = _compose_desired(
            base_config,
            desired,
            credentials,
            xray_log_level=settings.xray_log_level,
            reality_dest=settings.xray_reality_dest,
            reality_server_name=settings.xray_reality_server_name,
            reality_identity=reality,
        )
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
    with SessionLocal() as db:
        bootstrap_reality_identity(
            db,
            xray_binary=os.getenv("XRAY_BINARY", "/usr/local/bin/xray"),
        )
    output = render_from_database()
    settings = get_settings()
    print(f"rendered Xray config to {output} using {settings.app_env} settings")


if __name__ == "__main__":
    main()

