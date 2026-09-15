from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import Table, create_engine, select
from sqlalchemy.dialects import mysql
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

import backend.app.models  # noqa: F401
from backend.app.core.database import Base
from backend.app.core.secrets import put_secret
from backend.app.infra.credential_resolver import (
    SqlAlchemyCredentialResolver,
    _current_secret_statement,
)
from backend.app.models import Secret
from backend.app.providers.base import (
    CandidateConfig,
    CredentialDTO,
    CredentialResolutionError,
    DesiredRoutingState,
    XrayOutboundDTO,
)
from backend.app.providers.gateway.mock import MockGatewayProvider
from backend.app.providers.gateway.xray_composition import (
    XrayDeploymentConfig,
    XrayRealityConfig,
    XrayStaticSkeleton,
)
from backend.app.providers.gateway.xray_file import (
    XrayFileProvider,
    XrayRuntime,
    XrayValidationError,
)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'secrets.db'}")
    secret_table = cast(Table, Secret.__table__)
    Base.metadata.create_all(engine, tables=[secret_table])
    yield engine
    Base.metadata.drop_all(engine, tables=[secret_table])
    engine.dispose()


def test_secret_bearing_values_have_safe_repr_and_keep_value_access() -> None:
    username = "sentinel-username"
    password = "sentinel-password"
    ref = "sentinel-secret-ref"
    content = {"password": password, "credential_secret_ref": ref}

    credential = CredentialDTO(username, password)
    outbound = XrayOutboundDTO("egress", "proxy.example.invalid", 1080, "socks5", ref)
    candidate = CandidateConfig(content, "v1")
    values = (repr(credential), repr(outbound), repr(candidate))

    assert all(username not in value for value in values)
    assert all(password not in value for value in values)
    assert all(ref not in value for value in values)
    assert repr(credential) == "CredentialDTO(username=<redacted>, password=<redacted>)"
    assert "version='v1'" in repr(candidate)
    assert "content=<redacted>" in repr(candidate)
    assert credential.username == username
    assert credential.password == password
    assert outbound.credential_secret_ref == ref
    assert candidate.content == content
    assert candidate.version == "v1"
    assert candidate == CandidateConfig(content, "v1")


def test_sensitive_values_reject_generic_dataclass_serialization() -> None:
    username = "sentinel_username"
    password = "sentinel_password"
    ref = "sentinel_secret_ref"
    candidate = CandidateConfig(
        {
            "username": username,
            "password": password,
            "credential_secret_ref": ref,
        },
        "v1",
    )
    values = (
        CredentialDTO(username, password),
        candidate,
        XrayOutboundDTO("egress", "proxy.example.invalid", 1080, "socks5", ref),
    )

    for value in values:
        with pytest.raises(TypeError) as raised:
            asdict(cast(Any, value))
        assert "dataclass" in str(raised.value)
        assert username not in str(raised.value)
        assert password not in str(raised.value)
        assert ref not in str(raised.value)


def test_current_read_statement_refreshes_identity_map_and_locks_on_mysql() -> None:
    statement = _current_secret_statement("credential/current")

    assert statement.get_execution_options()["populate_existing"] is True
    assert "FOR UPDATE" in str(statement.compile(dialect=mysql.dialect()))


def test_resolver_reads_own_uncommitted_put(engine: Engine) -> None:
    with Session(engine) as db:
        put_secret(
            db,
            "credential/own-write",
            json.dumps({"username": "alice", "password": "pw"}),
            "EGRESS_CREDENTIAL",
        )

        resolved = SqlAlchemyCredentialResolver(db).resolve("credential/own-write")

    assert resolved == CredentialDTO("alice", "pw")


def test_resolver_refreshes_stale_identity_map_from_latest_commit(engine: Engine) -> None:
    with Session(engine) as writer:
        put_secret(
            writer,
            "credential/stale",
            json.dumps({"username": "old-user", "password": "old-pw"}),
            "EGRESS_CREDENTIAL",
        )
        writer.commit()

    with Session(engine) as reader:
        stale = reader.scalar(select(Secret).where(Secret.secret_ref == "credential/stale"))
        assert stale is not None
        with Session(engine) as writer:
            put_secret(
                writer,
                "credential/stale",
                json.dumps({"username": "new-user", "password": "new-pw"}),
                "EGRESS_CREDENTIAL",
            )
            writer.commit()

        resolved = SqlAlchemyCredentialResolver(reader).resolve("credential/stale")

    assert resolved == CredentialDTO("new-user", "new-pw")


@pytest.mark.parametrize(
    ("secret_ref", "plaintext", "expected_code"),
    [
        ("credential/json", "not-json", "CREDENTIAL_MALFORMED"),
        ("credential/list", json.dumps(["alice", "pw"]), "CREDENTIAL_MALFORMED"),
        ("credential/missing", json.dumps({"username": "alice"}), "CREDENTIAL_MALFORMED"),
        (
            "credential/unknown",
            json.dumps({"username": "alice", "password": "pw", "extra": "x"}),
            "CREDENTIAL_MALFORMED",
        ),
        (
            "credential/blank",
            json.dumps({"username": " ", "password": "pw"}),
            "CREDENTIAL_MALFORMED",
        ),
        (
            "credential/non-string-username",
            json.dumps({"username": 123, "password": "pw"}),
            "CREDENTIAL_MALFORMED",
        ),
        (
            "credential/non-string",
            json.dumps({"username": "alice", "password": 123}),
            "CREDENTIAL_MALFORMED",
        ),
        (
            "credential/blank-password",
            json.dumps({"username": "alice", "password": " "}),
            "CREDENTIAL_MALFORMED",
        ),
    ],
)
def test_resolver_rejects_malformed_payloads(
    engine: Engine, secret_ref: str, plaintext: str, expected_code: str
) -> None:
    with Session(engine) as db:
        put_secret(db, secret_ref, plaintext, "EGRESS_CREDENTIAL")
        with pytest.raises(CredentialResolutionError) as raised:
            SqlAlchemyCredentialResolver(db).resolve(secret_ref)

    assert raised.value.code == expected_code
    assert str(raised.value) == expected_code
    assert secret_ref not in str(raised.value)
    assert plaintext not in str(raised.value)


def test_resolver_rejects_blank_missing_and_decrypt_failures(engine: Engine) -> None:
    with Session(engine) as db:
        with pytest.raises(CredentialResolutionError) as blank:
            SqlAlchemyCredentialResolver(db).resolve(" ")
        with pytest.raises(CredentialResolutionError) as missing:
            SqlAlchemyCredentialResolver(db).resolve("credential/missing")
        db.add(
            Secret(
                secret_ref="credential/bad-ciphertext",
                ciphertext="ciphertext-sentinel",
                purpose="EGRESS_CREDENTIAL",
            )
        )
        db.flush()
        with pytest.raises(CredentialResolutionError) as decrypt_failed:
            SqlAlchemyCredentialResolver(db).resolve("credential/bad-ciphertext")

    assert blank.value.code == "CREDENTIAL_REF_INVALID"
    assert missing.value.code == "CREDENTIAL_NOT_FOUND"
    assert decrypt_failed.value.code == "CREDENTIAL_DECRYPT_FAILED"
    assert "ciphertext-sentinel" not in str(decrypt_failed.value)


def test_gateway_providers_accept_operation_scoped_resolver() -> None:
    class Resolver:
        def resolve(self, secret_ref: str) -> CredentialDTO:
            return CredentialDTO("resolver-user", "resolver-password")

        def resolve_reality_identity(self) -> XrayRealityConfig:
            return XrayRealityConfig("reality-private-key", ["reality-short-id"])

    resolver = Resolver()
    desired = DesiredRoutingState(
        user_routes={"user": "egress"},
        outbounds=(
            XrayOutboundDTO(
                "egress", "proxy.example.invalid", 1080, "socks5", "secret/ref"
            ),
        ),
    )

    mock_candidate = MockGatewayProvider().render(desired, resolver)
    xray_candidate = XrayFileProvider(
        runtime=cast(XrayRuntime, None),
        static_skeleton=XrayStaticSkeleton.canonical(),
        deployment_config=XrayDeploymentConfig(
            xray_log_level="warning",
            reality_dest="dest.example:443",
            reality_server_names=("dest.example",),
        ),
    ).render(desired, resolver)

    assert mock_candidate.version == "gateway-mock-v1"
    assert xray_candidate.version
    assert xray_candidate.content["outbounds"] == [
        {
            "tag": "BLOCK",
            "protocol": "blackhole",
        },
        {
            "tag": "egress",
            "protocol": "socks",
            "settings": {
                "servers": [
                    {
                        "address": "proxy.example.invalid",
                        "port": 1080,
                        "users": [{"username": "resolver-user", "password": "resolver-password"}],
                    }
                ]
            },
        },
    ]


def test_xray_renderer_resolves_each_full_outbound_and_keeps_block_fixed() -> None:
    class RecordingResolver:
        def __init__(self) -> None:
            self.refs: list[str] = []

        def resolve(self, secret_ref: str) -> CredentialDTO:
            self.refs.append(secret_ref)
            return CredentialDTO(f"user-{secret_ref}", f"password-{secret_ref}")

        def resolve_reality_identity(self) -> XrayRealityConfig:
            return XrayRealityConfig("reality-private-key", ["reality-short-id"])

    resolver = RecordingResolver()
    desired = DesiredRoutingState(
        user_routes={"principal-b": "egress-b", "principal-a": "egress-a"},
        outbounds=(
            XrayOutboundDTO("egress-a", "a.example.invalid", 1001, "socks", "ref-a"),
            XrayOutboundDTO("egress-b", "b.example.invalid", 1002, "socks5", "ref-b"),
        ),
    )

    candidate = XrayFileProvider(
        runtime=cast(XrayRuntime, None),
        static_skeleton=XrayStaticSkeleton.canonical(),
        deployment_config=XrayDeploymentConfig(
            xray_log_level="warning",
            reality_dest="dest.example:443",
            reality_server_names=("dest.example",),
        ),
    ).render(desired, resolver)

    assert resolver.refs == ["ref-a", "ref-b"]
    assert candidate.content["outbounds"] == [
        {"tag": "BLOCK", "protocol": "blackhole"},
        {
            "tag": "egress-a",
            "protocol": "socks",
            "settings": {
                "servers": [
                    {
                        "address": "a.example.invalid",
                        "port": 1001,
                        "users": [{"username": "user-ref-a", "password": "password-ref-a"}],
                    }
                ]
            },
        },
        {
            "tag": "egress-b",
            "protocol": "socks",
            "settings": {
                "servers": [
                    {
                        "address": "b.example.invalid",
                        "port": 1002,
                        "users": [{"username": "user-ref-b", "password": "password-ref-b"}],
                    }
                ]
            },
        },
    ]
    assert candidate.content["routing"]["rules"] == [  # type: ignore[index]
        {"type": "field", "ip": ["geoip:private"], "outboundTag": "BLOCK"},
        {"type": "field", "user": ["principal-a"], "outboundTag": "egress-a"},
        {"type": "field", "user": ["principal-b"], "outboundTag": "egress-b"},
        {"type": "field", "network": "tcp,udp", "outboundTag": "BLOCK"},
    ]


def test_desired_routing_state_has_no_legacy_outbound_tags_field() -> None:
    desired = DesiredRoutingState()

    assert not hasattr(desired, "outbound_tags")


@pytest.mark.parametrize(
    ("outbound", "error"),
    [
        (
            XrayOutboundDTO("egress", "proxy.example.invalid", 1080, "http", "secret/ref"),
            "unsupported desired outbound protocol",
        ),
        (
            XrayOutboundDTO("egress", "proxy.example.invalid", 0, "socks5", "secret/ref"),
            "invalid desired outbound port",
        ),
        (
            XrayOutboundDTO("egress", "proxy.example.invalid", 1080, "socks5", " "),
            "CREDENTIAL_REF_INVALID",
        ),
    ],
)
def test_xray_renderer_fails_closed_for_invalid_outbound_inputs(
    outbound: XrayOutboundDTO, error: str
) -> None:
    class Resolver:
        def resolve(self, secret_ref: str) -> CredentialDTO:
            if secret_ref == "secret/ref":
                return CredentialDTO("resolver-user", "resolver-password")
            raise CredentialResolutionError("CREDENTIAL_REF_INVALID")

        def resolve_reality_identity(self) -> XrayRealityConfig:
            return XrayRealityConfig("reality-private-key", ["reality-short-id"])

    desired = DesiredRoutingState(user_routes={"principal": outbound.tag}, outbounds=(outbound,))
    provider = XrayFileProvider(
        runtime=None,  # type: ignore[arg-type]
        static_skeleton=XrayStaticSkeleton.canonical(),
        deployment_config=XrayDeploymentConfig(
            xray_log_level="warning",
            reality_dest="dest.example:443",
            reality_server_names=("dest.example",),
        ),
    )
    with pytest.raises((XrayValidationError, CredentialResolutionError), match=error) as raised:
        provider.render(desired, Resolver())

    assert "secret/ref" not in str(raised.value)


def test_xray_renderer_rejects_missing_static_skeleton_before_composition() -> None:
    class Resolver:
        def resolve(self, secret_ref: str) -> CredentialDTO:
            return CredentialDTO("resolver-user", "resolver-password")

        def resolve_reality_identity(self) -> XrayRealityConfig:
            return XrayRealityConfig("reality-private-key", ["reality-short-id"])

    provider = XrayFileProvider(
        runtime=None,  # type: ignore[arg-type]
        deployment_config=XrayDeploymentConfig(
            xray_log_level="warning",
            reality_dest="dest.example:443",
            reality_server_names=("dest.example",),
        ),
    )
    with pytest.raises(XrayValidationError, match="static skeleton"):
        provider.render(DesiredRoutingState(), Resolver())

