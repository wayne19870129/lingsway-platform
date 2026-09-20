from pathlib import Path
from typing import cast

import httpx
import pytest
from sqlalchemy import Table, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.core.database import Base
from backend.app.core.secrets import SecretSnapshot
from backend.app.models import (
    MihomoTransportMaterialization,
    Secret,
    TransportEndpointRecord,
    TransportProviderRecord,
)
from backend.app.providers.transport.resolver import (
    SubscriptionTransportResolver,
    TransportProviderDescriptor,
    TransportResolutionError,
)
from backend.app.workers.transport_sync import refresh_provider_inventory


def test_resolver_uses_full_descriptor_and_isolates_cache(tmp_path: Path) -> None:
    def loader(_ref: str, _purpose: str) -> SecretSnapshot:
        return SecretSnapshot("https://provider.invalid/private", 1)

    resolver = SubscriptionTransportResolver(tmp_path)
    first = TransportProviderDescriptor(1, "A", "SUBSCRIPTION", "transport/a/url")
    second = TransportProviderDescriptor(2, "B", "SUBSCRIPTION", "transport/b/url")

    provider_a = resolver.resolve(first, loader)
    provider_b = resolver.resolve(second, loader)

    assert provider_a is not provider_b
    assert provider_a._cache_path != provider_b._cache_path  # noqa: SLF001
    assert resolver.resolve(first, loader) is provider_a
    with pytest.raises(TransportResolutionError, match="DESCRIPTOR_DRIFT"):
        resolver.resolve(
            TransportProviderDescriptor(1, "A2", "SUBSCRIPTION", "transport/a/url"),
            loader,
        )
    resolver.close()


@pytest.mark.parametrize(
    "descriptor, error",
    [
        (TransportProviderDescriptor(0, "A", "SUBSCRIPTION", "ref"), "DESCRIPTOR_INVALID"),
        (TransportProviderDescriptor(1, " ", "SUBSCRIPTION", "ref"), "DESCRIPTOR_INVALID"),
        (TransportProviderDescriptor(1, "A", "SELF_HOSTED", "ref"), "KIND_UNSUPPORTED"),
        (
            TransportProviderDescriptor(1, "A", "SUBSCRIPTION", "ref", "mock"),
            "IMPLEMENTATION_UNSUPPORTED",
        ),
    ],
)
def test_resolver_validates_descriptor_at_trust_boundary(
    tmp_path: Path,
    descriptor: TransportProviderDescriptor,
    error: str,
) -> None:
    resolver = SubscriptionTransportResolver(tmp_path)

    with pytest.raises(TransportResolutionError, match=error):
        resolver.resolve(descriptor, lambda _ref, _purpose: SecretSnapshot("unused", 1))


def test_resolver_rejects_resolution_after_shutdown(tmp_path: Path) -> None:
    resolver = SubscriptionTransportResolver(tmp_path)

    def loader(_ref: str, _purpose: str) -> SecretSnapshot:
        return SecretSnapshot("https://provider.invalid/private", 1)

    descriptor = TransportProviderDescriptor(1, "A", "SUBSCRIPTION", "ref")
    resolver.resolve(descriptor, loader)
    resolver.close()

    with pytest.raises(TransportResolutionError, match="SHUTDOWN"):
        resolver.resolve(TransportProviderDescriptor(2, "B", "SUBSCRIPTION", "ref-b"), loader)


def test_resolver_partial_close_is_truthful_and_idempotent_for_successes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def close(provider: object) -> None:
        code = provider.provider_code  # type: ignore[attr-defined]
        calls.append(code)
        if code == "A":
            raise RuntimeError("A close failed")

    monkeypatch.setattr(
        "backend.app.providers.transport.subscription.SubscriptionTransportProvider.close",
        close,
    )
    resolver = SubscriptionTransportResolver(tmp_path)

    def loader(_ref: str, _purpose: str) -> SecretSnapshot:
        return SecretSnapshot("https://provider.invalid/private", 1)

    resolver.resolve(TransportProviderDescriptor(1, "A", "SUBSCRIPTION", "a"), loader)
    resolver.resolve(TransportProviderDescriptor(2, "B", "SUBSCRIPTION", "b"), loader)

    with pytest.raises(ExceptionGroup, match="close failed"):
        resolver.close()
    with pytest.raises(TransportResolutionError, match="SHUTDOWN"):
        resolver.resolve(TransportProviderDescriptor(3, "C", "SUBSCRIPTION", "c"), loader)
    with pytest.raises(ExceptionGroup, match="close failed"):
        resolver.close()
    assert calls == ["A", "B", "A"]


def test_resolved_provider_materialization_proof_uses_bound_secret_revision(
    tmp_path: Path,
) -> None:
    resolver = SubscriptionTransportResolver(tmp_path)
    provider = resolver.resolve(
        TransportProviderDescriptor(1, "A", "SUBSCRIPTION", "ref"),
        lambda _ref, _purpose: SecretSnapshot("https://provider.invalid/private", 9),
    )
    provider._client = httpx.Client(  # noqa: SLF001
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                content=b"proxies:\n  - {name: A, type: trojan, server: a.invalid, port: 443}\n",
            )
        )
    )
    provider.sync_nodes()
    assert provider.materialization_proof() is not None
    assert provider.materialization_proof()[2] == 9  # type: ignore[index]


def test_reused_transport_provider_keeps_bound_source_revision(tmp_path: Path) -> None:
    resolver = SubscriptionTransportResolver(tmp_path)
    descriptor = TransportProviderDescriptor(1, "A", "SUBSCRIPTION", "ref")
    provider = resolver.resolve(descriptor, lambda _ref, _purpose: SecretSnapshot("url", 9))
    reused = resolver.resolve(descriptor, lambda _ref, _purpose: SecretSnapshot("url", 9))
    assert reused is provider
    assert provider._source_revision == 9  # noqa: SLF001


def test_transport_secret_revision_drift_still_fails_closed(tmp_path: Path) -> None:
    resolver = SubscriptionTransportResolver(tmp_path)
    descriptor = TransportProviderDescriptor(1, "A", "SUBSCRIPTION", "ref")
    resolver.resolve(descriptor, lambda _ref, _purpose: SecretSnapshot("url", 9))
    with pytest.raises(TransportResolutionError, match="SECRET_REVISION_DRIFT"):
        resolver.resolve(descriptor, lambda _ref, _purpose: SecretSnapshot("url", 10))


def test_refresh_inventory_uses_bound_proof_revision_not_current_secret_revision(
    tmp_path: Path,
) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=cast(
            list[Table],
            [
                TransportProviderRecord.__table__,
                TransportEndpointRecord.__table__,
                MihomoTransportMaterialization.__table__,
                Secret.__table__,
            ],
        ),
    )
    resolver = SubscriptionTransportResolver(tmp_path)
    try:
        with Session(engine) as db:
            provider_record = TransportProviderRecord(
                code="A",
                slug="a",
                name="A",
                secret_ref="transport/a/url",
            )
            db.add(provider_record)
            db.add(
                Secret(
                    secret_ref="transport/a/url",
                    ciphertext="current-secret",
                    purpose="TRANSPORT_SUBSCRIPTION_URL",
                    revision=10,
                )
            )
            db.commit()
            db.refresh(provider_record)

            provider = resolver.resolve(
                TransportProviderDescriptor(1, "A", "SUBSCRIPTION", "transport/a/url"),
                lambda _ref, _purpose: SecretSnapshot("https://provider.invalid/private", 9),
            )
            provider._client = httpx.Client(  # noqa: SLF001
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(
                        200,
                        content=(
                            b"proxies:\n  - {name: A, type: trojan, "
                            b"server: a.invalid, port: 443}\n"
                        ),
                    )
                )
            )

            assert refresh_provider_inventory(db, provider_record, provider) == 1
            receipt = db.scalar(
                select(MihomoTransportMaterialization).where(
                    MihomoTransportMaterialization.owner_record_id == provider_record.id
                )
            )
            proof = provider.materialization_proof()
            assert proof is not None
            assert receipt is not None
            assert receipt.source_revision == 9
            assert receipt.source_revision != 10
            assert (receipt.cache_identity, receipt.content_hash) == proof[:2]
    finally:
        resolver.close()
        engine.dispose()
