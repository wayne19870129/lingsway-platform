"""Immutable, deterministic Mihomo full-config projection boundary (ADR-023)."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType

from backend.app.providers.base import DesiredForwarderState, ForwarderListenerDTO


class MihomoProjectionError(ValueError):
    """Secret-safe fail-closed projection error."""


_RESERVED_IDENTITIES = frozenset(
    {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "PASS-RULE", "COMPATIBLE", "BLOCK"}
)
_RESERVED_IDENTITY_KEYS = frozenset(identity.casefold() for identity in _RESERVED_IDENTITIES)
_SUPPORTED_LOG_LEVELS = frozenset({"silent", "error", "warning", "info", "debug"})
_CANONICAL_BIND_ADDRESS = "127.0.0.1"


@dataclass(frozen=True, slots=True, repr=False)
class TransportMaterializationReference:
    owner_record_id: int
    provider_code: str
    source_revision: int
    cache_identity: str
    implementation: str = "subscription"

    def __repr__(self) -> str:
        return (
            "TransportMaterializationReference("
            f"owner_record_id={self.owner_record_id!r}, provider_code={self.provider_code!r}, "
            f"source_revision={self.source_revision!r}, cache_identity=<redacted>, "
            f"implementation={self.implementation!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class TransportMaterialization:
    owner_record_id: int
    provider_code: str
    source_revision: int
    cache_identity: str
    content_hash: str
    freshness_deadline: datetime
    content: Mapping[str, object]
    implementation: str = "subscription"

    def __post_init__(self) -> None:
        if not isinstance(self.content, Mapping):
            raise TypeError("transport materialization content must be a mapping")

        def freeze(value: object) -> object:
            if isinstance(value, Mapping):
                return MappingProxyType({str(key): freeze(item) for key, item in value.items()})
            if isinstance(value, (list, tuple)):
                return tuple(freeze(item) for item in value)
            if isinstance(value, (str, int, float, bool, type(None))):
                return value
            raise TypeError("transport materialization contains unsupported value")

        object.__setattr__(self, "content", freeze(self.content))

    def __repr__(self) -> str:
        return (
            "TransportMaterialization("
            f"owner_record_id={self.owner_record_id!r}, provider_code={self.provider_code!r}, "
            f"source_revision={self.source_revision!r}, cache_identity=<redacted>, "
            f"freshness_deadline={self.freshness_deadline!r}, content=<redacted>)"
        )


def _canonical(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise MihomoProjectionError("MIHOMO_NONCANONICAL_VALUE")


def _digest(value: object) -> str:
    try:
        encoded = json.dumps(_canonical(value), sort_keys=True, separators=(",", ":")).encode()
    except (TypeError, ValueError) as exc:
        raise MihomoProjectionError("MIHOMO_NONCANONICAL_VALUE") from exc
    return hashlib.sha256(encoded).hexdigest()


def _mapping(value: object, code: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise MihomoProjectionError(code)
    try:
        _canonical(value)
    except MihomoProjectionError:
        raise MihomoProjectionError(code) from None
    return value


def _validate_materialization(item: object) -> TransportMaterialization:
    if not isinstance(item, TransportMaterialization):
        raise MihomoProjectionError("MIHOMO_TRANSPORT_MATERIALIZATION_INVALID")
    if item.owner_record_id <= 0 or not item.provider_code.strip():
        raise MihomoProjectionError("MIHOMO_TRANSPORT_IDENTITY_INVALID")
    if item.source_revision <= 0 or not item.cache_identity or not item.content_hash:
        raise MihomoProjectionError("MIHOMO_TRANSPORT_PROOF_MISSING")
    if item.implementation != "subscription":
        raise MihomoProjectionError("MIHOMO_TRANSPORT_IMPLEMENTATION_UNSUPPORTED")
    if item.freshness_deadline <= datetime.now(UTC):
        raise MihomoProjectionError("MIHOMO_TRANSPORT_CACHE_STALE")
    if _digest(item.content) != item.content_hash:
        raise MihomoProjectionError("MIHOMO_TRANSPORT_CACHE_HASH_MISMATCH")
    return item


def _validate_controller_address(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MihomoProjectionError("MIHOMO_CONTROLLER_ADDRESS_INVALID")
    if any(char.isspace() or ord(char) < 32 for char in value):
        raise MihomoProjectionError("MIHOMO_CONTROLLER_ADDRESS_INVALID")
    host, separator, port_text = value.rpartition(":")
    if not separator or not host or not port_text:
        raise MihomoProjectionError("MIHOMO_CONTROLLER_ADDRESS_INVALID")
    if host.startswith("[") or host.endswith("]"):
        if not (host.startswith("[") and host.endswith("]")):
            raise MihomoProjectionError("MIHOMO_CONTROLLER_ADDRESS_INVALID")
        host = host[1:-1]
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise MihomoProjectionError("MIHOMO_CONTROLLER_ADDRESS_INVALID") from exc
    if address.is_loopback or address.is_unspecified:
        raise MihomoProjectionError("MIHOMO_CONTROLLER_ADDRESS_INVALID")
    if not port_text.isascii() or not port_text.isdecimal():
        raise MihomoProjectionError("MIHOMO_CONTROLLER_ADDRESS_INVALID")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise MihomoProjectionError("MIHOMO_CONTROLLER_ADDRESS_INVALID")
    if address.version == 6 and not value.startswith("["):
        raise MihomoProjectionError("MIHOMO_CONTROLLER_ADDRESS_INVALID")
    return value


def _validate_deployment_constants(constants: Mapping[str, object]) -> tuple[int, str]:
    allowed = {
        "mode",
        "mixed-port",
        "allow-lan",
        "log-level",
        "external-controller",
        "api-secret-ref",
        "api-secret-revision",
    }
    if any(key not in allowed for key in constants):
        raise MihomoProjectionError("MIHOMO_DEPLOYMENT_CONSTANT_UNSUPPORTED")

    mode = constants.get("mode", "rule")
    if not isinstance(mode, str) or mode != "rule":
        raise MihomoProjectionError("MIHOMO_MODE_INVALID")

    mixed_port = constants.get("mixed-port", 7890)
    if (
        isinstance(mixed_port, bool)
        or not isinstance(mixed_port, int)
        or not 1 <= mixed_port <= 65535
    ):
        raise MihomoProjectionError("MIHOMO_MIXED_PORT_INVALID")

    allow_lan = constants.get("allow-lan", False)
    if not isinstance(allow_lan, bool):
        raise MihomoProjectionError("MIHOMO_ALLOW_LAN_INVALID")

    log_level = constants.get("log-level", "warning")
    if not isinstance(log_level, str) or log_level not in _SUPPORTED_LOG_LEVELS:
        raise MihomoProjectionError("MIHOMO_LOG_LEVEL_INVALID")
    controller = _validate_controller_address(constants.get("external-controller"))
    return mixed_port, controller


def _validate_sections(desired: DesiredForwarderState) -> None:
    mixed_port, _ = _validate_deployment_constants(desired.deployment_constants)
    if desired.listeners:
        raise MihomoProjectionError("MIHOMO_LEGACY_LISTENERS_UNSUPPORTED")
    if desired.policy:
        raise MihomoProjectionError("MIHOMO_POLICY_UNSUPPORTED")
    if not desired.listener_specs:
        raise MihomoProjectionError("MIHOMO_LISTENER_SPEC_REQUIRED")
    listener_names: set[str] = set()
    listener_bindings: set[tuple[str, int]] = set()
    for listener in desired.listener_specs:
        if not isinstance(listener, ForwarderListenerDTO):
            raise MihomoProjectionError("MIHOMO_LISTENER_INVALID")
        name = listener.name.strip()
        kind = listener.listener_type
        address = listener.listen.strip()
        port = listener.port
        target = listener.proxy
        if not name or not address or not target:
            raise MihomoProjectionError("MIHOMO_LISTENER_INVALID")
        target = _validate_target(target, "MIHOMO_LISTENER_TARGET_INVALID")
        if any(ord(char) < 32 for char in f"{name}{address}{target}"):
            raise MihomoProjectionError("MIHOMO_LISTENER_INVALID")
        if kind not in {"socks", "http", "mixed"} or not 1 <= port <= 65535:
            raise MihomoProjectionError("MIHOMO_LISTENER_INVALID")
        if address == _CANONICAL_BIND_ADDRESS and port == mixed_port:
            raise MihomoProjectionError("MIHOMO_LISTENER_PORT_COLLISION")
        if name in listener_names or (address, port) in listener_bindings:
            raise MihomoProjectionError("MIHOMO_LISTENER_DUPLICATE")
        listener_names.add(name)
        listener_bindings.add((address, port))
    proxy_names: list[str] = []
    for proxy in desired.proxies:
        proxy = _mapping(proxy, "MIHOMO_PROXY_INVALID")
        name_obj = proxy.get("name")
        type_obj = proxy.get("type")
        if not isinstance(name_obj, str) or not isinstance(type_obj, str):
            raise MihomoProjectionError("MIHOMO_PROXY_INVALID")
        name = name_obj.strip()
        if not name or name != name_obj or _unsafe_component(name):
            raise MihomoProjectionError("MIHOMO_PROXY_INVALID")
        if name.casefold() in _RESERVED_IDENTITY_KEYS:
            raise MihomoProjectionError("MIHOMO_RESERVED_TARGET_IDENTITY")
        proxy_names.append(name)
    if len(proxy_names) != len(set(proxy_names)):
        raise MihomoProjectionError("MIHOMO_DUPLICATE_PROXY_IDENTITY")
    group_names: list[str] = []
    for group in desired.proxy_groups:
        group = _mapping(group, "MIHOMO_PROXY_GROUP_INVALID")
        name_obj = group.get("name")
        type_obj = group.get("type")
        if not isinstance(name_obj, str) or not isinstance(type_obj, str):
            raise MihomoProjectionError("MIHOMO_PROXY_GROUP_INVALID")
        name = name_obj.strip()
        if not name or name != name_obj or _unsafe_component(name):
            raise MihomoProjectionError("MIHOMO_PROXY_GROUP_INVALID")
        if name.casefold() in _RESERVED_IDENTITY_KEYS:
            raise MihomoProjectionError("MIHOMO_RESERVED_TARGET_IDENTITY")
        group_names.append(name)
        refs = group.get("proxies")
        if not isinstance(refs, tuple) or any(ref not in proxy_names for ref in refs):
            raise MihomoProjectionError("MIHOMO_PROXY_GROUP_REFERENCE_INVALID")
    if len(group_names) != len(set(group_names)):
        raise MihomoProjectionError("MIHOMO_DUPLICATE_PROXY_GROUP_IDENTITY")
    if set(proxy_names).intersection(group_names):
        raise MihomoProjectionError("MIHOMO_PROXY_GROUP_IDENTITY_COLLISION")
    allowed_targets = set(proxy_names) | set(group_names) | {"BLOCK"}
    for listener in desired.listener_specs:
        if listener.proxy not in allowed_targets:
            raise MihomoProjectionError("MIHOMO_LISTENER_TARGET_INVALID")
    fallback_rules: list[Mapping[str, object]] = []
    supported_rules = {"DOMAIN", "DOMAIN-SUFFIX", "IP-CIDR"}
    for rule in desired.rules:
        rule = _mapping(rule, "MIHOMO_RULE_INVALID")
        rule_match_obj = rule.get("match")
        rule_target_obj = rule.get("target")
        if not isinstance(rule_match_obj, str) or not isinstance(rule_target_obj, str):
            raise MihomoProjectionError("MIHOMO_RULE_INVALID")
        rule_match = rule_match_obj
        rule_target = _validate_target(rule_target_obj, "MIHOMO_RULE_TARGET_INVALID")
        if rule_match.upper() != "MATCH":
            value = rule.get("value")
            if rule_match.upper() not in supported_rules or not isinstance(value, str) or not value:
                raise MihomoProjectionError("MIHOMO_RULE_TYPE_UNSUPPORTED")
            _canonical_rule_value(rule_match.upper(), value)
        elif rule.get("value") not in (None, ""):
            raise MihomoProjectionError("MIHOMO_MATCH_VALUE_INVALID")
        if rule_match.upper() == "MATCH":
            fallback_rules.append(rule)
        if rule_match.upper() == "MATCH" and rule_target == "DIRECT":
            raise MihomoProjectionError("MIHOMO_DIRECT_FALLBACK_FORBIDDEN")
    if len(fallback_rules) != 1:
        raise MihomoProjectionError("MIHOMO_FALLBACK_RULE_INVALID")
    fallback_target_obj = fallback_rules[0].get("target")
    if not isinstance(fallback_target_obj, str) or fallback_target_obj != "BLOCK":
        raise MihomoProjectionError("MIHOMO_FALLBACK_MUST_BLOCK")
    if desired.rules[-1] is not fallback_rules[0]:
        raise MihomoProjectionError("MIHOMO_FALLBACK_NOT_TERMINAL")
    for rule in desired.rules:
        second_rule_match_obj = rule.get("match")
        second_rule_target_obj = rule.get("target")
        if not isinstance(second_rule_match_obj, str) or not isinstance(
            second_rule_target_obj, str
        ):
            raise MihomoProjectionError("MIHOMO_RULE_INVALID")
        second_rule_match = second_rule_match_obj
        second_rule_target = _validate_target(second_rule_target_obj, "MIHOMO_RULE_TARGET_INVALID")
        if second_rule_match.upper() != "MATCH" and second_rule_target not in allowed_targets:
            raise MihomoProjectionError("MIHOMO_RULE_TARGET_INVALID")
    _validate_dns_schema(desired.dns)
    _mapping(desired.policy, "MIHOMO_POLICY_INVALID")


def _validate_dns_schema(dns: Mapping[str, object]) -> None:
    for key, value in dns.items():
        if key in {"enable", "ipv6"} and isinstance(value, bool):
            continue
        if key == "nameserver" and isinstance(value, (list, tuple)):
            continue
        if key not in {"enable", "ipv6", "nameserver"} or not isinstance(
            value, (list, tuple, bool)
        ):
            raise MihomoProjectionError("MIHOMO_DNS_INVALID")
        raise MihomoProjectionError("MIHOMO_DNS_INVALID")


def _unsafe_component(value: str) -> bool:
    return any(char == "," or ord(char) < 32 for char in value)


def _validate_rule_component(value: str, error_code: str) -> str:
    if not value.strip() or _unsafe_component(value):
        raise MihomoProjectionError(error_code)
    return value.strip()


def _validate_target(value: str, error_code: str) -> str:
    if value.strip().casefold() == "block" and value != "BLOCK":
        raise MihomoProjectionError("MIHOMO_BLOCK_TARGET_INVALID")
    if value != value.strip() or not value or _unsafe_component(value):
        raise MihomoProjectionError(error_code)
    return value


def _canonical_domain(value: str) -> str:
    value = _validate_rule_component(value, "MIHOMO_RULE_VALUE_INVALID").lower()
    if value.startswith(".") or value.endswith(".") or ".." in value or " " in value:
        raise MihomoProjectionError("MIHOMO_RULE_VALUE_INVALID")
    try:
        value_bytes = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise MihomoProjectionError("MIHOMO_RULE_VALUE_INVALID") from exc
    if len(value_bytes) > 253:
        raise MihomoProjectionError("MIHOMO_RULE_VALUE_INVALID")
    labels = value.split(".")
    for label in labels:
        if not 1 <= len(label.encode("ascii")) <= 63:
            raise MihomoProjectionError("MIHOMO_RULE_VALUE_INVALID")
        if label[0] == "-" or label[-1] == "-":
            raise MihomoProjectionError("MIHOMO_RULE_VALUE_INVALID")
        if any(
            not ("a" <= char <= "z" or "A" <= char <= "Z" or "0" <= char <= "9" or char == "-")
            for char in label
        ):
            raise MihomoProjectionError("MIHOMO_RULE_VALUE_INVALID")
    return value


def _canonical_rule_value(kind: str, value: str) -> str:
    if kind in {"DOMAIN", "DOMAIN-SUFFIX"}:
        return _canonical_domain(value)
    if kind == "IP-CIDR":
        value = _validate_rule_component(value, "MIHOMO_RULE_VALUE_INVALID")
        try:
            return str(ipaddress.ip_network(value, strict=False))
        except ValueError as exc:
            raise MihomoProjectionError("MIHOMO_RULE_VALUE_INVALID") from exc
    raise MihomoProjectionError("MIHOMO_RULE_TYPE_UNSUPPORTED")


def _controller_identity(desired: DesiredForwarderState) -> tuple[str, int]:
    ref = desired.deployment_constants.get("api-secret-ref")
    revision = desired.deployment_constants.get("api-secret-revision")
    if not isinstance(ref, str) or not ref or ref != ref.strip() or _unsafe_component(ref):
        raise MihomoProjectionError("MIHOMO_CONTROLLER_SECRET_REF_INVALID")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
        raise MihomoProjectionError("MIHOMO_CONTROLLER_SECRET_REVISION_INVALID")
    return ref, revision


def compose_mihomo_document(desired: DesiredForwarderState) -> tuple[dict[str, object], str]:
    """Compose the complete repo-owned document from the supplied snapshot only."""
    if not isinstance(desired, DesiredForwarderState):
        raise MihomoProjectionError("MIHOMO_DESIRED_SNAPSHOT_INVALID")
    materials = [_validate_materialization(item) for item in desired.transport_materializations]
    references = list(desired.transport_references)
    if len(materials) != len(references):
        raise MihomoProjectionError("MIHOMO_TRANSPORT_MATERIALIZATION_MISSING")
    material_by_owner = {item.owner_record_id: item for item in materials}
    if len(material_by_owner) != len(materials):
        raise MihomoProjectionError("MIHOMO_DUPLICATE_TRANSPORT_IDENTITY")
    reference_owners = [
        ref.owner_record_id
        for ref in references
        if isinstance(ref, TransportMaterializationReference)
    ]
    if len(reference_owners) != len(references) or len(set(reference_owners)) != len(
        reference_owners
    ):
        raise MihomoProjectionError("MIHOMO_DUPLICATE_TRANSPORT_REFERENCE")
    if set(material_by_owner) != set(reference_owners):
        raise MihomoProjectionError("MIHOMO_TRANSPORT_OWNERSHIP_MISMATCH")
    for ref in references:
        if not isinstance(ref, TransportMaterializationReference):
            raise MihomoProjectionError("MIHOMO_TRANSPORT_REFERENCE_INVALID")
        item = material_by_owner.get(ref.owner_record_id)
        if item is None or (
            item.provider_code,
            item.source_revision,
            item.cache_identity,
            item.implementation,
        ) != (ref.provider_code, ref.source_revision, ref.cache_identity, ref.implementation):
            raise MihomoProjectionError("MIHOMO_TRANSPORT_PROOF_MISMATCH")
    materialized_proxies: list[Mapping[str, object]] = []
    for item in materials:
        candidate = item.content.get("proxies")
        if candidate is None:
            raise MihomoProjectionError("MIHOMO_TRANSPORT_CONTENT_UNBOUND")
        if not isinstance(candidate, (tuple, list)):
            raise MihomoProjectionError("MIHOMO_TRANSPORT_CONTENT_INVALID")
        materialized_proxies.extend(
            _mapping(proxy, "MIHOMO_TRANSPORT_CONTENT_INVALID") for proxy in candidate
        )
    effective_proxies = tuple(desired.proxies) + tuple(materialized_proxies)
    effective_desired = DesiredForwarderState(
        listeners=desired.listeners,
        listener_specs=desired.listener_specs,
        proxies=effective_proxies,
        proxy_groups=desired.proxy_groups,
        rules=desired.rules,
        dns=desired.dns,
        policy=desired.policy,
        deployment_constants=desired.deployment_constants,
        snapshot_revision=desired.snapshot_revision,
        snapshot_identity=desired.snapshot_identity,
    )
    controller_secret_ref, controller_secret_revision = _controller_identity(desired)
    _validate_sections(effective_desired)
    constants = {
        "ipv6": False,
        "unified-delay": True,
        "tcp-concurrent": True,
        "mixed-port": 7890,
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "mode": "rule",
        "log-level": "warning",
        "external-controller": _validate_deployment_constants(desired.deployment_constants)[1],
        **dict(desired.deployment_constants),
    }

    def serialize_rule(rule: Mapping[str, object]) -> str:
        match = rule.get("match")
        target = rule.get("target")
        if not isinstance(match, str) or not isinstance(target, str):
            raise MihomoProjectionError("MIHOMO_RULE_INVALID")
        match_value = match.upper()
        target_value = _validate_target(target, "MIHOMO_RULE_TARGET_INVALID")
        runtime_target = "REJECT" if target_value == "BLOCK" else target_value
        if match_value == "MATCH":
            return f"MATCH,{runtime_target}"
        value = rule.get("value")
        if not isinstance(value, str) or not value:
            raise MihomoProjectionError("MIHOMO_RULE_INVALID")
        return f"{match_value},{_canonical_rule_value(match_value, value)},{runtime_target}"

    raw_document: dict[str, object] = {
        **constants,
        "listeners": [
            {
                "name": listener.name,
                "type": listener.listener_type,
                "listen": listener.listen,
                "port": listener.port,
                "proxy": "REJECT" if listener.proxy == "BLOCK" else listener.proxy,
            }
            for listener in desired.listener_specs
        ],
        "proxies": list(effective_proxies),
        "proxy-groups": list(desired.proxy_groups),
        "rules": [serialize_rule(rule) for rule in desired.rules],
        "dns": dict(desired.dns),
    }
    secret_ref = desired.deployment_constants.get("api-secret-ref")
    if not isinstance(secret_ref, str) or not secret_ref:
        raise MihomoProjectionError("MIHOMO_API_SECRET_REF_REQUIRED")
    # The opaque reference is an operation input only; it is never serialized.
    raw_document.pop("api-secret-ref", None)
    raw_document.pop("api-secret-revision", None)
    document_value = _canonical(raw_document)
    if not isinstance(document_value, dict):
        raise MihomoProjectionError("MIHOMO_DOCUMENT_INVALID")
    document: dict[str, object] = document_value
    fingerprint = _digest(
        {
            "snapshot_revision": desired.snapshot_revision,
            "snapshot_identity": desired.snapshot_identity,
            "controller_secret_ref": controller_secret_ref,
            "controller_secret_revision": controller_secret_revision,
            "document": document,
            "transport_proofs": [
                {
                    "owner_record_id": item.owner_record_id,
                    "provider_code": item.provider_code,
                    "source_revision": item.source_revision,
                    "cache_identity": item.cache_identity,
                    "content_hash": item.content_hash,
                    "freshness_deadline": item.freshness_deadline.isoformat(),
                }
                for item in materials
            ],
        }
    )
    return document, fingerprint
