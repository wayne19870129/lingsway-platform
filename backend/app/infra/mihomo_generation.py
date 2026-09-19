"""Canonical generation identity and append-only allocator."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import MihomoProjectionGeneration
from backend.app.providers.base import DesiredForwarderState

MANIFEST_VERSION = "1"


def canonical_manifest(value: Mapping[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def manifest_fingerprint(value: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_manifest(value).encode()).hexdigest()


def snapshot_identity(
    revision: int, desired_fingerprint: str, *, manifest_version: str = MANIFEST_VERSION
) -> str:
    if isinstance(revision, bool) or revision <= 0:
        raise ValueError("MIHOMO_GENERATION_REVISION_INVALID")
    if len(desired_fingerprint) != 64 or desired_fingerprint.lower() != desired_fingerprint:
        raise ValueError("MIHOMO_GENERATION_FINGERPRINT_INVALID")
    return f"mihomo:{manifest_version}:{revision}:{desired_fingerprint}"


def _manifest_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Mapping):
        return {str(key): _manifest_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_manifest_value(item) for item in value]
    if isinstance(value, set) or is_dataclass(value):
        if is_dataclass(value):
            return _manifest_value(asdict(value))  # type: ignore[arg-type]
        raise ValueError("MIHOMO_MANIFEST_UNSUPPORTED_VALUE")
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise ValueError("MIHOMO_MANIFEST_UNSUPPORTED_VALUE")


def build_projection_source_manifest(
    desired: DesiredForwarderState,
) -> dict[str, object]:
    proofs: list[dict[str, object]] = []
    for item in desired.transport_materializations:
        fields = (
            "owner_record_id",
            "provider_code",
            "source_revision",
            "cache_identity",
            "content_hash",
            "freshness_deadline",
        )
        if not all(hasattr(item, field) for field in fields):
            raise ValueError("MIHOMO_TRANSPORT_PROOF_MISSING")
        proofs.append({field: _manifest_value(getattr(item, field)) for field in fields})
    return {
        "manifest_version": MANIFEST_VERSION,
        "listeners": _manifest_value(desired.listeners),
        "listener_specs": _manifest_value(desired.listener_specs),
        "proxies": _manifest_value(desired.proxies),
        "proxy_groups": _manifest_value(desired.proxy_groups),
        "rules": _manifest_value(desired.rules),
        "dns": _manifest_value(desired.dns),
        "policy": _manifest_value(desired.policy),
        "transport_references": _manifest_value(desired.transport_references),
        "transport_proofs": proofs,
        "deployment_constants": _manifest_value(desired.deployment_constants),
    }


def allocate_generation(
    db: Session,
    manifest: Mapping[str, object],
    *,
    manifest_version: str = MANIFEST_VERSION,
) -> MihomoProjectionGeneration:
    fingerprint = manifest_fingerprint(manifest)
    latest = db.scalar(
        select(MihomoProjectionGeneration)
        .order_by(MihomoProjectionGeneration.revision.desc())
        .with_for_update()
    )
    if (
        latest is not None
        and latest.manifest_version == manifest_version
        and latest.desired_fingerprint == fingerprint
    ):
        return latest
    generation = MihomoProjectionGeneration(
        manifest_version=manifest_version,
        desired_fingerprint=fingerprint,
        created_at=datetime.now(UTC),
    )
    db.add(generation)
    db.flush()
    return generation
