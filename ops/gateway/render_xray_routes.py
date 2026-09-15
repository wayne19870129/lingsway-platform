"""Render the complete Xray runtime configuration from database state."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import select

from backend.app.core.config import get_settings
from backend.app.core.database import SessionLocal
from backend.app.infra.credential_resolver import SqlAlchemyCredentialResolver
from backend.app.models import EgressBinding, EgressEndpoint, GatewayRouteBinding
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
                DesiredRoutingState(desired_routes, tuple(outbounds)),
                credentials,
            )
        )
    except (XrayCompositionError, TypeError, ValueError) as exc:
        raise XrayRenderError("Xray full configuration composition failed") from exc
    return dict(candidate.content)


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
        reality = resolver.resolve_reality_identity()
        rows = []
        for route, endpoint, secret_ref in _active_routes(db):
            credential = resolver.resolve(secret_ref)
            rows.append(
                (
                    route.gateway_principal,
                    route.outbound_tag,
                    endpoint.protocol,
                    endpoint.port,
                    endpoint.host,
                    {"username": credential.username, "password": credential.password},
                )
            )
        rendered = render_config(
            base_config,
            rows,
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
    output = render_from_database()
    settings = get_settings()
    print(f"rendered Xray config to {output} using {settings.app_env} settings")


if __name__ == "__main__":
    main()

