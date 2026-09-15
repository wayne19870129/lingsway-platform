from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import Table, create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

import backend.app.models  # noqa: F401
import ops.gateway.render_xray_routes as renderer
from backend.app.core.config import Settings
from backend.app.core.database import Base
from backend.app.core.secrets import put_secret
from backend.app.models import Secret


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


def _base_config() -> dict[str, Any]:
    return {
        "inbounds": [
            {
                "protocol": "vless",
                "listen": "0.0.0.0",
                "port": 8443,
                "streamSettings": {
                    "security": "reality",
                    "realitySettings": {
                        "show": False,
                        "xver": 0,
                        "dest": "template-bogus.example:443",
                        "serverNames": ["template-bogus.example"],
                        "privateKey": "template-bogus-private-key",
                        "shortIds": ["template-bogus-short-id"],
                    },
                },
            }
        ]
    }


def _settings() -> Settings:
    return Settings(
        xray_reality_dest="settings-dest.example:443",
        xray_reality_server_name="settings-server.example",
    )


def test_settings_own_reality_deploy_parameters() -> None:
    settings = Settings.from_env(
        {
            "XRAY_REALITY_DEST": "env-dest.example:443",
            "XRAY_REALITY_SERVER_NAME": "env-server.example",
        }
    )

    assert settings.xray_reality_dest == "env-dest.example:443"
    assert settings.xray_reality_server_name == "env-server.example"


def test_template_reality_values_are_ignored_and_identity_is_stable(
    secret_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    generated: list[str] = []

    def generate(binary: str) -> str:
        generated.append(binary)
        return json.dumps({"privateKey": "persisted-private-key", "shortIds": ["persisted-id"]})

    monkeypatch.setattr(renderer, "_new_reality_identity", generate)
    settings = _settings()

    with Session(secret_engine) as db:
        first = renderer._reality_settings(_base_config(), db, settings)

    with Session(secret_engine) as db:
        second = renderer._reality_settings(_base_config(), db, settings)
        stored = db.scalar(
            select(Secret).where(Secret.secret_ref == renderer.REALITY_IDENTITY_SECRET_REF)
        )

    assert first == second
    assert first["dest"] == "settings-dest.example:443"
    assert first["serverNames"] == ["settings-server.example"]
    assert first["privateKey"] == "persisted-private-key"
    assert first["shortIds"] == ["persisted-id"]
    assert generated == ["/usr/local/bin/xray"]
    assert stored is not None
    assert stored.purpose == renderer.REALITY_IDENTITY_PURPOSE


def test_blank_reality_settings_fail_closed_before_bootstrap(
    secret_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    generated = False

    def generate(_: str) -> str:
        nonlocal generated
        generated = True
        return "{}"

    monkeypatch.setattr(renderer, "_new_reality_identity", generate)

    with Session(secret_engine) as db:
        with pytest.raises(renderer.XrayRenderError, match="XRAY_REALITY_DEST"):
            renderer._reality_settings(
                _base_config(),
                db,
                Settings(xray_reality_dest=" ", xray_reality_server_name=""),
            )
        assert db.scalar(select(Secret)) is None

    assert generated is False


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
    ],
)
def test_malformed_reality_identity_fails_closed_without_regeneration(
    secret_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    stored_value: str,
) -> None:
    generated = False

    def generate(_: str) -> str:
        nonlocal generated
        generated = True
        return json.dumps({"privateKey": "replacement", "shortIds": ["replacement"]})

    monkeypatch.setattr(renderer, "_new_reality_identity", generate)
    with Session(secret_engine) as db:
        put_secret(
            db,
            renderer.REALITY_IDENTITY_SECRET_REF,
            stored_value,
            renderer.REALITY_IDENTITY_PURPOSE,
        )
        db.commit()

        with pytest.raises(
            renderer.XrayRenderError, match="XRAY_REALITY_IDENTITY_MALFORMED"
        ) as raised:
            renderer._reality_settings(_base_config(), db, _settings())

    assert generated is False
    assert "sentinel-private-key" not in str(raised.value)
    assert "sentinel-short-id" not in str(raised.value)
    assert stored_value not in str(raised.value)


def test_reality_identity_purpose_and_decrypt_failures_do_not_regenerate(
    secret_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    generated = False

    def generate(_: str) -> str:
        nonlocal generated
        generated = True
        return json.dumps({"privateKey": "replacement", "shortIds": ["replacement"]})

    monkeypatch.setattr(renderer, "_new_reality_identity", generate)
    with Session(secret_engine) as db:
        put_secret(
            db,
            renderer.REALITY_IDENTITY_SECRET_REF,
            json.dumps({"privateKey": "key", "shortIds": ["id"]}),
            "WRONG_PURPOSE",
        )
        db.add(
            Secret(
                secret_ref="gateway/xray/bad-reality-identity",
                ciphertext="ciphertext-sentinel",
                purpose=renderer.REALITY_IDENTITY_PURPOSE,
            )
        )
        db.commit()

        with pytest.raises(renderer.XrayRenderError, match="UNREADABLE"):
            renderer._reality_settings(_base_config(), db, _settings())

        wrong = db.scalar(
            select(Secret).where(Secret.secret_ref == renderer.REALITY_IDENTITY_SECRET_REF)
        )
        assert wrong is not None
        db.delete(wrong)
        db.flush()
        db.add(
            Secret(
                secret_ref=renderer.REALITY_IDENTITY_SECRET_REF,
                ciphertext="ciphertext-sentinel",
                purpose=renderer.REALITY_IDENTITY_PURPOSE,
            )
        )
        db.flush()
        with pytest.raises(renderer.XrayRenderError, match="UNREADABLE"):
            renderer._reality_settings(_base_config(), db, _settings())

    assert generated is False
