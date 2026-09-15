from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.sql.schema import Table

import backend.app.infra.xray_reality as reality_bootstrap
import backend.app.models  # noqa: F401
from backend.app.core.database import Base
from backend.app.core.secrets import put_secret
from backend.app.infra.credential_resolver import SqlAlchemyCredentialResolver
from backend.app.infra.xray_reality import (
    REALITY_IDENTITY_PURPOSE,
    REALITY_IDENTITY_SECRET_REF,
    XrayRealityBootstrapError,
    bootstrap_reality_identity,
)
from backend.app.models import Secret
from backend.app.providers.gateway.xray_composition import XrayRealityConfig


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'reality.db'}")
    secret_table = cast(Table, Secret.__table__)
    Base.metadata.create_all(engine, tables=[secret_table])
    yield engine
    Base.metadata.drop_all(engine, tables=[secret_table])
    engine.dispose()


def test_fresh_bootstrap_persists_once_and_resolver_reads_winner(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def generate(_: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({"privateKey": "generated-private-key", "shortIds": ["generated-id"]})

    monkeypatch.setattr(reality_bootstrap, "_new_reality_identity", generate)
    with Session(engine) as db:
        bootstrapped = bootstrap_reality_identity(db, xray_binary="unused-xray")
        resolved = SqlAlchemyCredentialResolver(db).resolve_reality_identity()

    assert calls == 1
    assert bootstrapped == XrayRealityConfig("generated-private-key", ("generated-id",))
    assert resolved == bootstrapped


def test_existing_valid_identity_never_calls_generator(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Session(engine) as db:
        put_secret(
            db,
            REALITY_IDENTITY_SECRET_REF,
            '{"privateKey":"existing-private-key","shortIds":["existing-id"]}',
            REALITY_IDENTITY_PURPOSE,
        )
        db.commit()

    def fail(_: str) -> str:
        raise AssertionError("generator must not run")

    monkeypatch.setattr(reality_bootstrap, "_new_reality_identity", fail)
    with Session(engine) as db:
        identity = bootstrap_reality_identity(db, xray_binary="unused-xray")

    assert identity == XrayRealityConfig("existing-private-key", ("existing-id",))


@pytest.mark.parametrize(
    ("seed", "purpose", "ciphertext"),
    [
        (
            '{"privateKey":"malformed-private-key","shortIds":[]}',
            REALITY_IDENTITY_PURPOSE,
            None,
        ),
        (
            '{"privateKey":"wrong-purpose-key","shortIds":["wrong-purpose-id"]}',
            "OTHER_PURPOSE",
            None,
        ),
        (None, REALITY_IDENTITY_PURPOSE, "ciphertext-sentinel"),
    ],
)
def test_invalid_existing_identity_never_regenerates(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    seed: str | None,
    purpose: str,
    ciphertext: str | None,
) -> None:
    with Session(engine) as db:
        if seed is not None:
            put_secret(db, REALITY_IDENTITY_SECRET_REF, seed, purpose)
            db.commit()
        else:
            db.add(
                Secret(
                    secret_ref=REALITY_IDENTITY_SECRET_REF,
                    ciphertext=ciphertext,
                    purpose=purpose,
                )
            )
            db.commit()

    generator_calls = 0

    def generate(_: str) -> str:
        nonlocal generator_calls
        generator_calls += 1
        return json.dumps({"privateKey": "unexpected-private-key", "shortIds": ["unexpected-id"]})

    monkeypatch.setattr(reality_bootstrap, "_new_reality_identity", generate)
    with Session(engine) as db, pytest.raises(XrayRealityBootstrapError) as raised:
        bootstrap_reality_identity(db, xray_binary="unused-xray")

    assert generator_calls == 0
    assert "privateKey" not in str(raised.value)
    assert "shortIds" not in str(raised.value)
    assert "unexpected-private-key" not in str(raised.value)
    assert "unexpected-id" not in str(raised.value)
    assert "ciphertext-sentinel" not in str(raised.value)
