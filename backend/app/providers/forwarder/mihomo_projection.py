"""Immutable, deterministic Mihomo full-config projection boundary (ADR-023)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType

from backend.app.providers.base import DesiredForwarderState


class MihomoProjectionError(ValueError):
    """Secret-safe fail-closed projection error."""


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


def _validate_sections(desired: DesiredForwarderState) -> None:
    if desired.listeners:
        raise MihomoProjectionError("MIHOMO_LEGACY_LISTENERS_UNSUPPORTED")
    if desired.policy:
        raise MihomoProjectionError("MIHOMO_POLICY_UNSUPPORTED")
    if not desired.listener_specs:
        raise MihomoProjectionError("MIHOMO_LISTENER_SPEC_REQUIRED")
    listener_names: set[str] = set()
    listener_bindings: set[tuple[str, int]] = set()
    for listener in desired.listener_specs:
        item = _mapping(listener, "MIHOMO_LISTENER_INVALID")
        name, kind, address, port, target = (
            item.get("name"),
            item.get("type"),
            item.get("listen"),
            item.get("port"),
            item.get("proxy"),
        )
        if not (
            isinstance(name, str)
            and isinstance(kind, str)
            and isinstance(address, str)
            and isinstance(target, str)
        ):
            raise MihomoProjectionError("MIHOMO_LISTENER_INVALID")
        if (
            kind not in {"socks", "http", "mixed"}
            or not isinstance(port, int)
            or not 1 <= port <= 65535
        ):
            raise MihomoProjectionError("MIHOMO_LISTENER_INVALID")
        if name in listener_names or (address, port) in listener_bindings:
            raise MihomoProjectionError("MIHOMO_LISTENER_DUPLICATE")
        listener_names.add(name)
        listener_bindings.add((address, port))
    for proxy in desired.proxies:
        proxy = _mapping(proxy, "MIHOMO_PROXY_INVALID")
        if not isinstance(proxy.get("name"), str) or not isinstance(proxy.get("type"), str):
            raise MihomoProjectionError("MIHOMO_PROXY_INVALID")
    names = [proxy["name"] for proxy in desired.proxies]
    if len(names) != len(set(names)):
        raise MihomoProjectionError("MIHOMO_DUPLICATE_PROXY_IDENTITY")
    for group in desired.proxy_groups:
        group = _mapping(group, "MIHOMO_PROXY_GROUP_INVALID")
        if not isinstance(group.get("name"), str) or not isinstance(group.get("type"), str):
            raise MihomoProjectionError("MIHOMO_PROXY_GROUP_INVALID")
        refs = group.get("proxies")
        if not isinstance(refs, tuple) or any(ref not in names for ref in refs):
            raise MihomoProjectionError("MIHOMO_PROXY_GROUP_REFERENCE_INVALID")
    group_names = {
        group.get("name") for group in desired.proxy_groups if isinstance(group.get("name"), str)
    }
    allowed_targets = set(names) | group_names | {"BLOCK"}
    for listener in desired.listener_specs:
        if listener.get("proxy") not in allowed_targets:
            raise MihomoProjectionError("MIHOMO_LISTENER_TARGET_INVALID")
    fallback_rules: list[Mapping[str, object]] = []
    supported_rules = {"DOMAIN", "DOMAIN-SUFFIX", "IP-CIDR"}
    for rule in desired.rules:
        rule = _mapping(rule, "MIHOMO_RULE_INVALID")
        match = rule.get("match")
        target = rule.get("target")
        if not isinstance(match, str) or not isinstance(target, str):
            raise MihomoProjectionError("MIHOMO_RULE_INVALID")
        if match.upper() != "MATCH":
            value = rule.get("value")
            if match.upper() not in supported_rules or not isinstance(value, str) or not value:
                raise MihomoProjectionError("MIHOMO_RULE_TYPE_UNSUPPORTED")
        elif rule.get("value") not in (None, ""):
            raise MihomoProjectionError("MIHOMO_MATCH_VALUE_INVALID")
        if match.upper() == "MATCH":
            fallback_rules.append(rule)
        if match.upper() == "MATCH" and target.upper() == "DIRECT":
            raise MihomoProjectionError("MIHOMO_DIRECT_FALLBACK_FORBIDDEN")
    if len(fallback_rules) != 1:
        raise MihomoProjectionError("MIHOMO_FALLBACK_RULE_INVALID")
    fallback_target = fallback_rules[0].get("target")
    if not isinstance(fallback_target, str) or fallback_target.upper() != "BLOCK":
        raise MihomoProjectionError("MIHOMO_FALLBACK_MUST_BLOCK")
    if desired.rules[-1] is not fallback_rules[0]:
        raise MihomoProjectionError("MIHOMO_FALLBACK_NOT_TERMINAL")
    for rule in desired.rules:
        match = rule.get("match")
        target = rule.get("target")
        if isinstance(match, str) and match.upper() != "MATCH" and target not in allowed_targets:
            raise MihomoProjectionError("MIHOMO_RULE_TARGET_INVALID")
    _mapping(desired.dns, "MIHOMO_DNS_INVALID")
    _mapping(desired.policy, "MIHOMO_POLICY_INVALID")
    allowed = {"mode", "mixed-port", "allow-lan", "log-level", "api-secret-ref"}
    if any(key not in allowed for key in desired.deployment_constants):
        raise MihomoProjectionError("MIHOMO_DEPLOYMENT_CONSTANT_UNSUPPORTED")


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
        "external-controller": "127.0.0.1:9090",
        **dict(desired.deployment_constants),
    }

    def serialize_rule(rule: Mapping[str, object]) -> str:
        match = rule.get("match")
        target = rule.get("target")
        if not isinstance(match, str) or not isinstance(target, str):
            raise MihomoProjectionError("MIHOMO_RULE_INVALID")
        if match.upper() == "MATCH":
            return f"MATCH,{target}"
        value = rule.get("value")
        if not isinstance(value, str) or not value:
            raise MihomoProjectionError("MIHOMO_RULE_INVALID")
        return f"{match.upper()},{value},{target}"

    raw_document: dict[str, object] = {
        **constants,
        "listeners": list(desired.listener_specs),
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
    document_value = _canonical(raw_document)
    if not isinstance(document_value, dict):
        raise MihomoProjectionError("MIHOMO_DOCUMENT_INVALID")
    document: dict[str, object] = document_value
    fingerprint = _digest(
        {
            "snapshot_revision": desired.snapshot_revision,
            "snapshot_identity": desired.snapshot_identity,
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
