from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.core.config import Settings
from backend.app.core.database import Base
from backend.app.infra import credential_resolver
from backend.app.infra.mihomo_generation import (
    MANIFEST_VERSION,
    allocate_generation,
    build_projection_source_manifest,
    manifest_fingerprint,
)
from backend.app.providers.base import DesiredForwarderState, ForwarderEgressProxyDTO
from backend.app.providers.forwarder.mihomo_projection import TransportMaterialization
from backend.app.providers.registry import ProviderConfigurationError, build_registry


def test_mihomo_forwarder_is_still_rejected() -> None:
    with pytest.raises(ProviderConfigurationError, match="FORWARDER_PROVIDER"):
        build_registry(Settings(forwarder_provider="mihomo"))


def _generation_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.tables["mihomo_projection_generations"].create(engine)
    return Session(engine)


def _desired(marker: str = "A") -> DesiredForwarderState:
    return DesiredForwarderState(
        proxies=({"name": marker, "type": "mock"},),
        deployment_constants={"external-controller": "192.0.2.1:9090", "api-secret-ref": "ref"},
        snapshot_revision=1,
        snapshot_identity="input",
    )


def _egress(name: str, ref: str = "egress/ref") -> ForwarderEgressProxyDTO:
    return ForwarderEgressProxyDTO(
        name=name,
        protocol="socks5",
        host="192.0.2.10",
        port=1080,
        credential_secret_ref=ref,
        credential_revision=3,
        transport_proxy_name="transport-node",
        transport_node_identity=("node", "192.0.2.20", 443),
    )


def test_manifest_includes_egress_proxies_without_reordering() -> None:
    desired = replace(_desired(), egress_proxies=(_egress("egress-a"), _egress("egress-b")))
    manifest = build_projection_source_manifest(desired)
    egress_manifest = cast(list[dict[str, object]], manifest["egress_proxies"])
    assert MANIFEST_VERSION == "2"
    assert [item["name"] for item in egress_manifest] == ["egress-a", "egress-b"]
    assert egress_manifest[0]["transport_proxy_name"] == "transport-node"
    assert egress_manifest[0]["transport_node_identity"] == [
        "node", "192.0.2.20", 443
    ]
    assert egress_manifest[0]["credential_secret_ref"] == "egress/ref"


def test_manifest_version_is_bumped_for_egress_proxies_shape() -> None:
    with _generation_session() as db:
        manifest = build_projection_source_manifest(_desired())
        legacy = allocate_generation(
            db, {**manifest, "manifest_version": "1"}, manifest_version="1"
        )
        current = allocate_generation(db, manifest)
        assert legacy.manifest_version == "1"
        assert current.manifest_version == "2"
        assert current.revision > legacy.revision
        assert allocate_generation(db, manifest).revision == current.revision


def test_egress_proxy_dto_repr_redacts_credential_ref() -> None:
    value = _egress("egress-a", "SECRET_REF_SENTINEL")
    assert "SECRET_REF_SENTINEL" not in repr(value)
    assert "SECRET_REF_SENTINEL" in repr(build_projection_source_manifest(
        replace(_desired(), egress_proxies=(value,))
    ))


def test_desired_forwarder_state_accepts_egress_proxies_and_freezes() -> None:
    value = _egress("egress-a")
    desired = replace(_desired(), egress_proxies=(value,))
    assert desired.egress_proxies == (value,)
    assert desired.egress_proxies[0].transport_node_identity == ("node", "192.0.2.20", 443)
    with pytest.raises(AttributeError):
        desired.egress_proxies[0].name = "changed"  # type: ignore[misc]


def test_generation_reuses_only_exact_latest_manifest() -> None:
    with _generation_session() as db:
        manifest = build_projection_source_manifest(_desired())
        first = allocate_generation(db, manifest)
        same = allocate_generation(db, manifest)
        assert same.revision == first.revision
        different = allocate_generation(db, build_projection_source_manifest(_desired("B")))
        assert different.revision > first.revision


def test_generation_a_b_a_allocates_new_revision() -> None:
    with _generation_session() as db:
        a = allocate_generation(db, build_projection_source_manifest(_desired("A")))
        b = allocate_generation(db, build_projection_source_manifest(_desired("B")))
        again = allocate_generation(db, build_projection_source_manifest(_desired("A")))
        assert a.revision < b.revision < again.revision
        assert again.revision != a.revision


def test_manifest_excludes_materialized_raw_content_and_plaintext_secret() -> None:
    material = TransportMaterialization(
        1,
        "provider",
        2,
        "cache-id",
        "a" * 64,
        datetime.now(UTC),
        {"proxies": [{"password": "SENTINEL"}]},
    )
    desired = _desired()
    desired = replace(desired, transport_materializations=(material,))
    manifest = build_projection_source_manifest(desired)
    encoded = str(manifest)
    assert "SENTINEL" not in encoded
    assert "plaintext" not in encoded


def test_manifest_datetime_and_order_are_canonical() -> None:
    instant = datetime(2026, 1, 1, 12, tzinfo=UTC)
    first_material = TransportMaterialization(
        1, "provider", 2, "cache", "a" * 64, instant, {"z": 1, "a": 2}
    )
    second_material = TransportMaterialization(
        1,
        "provider",
        2,
        "cache",
        "a" * 64,
        instant.astimezone(timezone(timedelta(hours=8))),
        {"a": 2, "z": 1},
    )
    first = build_projection_source_manifest(
        replace(
            _desired(),
            transport_materializations=(first_material,),
            dns={"enable": True, "ipv6": False},
        )
    )
    second = build_projection_source_manifest(
        replace(
            _desired(),
            transport_materializations=(second_material,),
            dns={"ipv6": False, "enable": True},
        )
    )
    assert manifest_fingerprint(first) == manifest_fingerprint(second)
    naive = build_projection_source_manifest(
        replace(
            _desired(),
            transport_materializations=(
                replace(first_material, freshness_deadline=datetime(2026, 1, 1, 12)),
            ),
        )
    )
    aware = build_projection_source_manifest(
        replace(
            _desired(),
            transport_materializations=(
                replace(first_material, freshness_deadline=datetime(2026, 1, 1, 12, tzinfo=UTC)),
            ),
        )
    )
    assert manifest_fingerprint(naive) == manifest_fingerprint(aware)


def test_sql_controller_secret_resolver_requires_exact_purpose_and_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def helper(_db: object, _ref: str, purpose: str) -> SimpleNamespace:
        seen.append(purpose)
        return SimpleNamespace(value="secret", revision=7)

    monkeypatch.setattr(
        credential_resolver,
        "reveal_secret_snapshot_for_purpose",
        helper,
    )
    resolver = credential_resolver.SqlMihomoControllerSecretResolver(
        cast(Session, SimpleNamespace())
    )
    result = resolver.resolve("controller-ref")
    assert result.ref == "controller-ref"
    assert result.revision == 7
    assert seen == ["MIHOMO_CONTROLLER_API_SECRET"]
