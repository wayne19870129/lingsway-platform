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
    allocate_generation,
    build_projection_source_manifest,
    manifest_fingerprint,
)
from backend.app.providers.base import DesiredForwarderState
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
