"""Immutable, deterministic Mihomo full-config projection boundary (ADR-023)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from backend.app.providers.base import DesiredForwarderState


class MihomoProjectionError(ValueError):
    """Secret-safe fail-closed projection error."""


@dataclass(frozen=True, slots=True)
class TransportMaterialization:
    owner_record_id: int
    provider_code: str
    source_revision: int
    cache_identity: str
    content_hash: str
    freshness_deadline: datetime
    content: Mapping[str, object]


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _validate_materialization(item: object) -> TransportMaterialization:
    if not isinstance(item, TransportMaterialization):
        raise MihomoProjectionError("MIHOMO_TRANSPORT_MATERIALIZATION_INVALID")
    if item.owner_record_id <= 0 or not item.provider_code.strip():
        raise MihomoProjectionError("MIHOMO_TRANSPORT_IDENTITY_INVALID")
    if item.source_revision <= 0 or not item.cache_identity or not item.content_hash:
        raise MihomoProjectionError("MIHOMO_TRANSPORT_PROOF_MISSING")
    if item.freshness_deadline <= datetime.now(item.freshness_deadline.tzinfo):
        raise MihomoProjectionError("MIHOMO_TRANSPORT_CACHE_STALE")
    actual_hash = hashlib.sha256(_canonical(item.content).encode()).hexdigest()
    if actual_hash != item.content_hash:
        raise MihomoProjectionError("MIHOMO_TRANSPORT_CACHE_HASH_MISMATCH")
    return item


def compose_mihomo_document(desired: DesiredForwarderState) -> tuple[dict[str, object], str]:
    """Compose the complete repo-owned document from the supplied snapshot only."""
    if not isinstance(desired, DesiredForwarderState):
        raise MihomoProjectionError("MIHOMO_DESIRED_SNAPSHOT_INVALID")
    listener_keys = tuple(desired.listeners)
    if len(listener_keys) != len(set(listener_keys)):
        raise MihomoProjectionError("MIHOMO_DUPLICATE_LISTENER_IDENTITY")
    proxies = list(desired.proxies)
    proxy_names = [str(proxy.get("name", "")) for proxy in proxies]
    if not all(proxy_names) or len(proxy_names) != len(set(proxy_names)):
        raise MihomoProjectionError("MIHOMO_DUPLICATE_PROXY_IDENTITY")
    materials = [_validate_materialization(item) for item in desired.transport_materializations]
    owners = [item.owner_record_id for item in materials]
    if len(owners) != len(set(owners)):
        raise MihomoProjectionError("MIHOMO_DUPLICATE_TRANSPORT_IDENTITY")
    document: dict[str, object] = {
        **dict(desired.deployment_constants),
        "listeners": [
            {"name": name, "listen": value} for name, value in sorted(desired.listeners.items())
        ],
        "proxies": proxies,
        "proxy-groups": list(desired.proxy_groups),
        "rules": list(desired.rules),
        "dns": dict(desired.dns),
        "policy": dict(desired.policy),
    }
    fingerprint = hashlib.sha256(_canonical(document).encode()).hexdigest()
    return document, fingerprint
