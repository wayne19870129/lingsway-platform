from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import Table, create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

import backend.app.models  # noqa: F401
from backend.app.core.config import Settings
from backend.app.core.database import Base
from backend.app.core.secrets import put_secret
from backend.app.infra.credential_resolver import SqlAlchemyCredentialResolver
from backend.app.models import Secret
from backend.app.providers.base import CredentialResolutionError
from backend.app.providers.gateway.xray_composition import XrayRealityConfig

REALITY_REF = "gateway/xray/reality-identity"
REALITY_PURPOSE = "XRAY_REALITY_IDENTITY"


@pytest.fixture
def secret_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'reality.db'}")
    secret_table = cast(Table, Secret.__table__)
    Base.metadata.create_all(engine, tables=[secret_table])
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine, tables=[secret_table])
        engine.dispose()


def test_settings_own_reality_deploy_parameters_and_xray_log_level() -> None:
    settings = Settings.from_env(
        {
            "XRAY_LOG_LEVEL": "  DeBuG ",
            "XRAY_REALITY_DEST": "env-dest.example:443",
            "XRAY_REALITY_SERVER_NAME": "env-server.example",
        }
    )

    assert settings.xray_log_level == "debug"
    assert settings.xray_reality_dest == "env-dest.example:443"
    assert settings.xray_reality_server_name == "env-server.example"


@pytest.mark.parametrize("value", ["", " ", "trace", "INFO\nextra"])
def test_invalid_xray_log_level_fails_closed(value: str) -> None:
    with pytest.raises(ValueError, match="XRAY_LOG_LEVEL"):
        Settings.from_env({"XRAY_LOG_LEVEL": value})


def test_reality_resolver_reads_persisted_identity_without_regeneration(
    secret_engine: Engine,
) -> None:
    with Session(secret_engine) as db:
        put_secret(
            db,
            REALITY_REF,
            json.dumps({"privateKey": "persisted-private-key", "shortIds": ["persisted-id"]}),
            REALITY_PURPOSE,
        )
        identity = SqlAlchemyCredentialResolver(db).resolve_reality_identity()

    assert identity == XrayRealityConfig("persisted-private-key", ["persisted-id"])
    assert repr(identity) == "XrayRealityConfig(private_key=<redacted>, short_ids=<redacted>)"


@pytest.mark.parametrize(
    "stored_value",
    [
        "not-json",
        json.dumps({"privateKey": "sentinel-private-key", "shortIds": []}),
        json.dumps(
            {
                "privateKey": "sentinel-private-key",
                "shortIds": ["sentinel-short-id"],
                "unexpected": True,
            }
        ),
        json.dumps({"privateKey": "sentinel-private-key", "shortIds": "not-a-list"}),
    ],
)
def test_malformed_reality_identity_fails_closed(
    secret_engine: Engine, stored_value: str
) -> None:
    with Session(secret_engine) as db:
        put_secret(db, REALITY_REF, stored_value, REALITY_PURPOSE)
        with pytest.raises(CredentialResolutionError) as raised:
            SqlAlchemyCredentialResolver(db).resolve_reality_identity()

    assert raised.value.code == "CREDENTIAL_MALFORMED"
    assert "sentinel-private-key" not in str(raised.value)
    assert "sentinel-short-id" not in str(raised.value)


def test_wrong_purpose_and_decrypt_failures_are_not_fallbacks(secret_engine: Engine) -> None:
    with Session(secret_engine) as db:
        put_secret(
            db,
            REALITY_REF,
            json.dumps({"privateKey": "key", "shortIds": ["id"]}),
            "WRONG_PURPOSE",
        )
        with pytest.raises(CredentialResolutionError) as wrong_purpose:
            SqlAlchemyCredentialResolver(db).resolve_reality_identity()
        assert wrong_purpose.value.code == "CREDENTIAL_NOT_FOUND"

        existing = db.scalar(select(Secret).where(Secret.secret_ref == REALITY_REF))
        assert existing is not None
        db.delete(existing)
        db.flush()
        db.add(
            Secret(
                secret_ref=REALITY_REF,
                ciphertext="ciphertext-sentinel",
                purpose=REALITY_PURPOSE,
            )
        )
        db.flush()
        with pytest.raises(CredentialResolutionError) as decrypt_failed:
            SqlAlchemyCredentialResolver(db).resolve_reality_identity()

    assert decrypt_failed.value.code == "CREDENTIAL_DECRYPT_FAILED"
    assert "ciphertext-sentinel" not in str(decrypt_failed.value)

