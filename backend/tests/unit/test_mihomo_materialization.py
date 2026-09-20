from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.app.core.database import Base
from backend.app.infra.mihomo_materialization import (
    MaterializationVerificationError,
    VerifiedMaterialization,
    verify_materialization,
)
from backend.app.models import MihomoTransportMaterialization


@pytest.fixture
def materialization_db() -> Generator[Session, None, None]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.tables["mihomo_transport_materializations"].create(engine)
    with Session(engine) as db:
        yield db


def _receipt(
    db: Session,
    path: Path,
    *,
    source_revision: int = 3,
    freshness_deadline: datetime | None = None,
) -> None:
    content = path.read_bytes()
    db.add(
        MihomoTransportMaterialization(
            owner_record_id=1,
            provider_code="provider-a",
            source_revision=source_revision,
            cache_identity=str(path),
            content_hash=sha256(content).hexdigest(),
            freshness_deadline=freshness_deadline or datetime.now(UTC) + timedelta(hours=1),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    db.commit()


def _receipt_state(db: Session) -> tuple[int, tuple[object, ...] | None]:
    db.expire_all()
    rows = db.scalars(select(MihomoTransportMaterialization)).all()
    if not rows:
        return 0, None
    receipt = rows[0]
    return 1, (
        receipt.owner_record_id,
        receipt.provider_code,
        receipt.source_revision,
        receipt.cache_identity,
        receipt.content_hash,
        receipt.freshness_deadline,
    )


def test_receipt_hash_mismatch_fails_closed_without_remint(
    tmp_path: Path, materialization_db: Session
) -> None:
    path = tmp_path / "provider.yaml"
    path.write_bytes(b"proxies: []\n")
    _receipt(materialization_db, path)
    path.write_bytes(b"proxies: [tampered]\n")
    before = _receipt_state(materialization_db)
    with pytest.raises(MaterializationVerificationError, match="HASH_MISMATCH"):
        verify_materialization(materialization_db, 1, path, "provider-a", source_revision=3)
    assert _receipt_state(materialization_db) == before


def test_receipt_cache_identity_mismatch_fails_closed(
    materialization_db: Session, tmp_path: Path
) -> None:
    first, second = tmp_path / "one.yaml", tmp_path / "two.yaml"
    first.write_bytes(b"proxies: []\n")
    second.write_bytes(first.read_bytes())
    _receipt(materialization_db, first)
    before = _receipt_state(materialization_db)
    with pytest.raises(MaterializationVerificationError, match="IDENTITY_MISMATCH"):
        verify_materialization(materialization_db, 1, second, "provider-a", source_revision=3)
    assert _receipt_state(materialization_db) == before


def test_missing_or_expired_receipt_fails_closed(
    materialization_db: Session, tmp_path: Path
) -> None:
    path = tmp_path / "provider.yaml"
    path.write_bytes(b"proxies: []\n")
    with pytest.raises(MaterializationVerificationError, match="RECEIPT_MISSING"):
        verify_materialization(materialization_db, 1, path, "provider-a", source_revision=3)
    assert _receipt_state(materialization_db) == (0, None)
    _receipt(materialization_db, path, freshness_deadline=datetime.now(UTC) - timedelta(seconds=1))
    before = _receipt_state(materialization_db)
    with pytest.raises(MaterializationVerificationError, match="EXPIRED"):
        verify_materialization(materialization_db, 1, path, "provider-a", source_revision=3)
    assert _receipt_state(materialization_db) == before


def test_source_revision_mismatch_fails_closed(materialization_db: Session, tmp_path: Path) -> None:
    path = tmp_path / "provider.yaml"
    path.write_bytes(b"proxies: []\n")
    _receipt(materialization_db, path)
    before = _receipt_state(materialization_db)
    with pytest.raises(MaterializationVerificationError, match="IDENTITY_MISMATCH"):
        verify_materialization(materialization_db, 1, path, "provider-a", source_revision=4)
    assert _receipt_state(materialization_db) == before


def test_verified_materialization_repr_redacts_raw_content() -> None:
    value = VerifiedMaterialization(
        1, "provider-a", 3, "secret-token-path", "a" * 64, datetime.now(UTC), b"SECRET_YAML"
    )
    rendered = repr(value)
    assert "SECRET_YAML" not in rendered
    assert "secret-token-path" not in rendered
    assert "<redacted>" in rendered


def test_receipt_verification_refreshes_identity_mapped_current_row(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'receipts.db'}")
    Base.metadata.tables["mihomo_transport_materializations"].create(engine)
    path = tmp_path / "provider.yaml"
    path.write_bytes(b"proxies: []\n")
    with Session(engine) as db:
        _receipt(db, path, source_revision=3)
    with Session(engine) as session_a, Session(engine) as session_b:
        old = session_a.scalar(select(MihomoTransportMaterialization))
        assert old is not None
        current = session_b.scalar(select(MihomoTransportMaterialization))
        assert current is not None
        current.source_revision = 4
        session_b.commit()
        old_content = old.content_hash
        with pytest.raises(MaterializationVerificationError, match="IDENTITY_MISMATCH"):
            verify_materialization(session_a, 1, path, "provider-a", source_revision=3)
        refreshed = session_a.scalar(select(MihomoTransportMaterialization))
        assert refreshed is not None
        assert refreshed.source_revision == 4
        assert refreshed.content_hash == old_content
