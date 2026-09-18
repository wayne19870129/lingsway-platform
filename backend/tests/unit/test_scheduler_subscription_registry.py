from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Table, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.database import Base
from backend.app.core.secrets import SecretSnapshot
from backend.app.models import (
    AuditLog,
    ProviderStatus,
    TransportProviderKind,
    TransportProviderRecord,
)
from backend.app.providers.transport.resolver import (
    SubscriptionTransportResolver,
    TransportProviderDescriptor,
)
from backend.app.workers import scheduler


class FakeProvider:
    def __init__(self, code: str, fail: bool = False) -> None:
        self.code = code
        self.fail = fail
        self.sync_calls = 0

    def sync_nodes(self) -> None:
        self.sync_calls += 1
        if self.fail:
            raise RuntimeError("provider failure")

    def list_endpoints(self) -> list[object]:
        return []

    def get_capacity(self) -> None:
        return None


class FakeResolver:
    def __init__(self, providers: dict[int, FakeProvider]) -> None:
        self.providers = providers
        self.descriptors: list[TransportProviderDescriptor] = []

    def resolve(self, descriptor: TransportProviderDescriptor, loader: object) -> FakeProvider:
        assert callable(loader)
        snapshot = loader(descriptor.secret_ref, "TRANSPORT_SUBSCRIPTION_URL")
        assert isinstance(snapshot, SecretSnapshot)
        self.descriptors.append(descriptor)
        return self.providers[descriptor.record_id]


class SecretSession:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def close(self) -> None:
        self.events.append("secret-close")


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


def _record(db: Session, code: str) -> TransportProviderRecord:
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


def test_scheduler_resolves_each_enabled_record_and_isolates_failure(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_a = _record(db_session, "A")
    record_b = _record(db_session, "B")
    provider_a = FakeProvider("A", fail=True)
    provider_b = FakeProvider("B")
    resolver = FakeResolver({record_a.id: provider_a, record_b.id: provider_b})
    events: list[str] = []

    monkeypatch.setattr(
        scheduler,
        "get_settings",
        lambda: type("Settings", (), {"transport_provider_mode": "subscription"})(),
    )

    def secret_factory() -> SecretSession:
        events.append("secret-open")
        return SecretSession(events)

    def load_secret(_db: object, _ref: str, _purpose: str) -> SecretSnapshot:
        events.append("secret-read")
        return SecretSnapshot("https://provider.invalid/private", 1)

    monkeypatch.setattr(scheduler, "reveal_secret_snapshot_for_purpose", load_secret)
    def refresh(_db: object, record: TransportProviderRecord, provider: FakeProvider) -> None:
        events.append(f"sync-{record.code}")
        provider.sync_nodes()

    monkeypatch.setattr(scheduler, "refresh_provider_inventory", refresh)
    registry = type("Registry", (), {"transport_resolver": resolver, "transport": None})()

    assert scheduler.sync_transport_capacity_and_inventory(
        db_session, registry, secret_session_factory=secret_factory
    ) == 1
    assert [descriptor.record_id for descriptor in resolver.descriptors] == [
        record_a.id,
        record_b.id,
    ]
    assert [descriptor.code for descriptor in resolver.descriptors] == ["A", "B"]
    assert provider_a.sync_calls == 1
    assert provider_b.sync_calls == 1
    assert events.index("secret-close") < events.index("sync-A")
    assert events.index("secret-close") < events.index("sync-B")
    refreshed_a = db_session.get(TransportProviderRecord, record_a.id)
    refreshed_b = db_session.get(TransportProviderRecord, record_b.id)
    assert refreshed_a is not None and refreshed_a.status is ProviderStatus.DEGRADED
    assert refreshed_b is not None and refreshed_b.status is ProviderStatus.HEALTHY
    assert db_session.scalars(select(AuditLog)).all()


def test_secret_revision_drift_fails_before_sync_and_isolates_other_record(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record_a = _record(db_session, "DRIFT_A")
    record_b = _record(db_session, "DRIFT_B")
    resolver = SubscriptionTransportResolver(tmp_path)
    revisions = {record_a.secret_ref: 1, record_b.secret_ref: 1}
    sync_calls: list[str] = []

    monkeypatch.setattr(
        scheduler,
        "get_settings",
        lambda: type("Settings", (), {"transport_provider_mode": "subscription"})(),
    )

    def secret_factory() -> SecretSession:
        return SecretSession([])

    def load_secret(_db: object, ref: str, _purpose: str) -> SecretSnapshot:
        return SecretSnapshot("https://provider.invalid/private", revisions[ref])

    def sync_nodes(provider: object) -> None:
        sync_calls.append(provider.provider_code)  # type: ignore[attr-defined]

    monkeypatch.setattr(scheduler, "reveal_secret_snapshot_for_purpose", load_secret)
    monkeypatch.setattr(
        "backend.app.providers.transport.subscription.SubscriptionTransportProvider.sync_nodes",
        sync_nodes,
    )
    def refresh(_db: object, record: TransportProviderRecord, provider: object) -> None:
        sync_nodes(provider)

    monkeypatch.setattr(scheduler, "refresh_provider_inventory", refresh)
    registry = type("Registry", (), {"transport_resolver": resolver, "transport": None})()

    first = scheduler.sync_transport_capacity_and_inventory(
        db_session, registry, secret_session_factory=secret_factory
    )
    assert first == 2
    assert sync_calls == ["DRIFT_A", "DRIFT_B"]

    revisions[record_a.secret_ref] = 2
    second = scheduler.sync_transport_capacity_and_inventory(
        db_session, registry, secret_session_factory=secret_factory
    )
    assert second == 1
    assert sync_calls == ["DRIFT_A", "DRIFT_B", "DRIFT_B"]
    refreshed_a = db_session.get(TransportProviderRecord, record_a.id)
    refreshed_b = db_session.get(TransportProviderRecord, record_b.id)
    assert refreshed_a is not None and refreshed_a.status is ProviderStatus.DEGRADED
    assert refreshed_b is not None and refreshed_b.status is ProviderStatus.HEALTHY
