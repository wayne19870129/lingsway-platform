from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Table, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.database import Base
from backend.app.models import AuditLog, EgressEndpoint, EgressGroup
from backend.app.providers.base import EgressEndpointDTO
from backend.app.providers.egress.mock import MockEgressProvider
from backend.app.workers.drift_check import check_egress_drift


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    table_names = {"audit_logs", "egress_endpoints", "egress_groups"}
    tables: list[Table] = [Base.metadata.tables[name] for name in table_names]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session
    Base.metadata.drop_all(engine, tables=tables)
    engine.dispose()


_next_listen_port = iter(range(21080, 21200))


def add_endpoint(
    db: Session, code: str, host: str, port: int, *, status: str = "AVAILABLE"
) -> EgressEndpoint:
    group = EgressGroup(code=f"GROUP_{code}", region="US")
    db.add(group)
    db.flush()
    endpoint = EgressEndpoint(
        group_id=group.id,
        code=code,
        provider_name="webshare",
        host=host,
        port=port,
        protocol="socks5",
        credential_secret_ref=f"egress/{code}/base",
        status=status,
        capacity=1,
        current_count=0,
        mihomo_listen_port=next(_next_listen_port),
    )
    db.add(endpoint)
    db.flush()
    return endpoint


def test_no_active_endpoints_is_a_no_op(db_session: Session) -> None:
    result = check_egress_drift(db_session, MockEgressProvider(endpoints=[]))
    assert result.checked == 0
    assert result.findings == ()
    assert result.provider_returned_no_data is False
    assert db_session.scalar(select(AuditLog)) is None


def test_matching_host_and_port_is_not_flagged(db_session: Session) -> None:
    add_endpoint(db_session, "EGRESS_01", "203.0.113.10", 1080)
    provider = MockEgressProvider(
        endpoints=[EgressEndpointDTO("EGRESS_01", "203.0.113.10", 1080)]
    )

    result = check_egress_drift(db_session, provider)

    assert result.checked == 1
    assert result.findings == ()
    assert db_session.scalar(select(AuditLog)) is None


def test_host_or_port_mismatch_is_flagged_and_audited(db_session: Session) -> None:
    add_endpoint(db_session, "EGRESS_02", "203.0.113.20", 1080)
    provider = MockEgressProvider(
        # Provider silently reports a different IP for the same endpoint.
        endpoints=[EgressEndpointDTO("EGRESS_02", "203.0.113.99", 1080)]
    )

    result = check_egress_drift(db_session, provider)

    assert result.checked == 1
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.endpoint_code == "EGRESS_02"
    assert finding.db_host == "203.0.113.20"
    assert finding.provider_host == "203.0.113.99"

    audit = db_session.scalar(select(AuditLog))
    assert audit is not None
    assert audit.action == "EGRESS_DRIFT_DETECTED"
    assert audit.entity_id == "EGRESS_02"

    # Read-only: no mutation of the endpoint row itself.
    stored = db_session.scalar(select(EgressEndpoint).where(EgressEndpoint.code == "EGRESS_02"))
    assert stored is not None
    assert stored.host == "203.0.113.20"


def test_provider_returning_no_data_records_one_finding_not_one_per_endpoint(
    db_session: Session,
) -> None:
    add_endpoint(db_session, "EGRESS_03", "203.0.113.30", 1080)
    add_endpoint(db_session, "EGRESS_04", "203.0.113.40", 1080)
    # Simulates providers/egress/webshare.py's current list_endpoints() stub,
    # which always returns [] pending full response-mapping work.
    provider = MockEgressProvider(endpoints=[])

    result = check_egress_drift(db_session, provider)

    assert result.checked == 2
    assert result.findings == ()
    assert result.provider_returned_no_data is True

    audits = db_session.scalars(select(AuditLog)).all()
    assert len(audits) == 1
    assert audits[0].action == "EGRESS_DRIFT_CHECK_NO_DATA"


def test_endpoint_not_reported_by_provider_is_not_flagged_as_drift(db_session: Session) -> None:
    add_endpoint(db_session, "EGRESS_05", "203.0.113.50", 1080)
    # Provider reports a different, unrelated endpoint but not this one.
    provider = MockEgressProvider(
        endpoints=[EgressEndpointDTO("SOME_OTHER_ENDPOINT", "198.51.100.1", 1080)]
    )

    result = check_egress_drift(db_session, provider)

    assert result.checked == 1
    assert result.findings == ()
    assert result.provider_returned_no_data is False
    assert db_session.scalar(select(AuditLog)) is None
