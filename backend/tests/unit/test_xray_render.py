import json
from pathlib import Path
from typing import cast

from backend.app.core.config import Settings
from backend.app.providers.base import CredentialDTO, DesiredRoutingState, XrayOutboundDTO
from backend.app.providers.gateway.xray_composition import (
    XrayDeploymentConfig,
    XrayRealityConfig,
    XrayStaticSkeleton,
)
from backend.app.providers.gateway.xray_file import XrayFileProvider
from ops.gateway.render_xray_routes import render_config


def _template() -> dict[str, object]:
    path = Path(__file__).parents[3] / "infrastructure/marzban/xray_config.base.json"
    return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))


class _Resolver:
    def resolve(self, secret_ref: str) -> CredentialDTO:
        return CredentialDTO("user-" + secret_ref, "password-" + secret_ref)

    def resolve_reality_identity(self) -> XrayRealityConfig:
        return XrayRealityConfig("persisted-private-key", ["persisted-short-id"])


def test_empty_desired_render_has_complete_file_and_terminal_guards() -> None:
    provider = XrayFileProvider.from_template(
        None,  # type: ignore[arg-type]
        _template(),
        Settings(
            xray_reality_dest="settings-dest.example:443",
            xray_reality_server_name="settings-server.example",
        ),
    )

    rendered = provider.render(DesiredRoutingState(), _Resolver())

    assert rendered.content["log"] == {"loglevel": "warning"}
    assert rendered.content["inbounds"][0]["settings"] == {  # type: ignore[index]
        "clients": [],
        "decryption": "none",
    }
    assert rendered.content["outbounds"] == [{"tag": "BLOCK", "protocol": "blackhole"}]
    assert rendered.content["routing"]["rules"] == [  # type: ignore[index]
        {"type": "field", "ip": ["geoip:private"], "outboundTag": "BLOCK"},
        {"type": "field", "network": "tcp,udp", "outboundTag": "BLOCK"},
    ]
    assert all(
        rule.get("outboundTag") != "DIRECT" for rule in rendered.content["routing"]["rules"]  # type: ignore[index]
    )


def test_standalone_and_provider_use_the_same_full_composer() -> None:
    routes = [
        (
            "principal",
            "egress",
            "socks5",
            1080,
            "proxy.example.invalid",
            {"username": "user-ops-render/egress", "password": "password-ops-render/egress"},
        )
    ]
    settings = Settings(
        xray_reality_dest="settings-dest.example:443",
        xray_reality_server_name="settings-server.example",
    )
    standalone = render_config(
        _template(),
        routes,
        reality_dest=settings.xray_reality_dest,
        reality_server_name=settings.xray_reality_server_name,
        reality_identity=XrayRealityConfig("persisted-private-key", ["persisted-short-id"]),
    )
    provider = XrayFileProvider(
        None,  # type: ignore[arg-type]
        static_skeleton=XrayStaticSkeleton.from_template(_template()),
        deployment_config=XrayDeploymentConfig.from_settings(settings),
    )
    provider_candidate = provider.render(
        DesiredRoutingState(
            {"principal": "egress"},
            (
                XrayOutboundDTO(
                    "egress", "proxy.example.invalid", 1080, "socks5", "ops-render/egress"
                ),
            ),
        ),
        _Resolver(),
    )
    assert standalone == provider_candidate.content

