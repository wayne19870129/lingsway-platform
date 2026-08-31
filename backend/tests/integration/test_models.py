import os

import pytest
from sqlalchemy import create_engine, text

import backend.app.models  # noqa: F401
from backend.app.core.database import Base


def test_split_models_register_the_complete_legacy_metadata() -> None:
    assert len(Base.metadata.tables) == 38
    assert {
        "customers",
        "orders",
        "subscriptions",
        "egress_endpoints",
        "gateway_route_bindings",
        "audit_logs",
    }.issubset(Base.metadata.tables)
    assert "purpose" in Base.metadata.tables["egress_endpoints"].columns


def test_mysql_84_database_is_reachable() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.fail("TEST_DATABASE_URL is required; integration tests are never skipped")
    sync_database_url = database_url.replace("mysql+asyncmy://", "mysql+pymysql://", 1)
    engine = create_engine(sync_database_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT 1")) == 1
            version = str(connection.scalar(text("SELECT VERSION()")))
            assert version.startswith("8.4.")
    finally:
        engine.dispose()
