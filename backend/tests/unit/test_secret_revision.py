from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.core.database import Base
from backend.app.core.secrets import (
    put_secret,
    reveal_secret_for_purpose,
    reveal_secret_snapshot_for_purpose,
)


def test_secret_revision_changes_only_when_value_or_purpose_changes(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setenv(
        "SECRET_ENCRYPTION_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
    )
    with Session(engine) as db:
        first = put_secret(db, "transport/a/url", "https://one.invalid", "PURPOSE")
        db.commit()
        assert first.revision == 1
        same = put_secret(db, "transport/a/url", "https://one.invalid", "PURPOSE")
        db.commit()
        assert same.revision == 1
        changed = put_secret(db, "transport/a/url", "https://two.invalid", "PURPOSE")
        db.commit()
        assert changed.revision == 2
        snapshot = reveal_secret_snapshot_for_purpose(db, "transport/a/url", "PURPOSE")
        assert snapshot.value == "https://two.invalid"
        assert snapshot.revision == 2
        assert "https://two.invalid" not in repr(snapshot)
        assert reveal_secret_for_purpose(db, "transport/a/url", "PURPOSE") == snapshot.value
