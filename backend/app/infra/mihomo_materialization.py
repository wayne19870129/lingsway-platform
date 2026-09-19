"""Verification boundary for DB-anchored transport cache receipts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import MihomoTransportMaterialization


class MaterializationVerificationError(ValueError):
    """A receipt or its cache proof cannot be trusted."""


@dataclass(frozen=True, slots=True)
class VerifiedMaterialization:
    owner_record_id: int
    provider_code: str
    source_revision: int
    cache_identity: str
    content_hash: str
    freshness_deadline: datetime
    content: bytes


def verify_materialization(
    db: Session,
    owner_record_id: int,
    cache_path: Path,
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
    if receipt.source_revision != source_revision:
        raise MaterializationVerificationError("MIHOMO_MATERIALIZATION_SOURCE_STALE")
    try:
        content = cache_path.read_bytes()
    except OSError as exc:
        raise MaterializationVerificationError(
            "MIHOMO_MATERIALIZATION_CACHE_MISSING"
        ) from exc
    if hashlib.sha256(content).hexdigest() != receipt.content_hash:
        raise MaterializationVerificationError("MIHOMO_MATERIALIZATION_HASH_MISMATCH")
    if receipt.freshness_deadline <= datetime.now(UTC):
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
