from __future__ import annotations

from typing import cast

from backend.app.providers.base import (
    CredentialDTO,
    DesiredRoutingState,
    XrayOutboundDTO,
)
from backend.app.providers.gateway.xray_composition import (
    XrayDeploymentConfig,
    XrayRealityConfig,
    XrayStaticSkeleton,
)
from backend.app.providers.gateway.xray_file import XrayFileProvider, XrayRuntime
from ops.gateway.render_xray_routes import render_config


class _FixedCredentialResolver:
    def __init__(self) -> None:
        self._credentials = {
            "credential-a": CredentialDTO("user-a", "password-a"),
            "credential-b": CredentialDTO("user-b", "password-b"),
        }

    def resolve(self, secret_ref: str) -> CredentialDTO:
        return self._credentials[secret_ref]

    def resolve_reality_identity(self) -> XrayRealityConfig:
        return XrayRealityConfig("reality-private-key", ["reality-short-id"])


def test_xray_provider_matches_standalone_socks_and_routing_fragment() -> None:
    resolver = _FixedCredentialResolver()
    desired = DesiredRoutingState(
        user_routes={
            "principal-a": "egress-a",
            "principal-b": "egress-b",
        },
        outbounds=(
            XrayOutboundDTO("egress-a", "proxy-a.example.invalid", 1080, "socks5", "credential-a"),
            XrayOutboundDTO("egress-b", "proxy-b.example.invalid", 1081, "socks", "credential-b"),
        ),
    )
    provider_candidate = XrayFileProvider(
        cast(XrayRuntime, None),
        static_skeleton=XrayStaticSkeleton.canonical(),
        deployment_config=XrayDeploymentConfig(
            xray_log_level="warning",
            reality_dest="dest.example:443",
            reality_server_names=("dest.example",),
        ),
    ).render(desired, resolver)

    standalone = render_config(
        {
            "log": {"loglevel": "warning"},
            "inbounds": [
                {
                    "tag": "vless-reality",
                    "listen": "0.0.0.0",
                    "port": 8443,
                    "protocol": "vless",
                    "settings": {"clients": [], "decryption": "none"},
                    "streamSettings": {
                        "network": "tcp",
                        "security": "reality",
                        "realitySettings": {
                            "show": False,
                            "dest": "",
                            "xver": 0,
                            "serverNames": [],
                            "privateKey": "",
                            "shortIds": [],
                        },
                    },
                }
            ],
            "outbounds": [],
            "routing": {"domainStrategy": "AsIs", "rules": []},
        },
        [
            (
                "principal-a",
                "egress-a",
                "socks5",
                1080,
                "proxy-a.example.invalid",
                {"username": "user-a", "password": "password-a"},
            ),
            (
                "principal-b",
                "egress-b",
                "socks",
                1081,
                "proxy-b.example.invalid",
                {"username": "user-b", "password": "password-b"},
            ),
        ],
        reality_dest="dest.example:443",
        reality_server_name="dest.example",
        reality_identity=XrayRealityConfig("reality-private-key", ["reality-short-id"]),
    )

    assert provider_candidate.content == standalone

