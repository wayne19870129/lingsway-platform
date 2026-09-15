from __future__ import annotations

from typing import cast

from backend.app.providers.base import (
    CredentialDTO,
    DesiredRoutingState,
    XrayOutboundDTO,
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
        cast(XrayRuntime, None)
    ).render(desired, resolver)

    standalone = render_config(
        {},
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
    )

    assert provider_candidate.content["outbounds"] == standalone["outbounds"]
    assert provider_candidate.content["routing"] == standalone["routing"]
