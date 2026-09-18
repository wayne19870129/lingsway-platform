from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from backend.app.core.secrets import put_secret
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
    return engine


def _write(engine: Engine, ref: str, value: str) -> None:
    with Session(engine) as db:
        put_secret(db, ref, value, "TRANSPORT_SUBSCRIPTION_URL")
        db.commit()


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
