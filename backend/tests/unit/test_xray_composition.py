from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from backend.app.core.config import Settings
from backend.app.providers.base import CredentialDTO, DesiredRoutingState, XrayOutboundDTO
from backend.app.providers.gateway.xray_composition import (
    XrayCompositionError,
    XrayDeploymentConfig,
    XrayFullConfigInput,
    XrayRealityConfig,
    XrayStaticSkeleton,
    compose_xray_config,
)
from backend.app.providers.gateway.xray_file import XrayFileProvider, XrayValidationError


def _template() -> dict[str, object]:
    path = Path(__file__).parents[3] / "infrastructure/marzban/xray_config.base.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


class _Resolver:
    def resolve(self, secret_ref: str) -> CredentialDTO:
        return CredentialDTO("user-" + secret_ref, "password-" + secret_ref)

    def resolve_reality_identity(self) -> XrayRealityConfig:
        return XrayRealityConfig("private-key", ["short-id"])


def _provider() -> XrayFileProvider:
    return XrayFileProvider.from_template(
        None,  # type: ignore[arg-type]
        _template(),
        Settings(
            xray_reality_dest="settings-dest.example:443",
            xray_reality_server_name="settings-server.example",
        ),
    )


def test_composer_emits_all_canonical_owners_and_ignores_template_values() -> None:
    template = _template()
    template["log"]["loglevel"] = "debug"  # type: ignore[index]
    template["inbounds"][0]["tag"] = "template-tag"  # type: ignore[index]
    template["routing"]["domainStrategy"] = "UseIP"  # type: ignore[index]
    candidate = _provider().render(
        DesiredRoutingState(
            {"principal": "egress"},
            (
                XrayOutboundDTO(
                    "egress", "proxy.example.invalid", 1080, "socks5", "credential/ref"
                ),
            ),
        ),
        _Resolver(),
    )

    assert candidate.version == "xray-full-v1"
    assert candidate.content["log"] == {"loglevel": "warning"}
    inbound = candidate.content["inbounds"][0]
    assert inbound["tag"] == "vless-reality"
    assert inbound["settings"]["clients"] == []
    assert inbound["streamSettings"]["realitySettings"]["privateKey"] == "private-key"
    assert candidate.content["routing"]["domainStrategy"] == "AsIs"  # type: ignore[index]


def test_unknown_template_field_fails_closed_without_copying_it() -> None:
    template = _template()
    template["unexpected"] = {"sentinel": True}

    with pytest.raises(XrayCompositionError, match="unknown top-level"):
        XrayStaticSkeleton.from_template(template)


def test_secret_bearing_composition_types_are_not_generic_dataclasses() -> None:
    reality = XrayRealityConfig("sentinel-private-key", ["sentinel-short-id"])
    input_value = XrayFullConfigInput(
        XrayStaticSkeleton.canonical(),
        XrayDeploymentConfig(
            xray_log_level="warning",
            reality_dest="dest.example:443",
            reality_server_names=("dest.example",),
        ),
        reality,
        DesiredRoutingState(),
        {},
    )

    assert "sentinel-private-key" not in repr(reality)
    assert "sentinel-short-id" not in repr(reality)
    assert repr(input_value) == "XrayFullConfigInput(<redacted>)"
    with pytest.raises(TypeError):
        asdict(reality)  # type: ignore[call-overload]
    with pytest.raises(TypeError):
        asdict(input_value)  # type: ignore[call-overload]


def test_provider_requires_xray_resolver_capability_before_any_secret_read() -> None:
    class GenericOnlyResolver:
        def resolve(self, secret_ref: str) -> CredentialDTO:
            raise AssertionError(f"must not resolve {secret_ref}")

    with pytest.raises(XrayValidationError, match="capability"):
        _provider().render(DesiredRoutingState(), GenericOnlyResolver())


def test_pure_composer_has_no_runtime_dependency() -> None:
    full_input = XrayFullConfigInput(
        XrayStaticSkeleton.canonical(),
        XrayDeploymentConfig(
            xray_log_level="error",
            reality_dest="dest.example:443",
            reality_server_names=("dest.example",),
        ),
        XrayRealityConfig("private-key", ["short-id"]),
        DesiredRoutingState(),
        {},
    )

    candidate = compose_xray_config(full_input)
    assert candidate.content["log"] == {"loglevel": "error"}

