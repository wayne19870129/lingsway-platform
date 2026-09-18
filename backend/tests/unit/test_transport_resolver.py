from pathlib import Path

import pytest

from backend.app.core.secrets import SecretSnapshot
from backend.app.providers.transport.resolver import (
    SubscriptionTransportResolver,
    TransportProviderDescriptor,
    TransportResolutionError,
)


class _Session:
    def close(self) -> None:
        return None


def test_resolver_uses_full_descriptor_and_isolates_cache(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "backend.app.providers.transport.resolver.reveal_secret_snapshot_for_purpose",
        lambda *_args: SecretSnapshot("https://provider.invalid/private", 1),
    )
    resolver = SubscriptionTransportResolver(tmp_path, _Session)
    first = TransportProviderDescriptor(1, "A", "SUBSCRIPTION", "transport/a/url")
    second = TransportProviderDescriptor(2, "B", "SUBSCRIPTION", "transport/b/url")

    provider_a = resolver.resolve(first)
    provider_b = resolver.resolve(second)

    assert provider_a is not provider_b
    assert provider_a._cache_path != provider_b._cache_path  # noqa: SLF001
    assert resolver.resolve(first) is provider_a
    with pytest.raises(TransportResolutionError, match="DESCRIPTOR_DRIFT"):
        resolver.resolve(TransportProviderDescriptor(1, "A2", "SUBSCRIPTION", "transport/a/url"))
    resolver.close()
