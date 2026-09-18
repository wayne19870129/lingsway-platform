from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from backend.app.core.database import Base
from backend.app.core.secrets import decrypt_secret, encrypt_secret, put_secret
from backend.app.models.ops import Secret


@pytest.fixture(scope="module")
def mysql_engine() -> Engine:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for MySQL secret concurrency tests")
    sync_url = database_url.replace("mysql+asyncmy://", "mysql+pymysql://", 1)
    engine = create_engine(sync_url, pool_pre_ping=True)
    if engine.dialect.name != "mysql":
        pytest.skip("MySQL secret concurrency tests require a MySQL engine")
    Base.metadata.create_all(engine, tables=[Base.metadata.tables["secrets"]])
    return engine


def _write(engine: Engine, ref: str, value: str) -> None:
    with Session(engine) as db:
        put_secret(db, ref, value, "TRANSPORT_SUBSCRIPTION_URL")
        db.commit()


def test_mysql_0023_upgrade_preserves_rows_and_revision_semantics(
    mysql_engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync_url = str(mysql_engine.url)
    ciphertext = encrypt_secret("https://before.invalid")
    with mysql_engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS secrets"))
        connection.execute(
            text(
                "CREATE TABLE secrets ("
                "id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
                "secret_ref VARCHAR(160) NOT NULL UNIQUE, "
                "ciphertext TEXT NOT NULL, purpose VARCHAR(80) NOT NULL, "
                "created_at DATETIME(6) NOT NULL, updated_at DATETIME(6) NOT NULL"
                ") ENGINE=InnoDB"
            )
        )
        connection.execute(
            text(
                "INSERT INTO secrets (secret_ref, ciphertext, purpose, created_at, updated_at) "
                "VALUES (:ref, :ciphertext, :purpose, NOW(6), NOW(6))"
            ),
            {"ref": "migration/old", "ciphertext": ciphertext, "purpose": "TEST"},
        )
    from backend.app.core import config as app_config

    script_location = Path(__file__).parents[3] / "infrastructure" / "alembic"
    settings = app_config.get_settings()
    monkey_settings = type(settings)(
        database_url=sync_url,
        secret_encryption_key=settings.secret_encryption_key,
    )
    monkeypatch.setattr(app_config, "get_settings", lambda: monkey_settings)
    alembic_config = Config()
    alembic_config.set_main_option("script_location", str(script_location))
    alembic_config.set_main_option("sqlalchemy.url", sync_url)
    command.stamp(alembic_config, "0022_egress_purpose")
    command.upgrade(alembic_config, "0023_secret_revision")

    with mysql_engine.connect() as connection:
        columns = {column["name"]: column for column in inspect(connection).get_columns("secrets")}
        assert columns["revision"]["nullable"] is False
        assert columns["revision"]["default"] in ("1", "'1'")
        row = connection.execute(
            text("SELECT ciphertext, purpose, revision FROM secrets WHERE secret_ref = :ref"),
            {"ref": "migration/old"},
        ).one()
        assert row.ciphertext == ciphertext
        assert row.purpose == "TEST"
        assert row.revision == 1
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0023_secret_revision"
        )

    _write(mysql_engine, "migration/new", "https://new.invalid")
    with Session(mysql_engine) as db:
        row = db.scalar(select(Secret).where(Secret.secret_ref == "migration/new"))
        assert row is not None and row.revision == 1
        put_secret(db, "migration/new", "https://newer.invalid", "TRANSPORT_SUBSCRIPTION_URL")
        db.commit()
        row = db.scalar(select(Secret).where(Secret.secret_ref == "migration/new"))
        assert row is not None and row.revision == 2
        assert decrypt_secret(row.ciphertext) == "https://newer.invalid"


def test_mysql_same_missing_secret_ref_keeps_revision_one(mysql_engine: Engine) -> None:
    ref = f"mysql-secret/{uuid4().hex}"
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_write, mysql_engine, ref, "https://same.invalid") for _ in range(2)]
        for future in futures:
            future.result()
    with Session(mysql_engine) as db:
        row = db.scalar(select(Secret).where(Secret.secret_ref == ref))
        assert row is not None
        assert row.revision == 1


def test_mysql_semantic_secret_updates_serialize_revision(mysql_engine: Engine) -> None:
    ref = f"mysql-secret/{uuid4().hex}"
    _write(mysql_engine, ref, "https://initial.invalid")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_write, mysql_engine, ref, "https://one.invalid"),
            pool.submit(_write, mysql_engine, ref, "https://two.invalid"),
        ]
        for future in futures:
            future.result()
    with Session(mysql_engine) as db:
        row = db.scalar(select(Secret).where(Secret.secret_ref == ref))
        assert row is not None
        assert row.revision == 3
        assert row.ciphertext
        assert decrypt_secret(row.ciphertext) in {"https://one.invalid", "https://two.invalid"}


def test_mysql_different_missing_secret_refs_do_not_share_gap_lock(
    mysql_engine: Engine,
) -> None:
    refs = [f"mysql-secret/{uuid4().hex}", f"mysql-secret/{uuid4().hex}"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_write, mysql_engine, refs[0], "https://one.invalid"),
            pool.submit(_write, mysql_engine, refs[1], "https://two.invalid"),
        ]
        for future in futures:
            future.result()
