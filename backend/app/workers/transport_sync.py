"""Database adapter for synchronizing transport provider inventory."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.models import (
    MihomoTransportMaterialization,
    ProviderStatus,
    TransportCapacityAlert,
    TransportEndpointRecord,
    TransportProviderRecord,
)
from backend.app.providers.base import TransportProvider


def refresh_provider_inventory(
    db: Session,
    provider_record: TransportProviderRecord,
    provider: TransportProvider,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now(UTC)
    provider.sync_nodes()
    endpoints = provider.list_endpoints()
    seen: set[str] = set()
    for endpoint in endpoints:
        seen.add(endpoint.external_id)
        record = db.scalar(
            select(TransportEndpointRecord).where(
                TransportEndpointRecord.provider_id == provider_record.id,
                TransportEndpointRecord.external_id == endpoint.external_id,
            )
        )
        if record is None:
            try:
                with db.begin_nested():
                    record = TransportEndpointRecord(
                        provider_id=provider_record.id,
                        external_id=endpoint.external_id,
                        name=endpoint.name,
                        region=endpoint.region,
                        protocol=endpoint.protocol,
                        host=endpoint.host,
                        port=endpoint.port,
                        transport=endpoint.transport,
                        tls_mode=endpoint.tls_mode,
                        auth_secret_ref=endpoint.auth_secret_ref or "",
                        status="AVAILABLE",
                        raw_metadata_json=json.dumps(
                            endpoint.raw_metadata or {}, separators=(",", ":")
                        ),
                    )
                    db.add(record)
                    db.flush()
            except IntegrityError:
                record = db.scalar(
                    select(TransportEndpointRecord).where(
                        TransportEndpointRecord.provider_id == provider_record.id,
                        TransportEndpointRecord.external_id == endpoint.external_id,
                    )
                )
                if record is None:
                    raise
        record.name = endpoint.name
        record.region = endpoint.region
        record.protocol = endpoint.protocol
        record.host = endpoint.host
        record.port = endpoint.port
        record.transport = endpoint.transport
        record.tls_mode = endpoint.tls_mode
        record.status = "AVAILABLE"
        record.raw_metadata_json = json.dumps(endpoint.raw_metadata or {}, separators=(",", ":"))
    stale_query = select(TransportEndpointRecord).where(
        TransportEndpointRecord.provider_id == provider_record.id
    )
    if seen:
        stale_query = stale_query.where(TransportEndpointRecord.external_id.not_in(seen))
    stale = db.scalars(stale_query)
    for record in stale:
        record.status = "REMOVED"
    capacity = provider.get_capacity()
    if capacity is None:
        provider_record.quota_bytes = None
        provider_record.used_bytes = None
        provider_record.measured_at = None
    else:
        provider_record.quota_bytes = capacity.quota_bytes
        provider_record.used_bytes = capacity.used_bytes
        provider_record.measured_at = capacity.measured_at
        record_capacity_alerts(db, provider_record)
    provider_record.status = ProviderStatus.HEALTHY if endpoints else ProviderStatus.DEGRADED
    provider_record.last_refresh_at = now
    provider_record.last_health_at = now
    proof = getattr(provider, "materialization_proof", lambda: None)()
    if proof is not None:
        cache_identity, content_hash, source_revision = proof
        receipt = db.scalar(
            select(MihomoTransportMaterialization)
            .where(MihomoTransportMaterialization.owner_record_id == provider_record.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        deadline = (
            capacity.expire_at
            if capacity is not None and capacity.expire_at
            else now + timedelta(hours=1)
        )
        if receipt is None:
            receipt = MihomoTransportMaterialization(owner_record_id=provider_record.id)
            db.add(receipt)
        receipt.provider_code = provider_record.code
        receipt.source_revision = source_revision
        receipt.cache_identity = cache_identity
        receipt.content_hash = content_hash
        receipt.freshness_deadline = deadline
        receipt.updated_at = now
    db.commit()
    return len(endpoints)


def record_capacity_alerts(db: Session, provider: TransportProviderRecord) -> None:
    if not provider.quota_bytes or provider.used_bytes is None:
        return
    percent = (provider.used_bytes * 100) // provider.quota_bytes
    for threshold, level in ((60, "REMINDER"), (80, "PROCURE"), (95, "EMERGENCY")):
        if percent < threshold:
            continue
        existing = db.scalar(
            select(TransportCapacityAlert).where(
                TransportCapacityAlert.provider_id == provider.id,
                TransportCapacityAlert.threshold == threshold,
                TransportCapacityAlert.quota_bytes == provider.quota_bytes,
            )
        )
        if existing is not None:
            continue
        try:
            with db.begin_nested():
                db.add(
                    TransportCapacityAlert(
                        provider_id=provider.id,
                        threshold=threshold,
                        level=level,
                        quota_bytes=provider.quota_bytes,
                        used_bytes=provider.used_bytes,
                    )
                )
                db.flush()
        except IntegrityError:
            recovered = db.scalar(
                select(TransportCapacityAlert).where(
                    TransportCapacityAlert.provider_id == provider.id,
                    TransportCapacityAlert.threshold == threshold,
                    TransportCapacityAlert.quota_bytes == provider.quota_bytes,
                )
            )
            if recovered is None:
                raise
