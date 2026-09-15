"""Pure, canonical composition of the repository-owned Xray file.

The types in this module are deliberately concrete Xray adapter types.  The
generic provider contract remains in ``providers.base``; this module is the
only place where the full Xray JSON shape is assembled.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from backend.app.core.config import Settings
from backend.app.providers.base import (
    CandidateConfig,
    CredentialDTO,
    CredentialResolver,
    DesiredRoutingState,
)


class XrayCompositionError(RuntimeError):
    """Raised when a complete Xray candidate cannot be composed safely."""


XRAY_RENDERER_CONSTANTS = MappingProxyType(
    {
        "tag": "vless-reality",
        "decryption": "none",
        "network": "tcp",
        "security": "reality",
        "reality_show": False,
        "reality_xver": 0,
        "domain_strategy": "AsIs",
        "block_tag": "BLOCK",
    }
)
_ALLOWED_LOG_LEVELS = frozenset({"debug", "info", "warning", "error", "none"})


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _mapping(value: object, message: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise XrayCompositionError(message)
    return value


def _keys(value: Mapping[str, object], expected: set[str], message: str) -> None:
    if set(value) != expected:
        raise XrayCompositionError(message)


def _nonblank_string(value: object, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise XrayCompositionError(message)
    return value


class XrayStaticSkeleton:
    """Immutable validated projection of the accepted template skeleton."""

    __slots__ = ("_inbounds",)
    _inbounds: tuple[Mapping[str, object], ...]

    def __init__(self, inbounds: Sequence[Mapping[str, object]]) -> None:
        if not inbounds:
            raise XrayCompositionError("Xray template must contain an inbound")
        projected: list[Mapping[str, object]] = []
        for inbound in inbounds:
            projected.append(
                MappingProxyType(
                    {
                        "listen": _nonblank_string(
                            inbound.get("listen"), "static inbound listen is invalid"
                        ),
                        "port": _valid_port(inbound.get("port"), "static inbound port is invalid"),
                        "protocol": _nonblank_string(
                            inbound.get("protocol"), "static inbound protocol is invalid"
                        ),
                    }
                )
            )
        object.__setattr__(self, "_inbounds", tuple(projected))

    @classmethod
    def from_template(cls, template: Mapping[str, object]) -> XrayStaticSkeleton:
        """Validate the explicit template allowlist and retain only ADR-014 fields."""

        _keys(
            template,
            {"log", "inbounds", "outbounds", "routing"},
            "Xray template contains unknown top-level fields",
        )
        log = _mapping(template["log"], "Xray template log must be an object")
        _keys(log, {"loglevel"}, "Xray template log contains unknown fields")
        _nonblank_string(log["loglevel"], "Xray template loglevel is invalid")

        inbounds = template["inbounds"]
        if not isinstance(inbounds, list) or not inbounds:
            raise XrayCompositionError("Xray template inbounds must be a non-empty list")
        for inbound_value in inbounds:
            inbound = _mapping(inbound_value, "Xray template inbound must be an object")
            _keys(
                inbound,
                {"tag", "listen", "port", "protocol", "settings", "streamSettings"},
                "Xray template inbound contains unknown fields",
            )
            _nonblank_string(inbound["tag"], "Xray template inbound tag is invalid")
            _nonblank_string(inbound["listen"], "static inbound listen is invalid")
            _valid_port(inbound["port"], "static inbound port is invalid")
            _nonblank_string(inbound["protocol"], "static inbound protocol is invalid")

            settings = _mapping(inbound["settings"], "Xray template inbound settings is invalid")
            _keys(
                settings,
                {"clients", "decryption"},
                "Xray template settings contains unknown fields",
            )
            if settings["clients"] != []:
                raise XrayCompositionError("Xray template clients skeleton must be empty")
            _nonblank_string(settings["decryption"], "Xray template decryption is invalid")

            stream = _mapping(
                inbound["streamSettings"], "Xray template streamSettings is invalid"
            )
            _keys(
                stream,
                {"network", "security", "realitySettings"},
                "Xray template streamSettings contains unknown fields",
            )
            _nonblank_string(stream["network"], "Xray template network is invalid")
            _nonblank_string(stream["security"], "Xray template security is invalid")
            reality = _mapping(
                stream["realitySettings"], "Xray template realitySettings is invalid"
            )
            _keys(
                reality,
                {"show", "dest", "xver", "serverNames", "privateKey", "shortIds"},
                "Xray template realitySettings contains unknown fields",
            )
            if not isinstance(reality["show"], bool):
                raise XrayCompositionError("Xray template Reality show is invalid")
            if not isinstance(reality["xver"], int) or isinstance(reality["xver"], bool):
                raise XrayCompositionError("Xray template Reality xver is invalid")
            if not isinstance(reality["dest"], str):
                raise XrayCompositionError("Xray template Reality dest is invalid")
            _string_list(
                reality["serverNames"],
                "Xray template Reality serverNames is invalid",
                allow_empty=True,
            )
            if not isinstance(reality["privateKey"], str):
                raise XrayCompositionError("Xray template Reality privateKey is invalid")
            _string_list(
                reality["shortIds"],
                "Xray template Reality shortIds is invalid",
                allow_empty=True,
            )

        outbounds = template["outbounds"]
        if not isinstance(outbounds, list) or outbounds:
            raise XrayCompositionError("Xray template outbounds must be the empty list")
        routing = _mapping(template["routing"], "Xray template routing must be an object")
        _keys(routing, {"domainStrategy", "rules"}, "Xray template routing contains unknown fields")
        _nonblank_string(routing["domainStrategy"], "Xray template domainStrategy is invalid")
        if routing["rules"] != []:
            raise XrayCompositionError("Xray template routing rules must be the empty list")
        return cls(inbounds)

    @classmethod
    def canonical(cls) -> XrayStaticSkeleton:
        """Return the repository's accepted default skeleton for tests/adapters."""

        return cls(({"listen": "0.0.0.0", "port": 8443, "protocol": "vless"},))

    @property
    def inbounds(self) -> tuple[Mapping[str, object], ...]:
        return self._inbounds

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __repr__(self) -> str:
        return f"XrayStaticSkeleton(inbounds={len(self.inbounds)!r})"


class XrayDeploymentConfig:
    """Immutable, non-secret Settings projection used by Xray composition."""

    __slots__ = ("_xray_log_level", "_reality_dest", "_reality_server_names")
    _xray_log_level: str
    _reality_dest: str
    _reality_server_names: tuple[str, ...]

    def __init__(
        self,
        *,
        xray_log_level: str,
        reality_dest: str,
        reality_server_names: Sequence[str],
    ) -> None:
        if isinstance(reality_server_names, (str, bytes)):
            raise XrayCompositionError("XRAY_REALITY_SERVER_NAME is required")
        normalized_level = _normalize_log_level(xray_log_level)
        dest = _nonblank_string(reality_dest, "XRAY_REALITY_DEST is required").strip()
        names = tuple(
            item.strip()
            for item in reality_server_names
            if isinstance(item, str) and item.strip()
        )
        if not names or len(names) != len(reality_server_names):
            raise XrayCompositionError("XRAY_REALITY_SERVER_NAME is required")
        object.__setattr__(self, "_xray_log_level", normalized_level)
        object.__setattr__(self, "_reality_dest", dest)
        object.__setattr__(self, "_reality_server_names", names)

    @classmethod
    def from_settings(cls, settings: Settings) -> XrayDeploymentConfig:
        return cls(
            xray_log_level=settings.xray_log_level,
            reality_dest=settings.xray_reality_dest,
            reality_server_names=(settings.xray_reality_server_name,),
        )

    @property
    def xray_log_level(self) -> str:
        return self._xray_log_level

    @property
    def reality_dest(self) -> str:
        return self._reality_dest

    @property
    def reality_server_names(self) -> tuple[str, ...]:
        return self._reality_server_names

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __repr__(self) -> str:
        return (
            "XrayDeploymentConfig("
            f"xray_log_level={self.xray_log_level!r}, "
            f"reality_dest={self.reality_dest!r}, "
            f"reality_server_names={self.reality_server_names!r})"
        )


class XrayRealityConfig:
    """Immutable persisted Reality identity with a secret-safe representation."""

    __slots__ = ("_private_key", "_short_ids")
    _private_key: str
    _short_ids: tuple[str, ...]

    def __init__(self, private_key: str, short_ids: Sequence[str]) -> None:
        if isinstance(short_ids, (str, bytes)):
            raise XrayCompositionError("Reality shortIds are invalid")
        key = _nonblank_string(private_key, "Reality privateKey is invalid")
        ids = tuple(
            item for item in short_ids if isinstance(item, str) and item.strip()
        )
        if not ids or len(ids) != len(short_ids):
            raise XrayCompositionError("Reality shortIds are invalid")
        object.__setattr__(self, "_private_key", key)
        object.__setattr__(self, "_short_ids", ids)

    @property
    def private_key(self) -> str:
        return self._private_key

    @property
    def short_ids(self) -> tuple[str, ...]:
        return self._short_ids

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, XrayRealityConfig)
            and self.private_key == other.private_key
            and self.short_ids == other.short_ids
        )

    def __repr__(self) -> str:
        return "XrayRealityConfig(private_key=<redacted>, short_ids=<redacted>)"


@runtime_checkable
class XrayRenderResolver(CredentialResolver, Protocol):
    """Concrete Xray capability layered on the generic operation resolver."""

    def resolve_reality_identity(self) -> XrayRealityConfig: ...


class XrayFullConfigInput:
    """Immutable per-render input; its representation never recurses into secrets."""

    __slots__ = ("_skeleton", "_deployment", "_reality", "_desired", "_credentials")
    _skeleton: XrayStaticSkeleton
    _deployment: XrayDeploymentConfig
    _reality: XrayRealityConfig
    _desired: DesiredRoutingState
    _credentials: Mapping[str, CredentialDTO]

    def __init__(
        self,
        skeleton: XrayStaticSkeleton,
        deployment: XrayDeploymentConfig,
        reality: XrayRealityConfig,
        desired: DesiredRoutingState,
        credentials: Mapping[str, CredentialDTO],
    ) -> None:
        object.__setattr__(self, "_skeleton", skeleton)
        object.__setattr__(self, "_deployment", deployment)
        object.__setattr__(self, "_reality", reality)
        object.__setattr__(self, "_desired", desired)
        object.__setattr__(self, "_credentials", MappingProxyType(dict(credentials)))

    @property
    def skeleton(self) -> XrayStaticSkeleton:
        return self._skeleton

    @property
    def deployment(self) -> XrayDeploymentConfig:
        return self._deployment

    @property
    def reality(self) -> XrayRealityConfig:
        return self._reality

    @property
    def desired(self) -> DesiredRoutingState:
        return self._desired

    @property
    def credentials(self) -> Mapping[str, CredentialDTO]:
        return self._credentials

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __repr__(self) -> str:
        return "XrayFullConfigInput(<redacted>)"


def compose_xray_config(value: XrayFullConfigInput) -> CandidateConfig:
    """Compose one complete canonical Xray candidate without runtime fallback."""

    desired = value.desired
    outbounds: list[dict[str, object]] = [
        {"tag": XRAY_RENDERER_CONSTANTS["block_tag"], "protocol": "blackhole"}
    ]
    outbound_tags: set[str] = set()
    for outbound in desired.outbounds:
        tag = _nonblank_string(outbound.tag, "invalid desired outbound tag")
        if tag == XRAY_RENDERER_CONSTANTS["block_tag"] or tag in outbound_tags:
            raise XrayCompositionError("invalid or duplicate desired outbound tag")
        host = _nonblank_string(outbound.host, "invalid desired outbound host")
        port = _valid_port(outbound.port, "invalid desired outbound port")
        protocol = _nonblank_string(outbound.protocol, "invalid desired outbound protocol").lower()
        if protocol not in {"socks", "socks5"}:
            raise XrayCompositionError("unsupported desired outbound protocol")
        credential = value.credentials.get(outbound.credential_secret_ref)
        if credential is None:
            raise XrayCompositionError("missing resolved outbound credential")
        username = _nonblank_string(credential.username, "resolved outbound username is invalid")
        password = _nonblank_string(credential.password, "resolved outbound password is invalid")
        outbound_tags.add(tag)
        outbounds.append(
            {
                "tag": tag,
                "protocol": "socks",
                "settings": {
                    "servers": [
                        {
                            "address": host,
                            "port": port,
                            "users": [
                                {
                                    "username": username,
                                    "password": password,
                                }
                            ],
                        }
                    ]
                },
            }
        )

    rules: list[dict[str, object]] = [
        {"type": "field", "ip": ["geoip:private"], "outboundTag": "BLOCK"}
    ]
    for principal, target in sorted(desired.user_routes.items()):
        if (
            not isinstance(principal, str)
            or not principal.strip()
            or not isinstance(target, str)
            or not target.strip()
            or target == "BLOCK"
            or target not in outbound_tags
        ):
            raise XrayCompositionError("desired route references missing outbound")
        rules.append({"type": "field", "user": [principal], "outboundTag": target})
    rules.append({"type": "field", "network": "tcp,udp", "outboundTag": "BLOCK"})

    inbounds: list[dict[str, object]] = []
    for static in value.skeleton.inbounds:
        inbounds.append(
            {
                "tag": XRAY_RENDERER_CONSTANTS["tag"],
                "listen": static["listen"],
                "port": static["port"],
                "protocol": static["protocol"],
                "settings": {
                    "clients": [],
                    "decryption": XRAY_RENDERER_CONSTANTS["decryption"],
                },
                "streamSettings": {
                    "network": XRAY_RENDERER_CONSTANTS["network"],
                    "security": XRAY_RENDERER_CONSTANTS["security"],
                    "realitySettings": {
                        "show": XRAY_RENDERER_CONSTANTS["reality_show"],
                        "dest": value.deployment.reality_dest,
                        "xver": XRAY_RENDERER_CONSTANTS["reality_xver"],
                        "serverNames": list(value.deployment.reality_server_names),
                        "privateKey": value.reality.private_key,
                        "shortIds": list(value.reality.short_ids),
                    },
                },
            }
        )

    content: dict[str, object] = {
        "log": {"loglevel": value.deployment.xray_log_level},
        "inbounds": inbounds,
        "outbounds": outbounds,
        "routing": {
            "domainStrategy": XRAY_RENDERER_CONSTANTS["domain_strategy"],
            "rules": rules,
        },
    }
    return CandidateConfig(content, "xray-full-v1")


def validate_repo_owned_xray_config(config: Mapping[str, object]) -> tuple[str, ...]:
    """Validate the complete repository-owned Xray file shape.

    The returned messages intentionally contain only safe categories and paths;
    they never include values from Reality or outbound credentials.
    """

    try:
        _validate_repo_owned_xray_config(config)
    except XrayCompositionError as exc:
        return (str(exc),)
    return ()


def repo_owned_xray_projection(config: Mapping[str, object]) -> str:
    """Return an order-aware, key-order-independent repo-owned projection."""

    errors = validate_repo_owned_xray_config(config)
    if errors:
        raise XrayCompositionError(errors[0])
    return json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def repo_owned_projection_errors(
    expected: Mapping[str, object], observed: Mapping[str, object]
) -> tuple[str, ...]:
    """Compare two complete repo-owned files without exposing field values."""

    expected_errors = validate_repo_owned_xray_config(expected)
    if expected_errors:
        return ("candidate repo-owned Xray config is invalid",)
    observed_errors = validate_repo_owned_xray_config(observed)
    if observed_errors:
        return ("observed repo-owned Xray config is invalid",)

    mismatches = tuple(
        f"repo-owned {section} mismatch"
        for section in ("log", "inbounds", "outbounds", "routing")
        if expected[section] != observed[section]
    )
    if mismatches:
        return mismatches
    if repo_owned_xray_projection(expected) != repo_owned_xray_projection(observed):
        return ("repo-owned Xray projection mismatch",)
    return ()


def _validate_repo_owned_xray_config(config: Mapping[str, object]) -> None:
    if not isinstance(config, Mapping):
        raise XrayCompositionError("repo-owned Xray config must be an object")
    _keys(
        config,
        {"log", "inbounds", "outbounds", "routing"},
        "repo-owned Xray config contains unknown top-level fields",
    )

    log = _mapping(config["log"], "repo-owned Xray log is malformed")
    _keys(log, {"loglevel"}, "repo-owned Xray log contains unknown fields")
    _normalize_log_level(log["loglevel"])

    inbounds = config["inbounds"]
    if not isinstance(inbounds, list) or not inbounds:
        raise XrayCompositionError("repo-owned Xray inbounds are malformed")
    for inbound_value in inbounds:
        inbound = _mapping(inbound_value, "repo-owned Xray inbound is malformed")
        _keys(
            inbound,
            {"tag", "listen", "port", "protocol", "settings", "streamSettings"},
            "repo-owned Xray inbound contains unknown fields",
        )
        if inbound["tag"] != XRAY_RENDERER_CONSTANTS["tag"]:
            raise XrayCompositionError("repo-owned Xray inbound tag is invalid")
        _nonblank_string(inbound["listen"], "repo-owned Xray inbound listen is invalid")
        _valid_port(inbound["port"], "repo-owned Xray inbound port is invalid")
        _nonblank_string(inbound["protocol"], "repo-owned Xray inbound protocol is invalid")

        settings = _mapping(inbound["settings"], "repo-owned Xray inbound settings are malformed")
        _keys(
            settings,
            {"clients", "decryption"},
            "repo-owned Xray inbound settings contain unknown fields",
        )
        if settings["clients"] != []:
            raise XrayCompositionError("repo-owned Xray clients skeleton is invalid")
        if settings["decryption"] != XRAY_RENDERER_CONSTANTS["decryption"]:
            raise XrayCompositionError("repo-owned Xray decryption is invalid")

        stream = _mapping(
            inbound["streamSettings"], "repo-owned Xray streamSettings are malformed"
        )
        _keys(
            stream,
            {"network", "security", "realitySettings"},
            "repo-owned Xray streamSettings contain unknown fields",
        )
        if stream["network"] != XRAY_RENDERER_CONSTANTS["network"]:
            raise XrayCompositionError("repo-owned Xray network is invalid")
        if stream["security"] != XRAY_RENDERER_CONSTANTS["security"]:
            raise XrayCompositionError("repo-owned Xray security is invalid")
        reality = _mapping(
            stream["realitySettings"], "repo-owned Xray Reality settings are malformed"
        )
        _keys(
            reality,
            {"show", "dest", "xver", "serverNames", "privateKey", "shortIds"},
            "repo-owned Xray Reality settings contain unknown fields",
        )
        if reality["show"] is not XRAY_RENDERER_CONSTANTS["reality_show"]:
            raise XrayCompositionError("repo-owned Xray Reality show is invalid")
        if reality["xver"] != XRAY_RENDERER_CONSTANTS["reality_xver"]:
            raise XrayCompositionError("repo-owned Xray Reality xver is invalid")
        _nonblank_string(reality["dest"], "repo-owned Xray Reality dest is invalid")
        _string_list(
            reality["serverNames"],
            "repo-owned Xray Reality serverNames are invalid",
        )
        _nonblank_string(
            reality["privateKey"], "repo-owned Xray Reality identity is invalid"
        )
        _string_list(reality["shortIds"], "repo-owned Xray Reality identity is invalid")

    outbounds = config["outbounds"]
    if not isinstance(outbounds, list) or not outbounds:
        raise XrayCompositionError("repo-owned Xray outbounds are malformed")
    outbound_tags: set[str] = set()
    for index, outbound_value in enumerate(outbounds):
        outbound = _mapping(outbound_value, "repo-owned Xray outbound is malformed")
        tag = outbound.get("tag")
        if not isinstance(tag, str) or not tag.strip() or tag in outbound_tags:
            raise XrayCompositionError("repo-owned Xray outbound tag is invalid")
        if tag == "DIRECT":
            raise XrayCompositionError("DIRECT fallback is not permitted")
        outbound_tags.add(tag)
        if index == 0:
            _keys(
                outbound,
                {"tag", "protocol"},
                "repo-owned Xray BLOCK outbound is malformed",
            )
            if tag != XRAY_RENDERER_CONSTANTS["block_tag"] or outbound["protocol"] != "blackhole":
                raise XrayCompositionError("repo-owned Xray BLOCK outbound is invalid")
            continue

        _keys(
            outbound,
            {"tag", "protocol", "settings"},
            "repo-owned Xray outbound contains unknown fields",
        )
        if outbound["protocol"] != "socks":
            raise XrayCompositionError("unsupported repo-owned Xray outbound protocol")
        outbound_settings = _mapping(
            outbound["settings"], "repo-owned Xray outbound settings are malformed"
        )
        _keys(
            outbound_settings,
            {"servers"},
            "repo-owned Xray outbound settings contain unknown fields",
        )
        servers = outbound_settings["servers"]
        if not isinstance(servers, list) or len(servers) != 1:
            raise XrayCompositionError("repo-owned Xray outbound servers are malformed")
        server = _mapping(servers[0], "repo-owned Xray outbound server is malformed")
        _keys(server, {"address", "port", "users"}, "repo-owned Xray server is malformed")
        _nonblank_string(server["address"], "repo-owned Xray outbound host is invalid")
        _valid_port(server["port"], "repo-owned Xray outbound port is invalid")
        users = server["users"]
        if not isinstance(users, list) or len(users) != 1:
            raise XrayCompositionError("repo-owned Xray outbound users are malformed")
        user = _mapping(users[0], "repo-owned Xray outbound credential is malformed")
        _keys(user, {"username", "password"}, "repo-owned Xray credential is malformed")
        _nonblank_string(user["username"], "repo-owned Xray credential is invalid")
        _nonblank_string(user["password"], "repo-owned Xray credential is invalid")

    if XRAY_RENDERER_CONSTANTS["block_tag"] not in outbound_tags:
        raise XrayCompositionError("repo-owned Xray BLOCK outbound is missing")

    routing = _mapping(config["routing"], "repo-owned Xray routing is malformed")
    _keys(routing, {"domainStrategy", "rules"}, "repo-owned Xray routing contains unknown fields")
    if routing["domainStrategy"] != XRAY_RENDERER_CONSTANTS["domain_strategy"]:
        raise XrayCompositionError("repo-owned Xray domainStrategy is invalid")
    rules = routing["rules"]
    if not isinstance(rules, list) or len(rules) < 2:
        raise XrayCompositionError("repo-owned Xray routing rules are malformed")
    private = {"type": "field", "ip": ["geoip:private"], "outboundTag": "BLOCK"}
    fallback = {"type": "field", "network": "tcp,udp", "outboundTag": "BLOCK"}
    if rules[0] != private:
        raise XrayCompositionError("repo-owned Xray private BLOCK rule must be first")
    if rules[-1] != fallback:
        raise XrayCompositionError("repo-owned Xray tcp,udp BLOCK rule must be last")
    principals: set[str] = set()
    for rule_value in rules[1:-1]:
        rule = _mapping(rule_value, "repo-owned Xray routing rule is malformed")
        _keys(rule, {"type", "user", "outboundTag"}, "repo-owned Xray route is malformed")
        if rule["type"] != "field":
            raise XrayCompositionError("repo-owned Xray route type is invalid")
        users = rule["user"]
        if not isinstance(users, list) or len(users) != 1 or not isinstance(users[0], str):
            raise XrayCompositionError("repo-owned Xray route principal is invalid")
        principal = users[0]
        if not principal.strip() or principal in principals:
            raise XrayCompositionError("repo-owned Xray route principal is invalid")
        principals.add(principal)
        target = rule["outboundTag"]
        if (
            not isinstance(target, str)
            or not target.strip()
            or target == XRAY_RENDERER_CONSTANTS["block_tag"]
            or target not in outbound_tags
        ):
            raise XrayCompositionError("repo-owned Xray route references missing outbound")
        if target == "DIRECT":
            raise XrayCompositionError("DIRECT fallback is not permitted")


def _normalize_log_level(value: object) -> str:
    if not isinstance(value, str):
        raise XrayCompositionError("XRAY_LOG_LEVEL is invalid")
    normalized = value.strip().lower()
    if normalized not in _ALLOWED_LOG_LEVELS:
        raise XrayCompositionError("XRAY_LOG_LEVEL is invalid")
    return normalized


def _string_list(value: object, message: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ) or (not allow_empty and not value):
        raise XrayCompositionError(message)
    return value


def _valid_port(value: object, message: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
        raise XrayCompositionError(message)
    return value
