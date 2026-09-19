"""Canonical generation identity and append-only allocator."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import MihomoProjectionGeneration

MANIFEST_VERSION = "1"


def canonical_manifest(value: Mapping[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def manifest_fingerprint(value: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_manifest(value).encode()).hexdigest()


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
