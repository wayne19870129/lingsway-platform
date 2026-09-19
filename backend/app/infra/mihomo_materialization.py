"""Verification boundary for DB-anchored transport cache receipts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import MihomoTransportMaterialization
from backend.app.providers.forwarder.mihomo_projection import TransportMaterialization


class MaterializationVerificationError(ValueError):
    """A receipt or its cache proof cannot be trusted."""


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedMaterialization:
    owner_record_id: int
    provider_code: str
    source_revision: int
    cache_identity: str
    content_hash: str
    freshness_deadline: datetime
    content: bytes

    def __repr__(self) -> str:
        return (
            "VerifiedMaterialization("
            f"owner_record_id={self.owner_record_id!r}, provider_code={self.provider_code!r}, "
            f"source_revision={self.source_revision!r}, cache_identity=<redacted>, "
            f"content_hash={self.content_hash!r}, freshness_deadline={self.freshness_deadline!r}, "
            "content=<redacted>)"
        )


def verify_materialization(
    db: Session,
    owner_record_id: int,
    cache_path: Path,
    provider_code: str,
    *,
    source_revision: int,
) -> VerifiedMaterialization:
    receipt = db.scalar(
        select(MihomoTransportMaterialization)
        .where(MihomoTransportMaterialization.owner_record_id == owner_record_id)
        .with_for_update()
    )
    if receipt is None:
        raise MaterializationVerificationError("MIHOMO_MATERIALIZATION_RECEIPT_MISSING")
    if (
        receipt.provider_code != provider_code
        or receipt.source_revision != source_revision
        or receipt.cache_identity != str(cache_path)
    ):
        raise MaterializationVerificationError("MIHOMO_MATERIALIZATION_IDENTITY_MISMATCH")
    try:
        content = cache_path.read_bytes()
    except OSError as exc:
        raise MaterializationVerificationError("MIHOMO_MATERIALIZATION_CACHE_MISSING") from exc
    if hashlib.sha256(content).hexdigest() != receipt.content_hash:
        raise MaterializationVerificationError("MIHOMO_MATERIALIZATION_HASH_MISMATCH")
    deadline = receipt.freshness_deadline
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    if deadline <= datetime.now(UTC):
        raise MaterializationVerificationError("MIHOMO_MATERIALIZATION_EXPIRED")
    return VerifiedMaterialization(
        receipt.owner_record_id,
        receipt.provider_code,
        receipt.source_revision,
        receipt.cache_identity,
        receipt.content_hash,
        receipt.freshness_deadline,
        content,
    )


def load_transport_materialization(
    verified: VerifiedMaterialization,
) -> TransportMaterialization:
    try:
        document = yaml.safe_load(verified.content)
    except yaml.YAMLError as exc:
        raise MaterializationVerificationError("MIHOMO_MATERIALIZATION_YAML_INVALID") from exc
    if not isinstance(document, dict) or not isinstance(document.get("proxies"), list):
        raise MaterializationVerificationError("MIHOMO_MATERIALIZATION_CONTENT_INVALID")
    return TransportMaterialization(
        owner_record_id=verified.owner_record_id,
        provider_code=verified.provider_code,
        source_revision=verified.source_revision,
        cache_identity=verified.cache_identity,
        content_hash=verified.content_hash,
        freshness_deadline=verified.freshness_deadline,
        content=document,
    )
