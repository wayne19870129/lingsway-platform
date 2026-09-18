from pathlib import Path

import pytest

from backend.app.core.secrets import SecretSnapshot
from backend.app.providers.transport.resolver import (
    SubscriptionTransportResolver,
    TransportProviderDescriptor,
    TransportResolutionError,
)


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
