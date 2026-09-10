"""Covers the transport-sync failure transition that TASK-T5G flagged as
untested: `scheduler.sync_transport_capacity_and_inventory` must mark a
failing provider record DEGRADED and audit the transition exactly once,
without ever raising out of the batch (one bad provider must not stop the
rest of the scheduler tick).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Table, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.database import Base
from backend.app.models import (
    AuditLog,
    ProviderStatus,
    TransportProviderKind,
    TransportProviderRecord,
)
from backend.app.workers.scheduler import sync_transport_capacity_and_inventory


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables: list[Table] = [
        Base.metadata.tables["transport_providers"],
        Base.metadata.tables["audit_logs"],
    ]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session
    Base.metadata.drop_all(engine, tables=tables)
    engine.dispose()


class RaisingTransportProvider:
    def sync_nodes(self) -> None:
        raise RuntimeError("simulated transport provider outage")


class FakeRegistry:
    def __init__(self, transport: object) -> None:
        self.transport = transport


def add_healthy_record(db: Session, code: str = "PROV_1") -> TransportProviderRecord:
    record = TransportProviderRecord(
        code=code,
        slug=code.lower(),
        name=code,
        kind=TransportProviderKind.SUBSCRIPTION,
        secret_ref=f"transport/{code}/secret",
        status=ProviderStatus.HEALTHY,
        enabled=True,
    )
    db.add(record)
    db.commit()
    return record


def test_provider_failure_marks_record_degraded_and_audits_once(db_session: Session) -> None:
    record = add_healthy_record(db_session)
    registry = FakeRegistry(RaisingTransportProvider())

    completed = sync_transport_capacity_and_inventory(db_session, registry)  # type: ignore[arg-type]

    assert completed == 0
    refreshed = db_session.get(TransportProviderRecord, record.id)
    assert refreshed is not None
    assert refreshed.status is ProviderStatus.DEGRADED
    audits = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "TRANSPORT_PROVIDER_SYNC_FAILED")
    ).all()
    assert len(audits) == 1


def test_repeated_failure_does_not_duplicate_audit_log(db_session: Session) -> None:
    add_healthy_record(db_session)
    registry = FakeRegistry(RaisingTransportProvider())

    sync_transport_capacity_and_inventory(db_session, registry)  # type: ignore[arg-type]
    sync_transport_capacity_and_inventory(db_session, registry)  # type: ignore[arg-type]

    audits = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "TRANSPORT_PROVIDER_SYNC_FAILED")
    ).all()
    # Only the HEALTHY -> DEGRADED transition is audited, not every
    # subsequent failure while it stays DEGRADED.
    assert len(audits) == 1


def test_disabled_or_non_subscription_records_are_skipped(db_session: Session) -> None:
    disabled = TransportProviderRecord(
        code="PROV_DISABLED",
        slug="prov-disabled",
        name="Disabled",
        kind=TransportProviderKind.SUBSCRIPTION,
        secret_ref="transport/disabled/secret",
        status=ProviderStatus.HEALTHY,
        enabled=False,
    )
    self_hosted = TransportProviderRecord(
        code="PROV_SELF",
        slug="prov-self",
        name="Self Hosted",
        kind=TransportProviderKind.SELF_HOSTED,
        secret_ref="transport/self/secret",
        status=ProviderStatus.HEALTHY,
        enabled=True,
    )
    db_session.add_all([disabled, self_hosted])
    db_session.commit()
    registry = FakeRegistry(RaisingTransportProvider())

    completed = sync_transport_capacity_and_inventory(db_session, registry)  # type: ignore[arg-type]

    assert completed == 0
    refreshed_disabled = db_session.get(TransportProviderRecord, disabled.id)
    refreshed_self_hosted = db_session.get(TransportProviderRecord, self_hosted.id)
    assert refreshed_disabled is not None
    assert refreshed_disabled.status is ProviderStatus.HEALTHY
    assert refreshed_self_hosted is not None
    assert refreshed_self_hosted.status is ProviderStatus.HEALTHY
