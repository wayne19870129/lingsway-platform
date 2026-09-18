from __future__ import annotations

import json
import os
from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

import backend.app.models  # noqa: F401
from backend.app.core.database import Base, build_engine
from backend.app.infra.mihomo_reconciliation import (
    CommitOutcome,
    classify_finalization_outcome,
    enqueue_mihomo_reconciliation,
)
from backend.app.models import JobStatus


@pytest.fixture
def mysql_engine() -> Iterator[Engine]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for Mihomo reconciliation tests")
    engine = build_engine(database_url.replace("mysql+asyncmy://", "mysql+pymysql://", 1))
    if engine.dialect.name != "mysql":
        engine.dispose()
        pytest.skip("SKIP LOCKED/current-read coverage is MySQL-only")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_mysql_enqueue_commit_and_classifier(mysql_engine: Engine) -> None:
    with Session(mysql_engine, expire_on_commit=False) as db:
        job = enqueue_mihomo_reconciliation(
            db, operation_id="mysql-r1", operation_kind="RECONCILE", snapshot_revision=1
        )
        db.commit()
        assert job.status is JobStatus.PENDING
        payload = json.loads(job.payload_json)
        payload["finalization_evidence"] = {
            "operation_id": "mysql-r1",
            "operation_kind": "RECONCILE",
            "snapshot_revision": 1,
            "candidate_fingerprint": "projection-v1",
            "verification": "readback-v1",
        }
        job.payload_json = json.dumps(payload)
        job.status = JobStatus.SUCCEEDED
        db.commit()
        assert classify_finalization_outcome(
            db,
            job_id=job.id,
            operation_id="mysql-r1",
            operation_kind="RECONCILE",
            snapshot_revision=1,
            candidate_fingerprint="projection-v1",
        ) is CommitOutcome.LANDED
