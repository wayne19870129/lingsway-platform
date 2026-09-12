import base64
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from backend.app.core.config import Settings
from backend.app.providers.registry import ProviderRegistry, build_registry
from backend.app.providers.transport.subscription import (
    SubscriptionTransportProvider,
    parse_subscription,
    parse_subscription_userinfo,
)


def test_parse_clash_subscription_sanitizes_credentials() -> None:
    content = """
proxies:
  - name: JP-01
    type: vless
    server: jp.example.invalid
    port: 443
    uuid: secret-uuid
    tls: true
    network: ws
"""
    endpoints = parse_subscription("PROVIDER_A", content)
    assert len(endpoints) == 1
    assert endpoints[0].protocol == "vless"
    assert endpoints[0].tls_mode == "tls"
    metadata = endpoints[0].raw_metadata
    auth_secret_ref = endpoints[0].auth_secret_ref
    assert metadata is not None
    assert auth_secret_ref is not None
    assert "secret-uuid" not in str(metadata)
    assert auth_secret_ref.startswith("transport/PROVIDER_A/")


def test_parse_base64_uri_subscription() -> None:
    plain = "vless://uuid@jp.example.invalid:443?security=tls&type=ws#JP-01\n"
    encoded = base64.urlsafe_b64encode(plain.encode()).decode().rstrip("=")
    endpoints = parse_subscription("PROVIDER_B", encoded)
    assert len(endpoints) == 1
    assert endpoints[0].name == "JP-01"


def test_subscription_adapter_refreshes_without_exposing_url() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            text="proxies:\n  - {name: US-01, type: trojan, server: us.invalid, port: 443}\n",
            headers={
                "subscription-userinfo": "upload=100; download=200; total=1000; expire=1893456000"
            },
        )
    )
    adapter = SubscriptionTransportProvider(
        "PROVIDER_A",
        "https://provider.invalid/private-token",
        client=httpx.Client(transport=transport),
    )
    adapter.sync_nodes()
    assert adapter.health_check() is True
    assert adapter.list_endpoints()[0].host == "us.invalid"
    capacity = adapter.get_capacity()
    assert capacity is not None
    assert capacity.quota_bytes == 1000
    assert capacity.used_bytes == 300


def test_subscription_userinfo_missing_or_malformed_is_unknown() -> None:
    assert parse_subscription_userinfo(None) is None
    assert parse_subscription_userinfo("upload=x; download=2; total=10") is None
    assert parse_subscription_userinfo("upload=1; download=2") is None


def test_successful_sync_atomically_updates_mihomo_provider_file(tmp_path: Path) -> None:
    content = "proxies:\n  - {name: US, type: trojan, server: us.invalid, port: 443}\n"
    target = tmp_path / "provider.yaml"
    adapter = SubscriptionTransportProvider(
        "CAP",
        "https://provider.invalid/private",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=content.encode())
            )
        ),
        cache_path=target,
    )
    adapter.sync_nodes()
    assert target.read_text() == content
    assert not target.with_suffix(".yaml.tmp").exists()


def test_failed_sync_preserves_last_known_good_provider_file(tmp_path: Path) -> None:
    target = tmp_path / "provider.yaml"
    target.write_text(
        "proxies:\n  - {name: OLD, type: trojan, server: old.invalid, port: 443}\n"
    )
    adapter = SubscriptionTransportProvider(
        "CAP",
        "https://provider.invalid/private",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, text="proxies: []\n")
            )
        ),
        cache_path=target,
    )
    with pytest.raises(ValueError):
        adapter.sync_nodes()
    assert "OLD" in target.read_text()


# ---------------------------------------------------------------------------
# TASK-T16 Phase 2B8: provider resource lifecycle (_owns_client / close()).
# ---------------------------------------------------------------------------


def test_close_closes_a_self_created_client() -> None:
    """When no client is injected, SubscriptionTransportProvider builds
    its own httpx.Client -- close() must actually close that owned
    client. Previously this provider had no ownership tracking or close()
    at all, leaking one httpx.Client per instance."""
    adapter = SubscriptionTransportProvider("PROVIDER_A", "https://provider.invalid/private")
    assert adapter._owns_client is True  # noqa: SLF001
    client = adapter._client  # noqa: SLF001
    assert client.is_closed is False

    adapter.close()

    assert client.is_closed is True


def test_close_never_closes_an_injected_client() -> None:
    """An externally injected client is the injecting caller's own
    resource -- close() must never close it out from under them."""
    injected_client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="proxies: []\n"))
    )
    adapter = SubscriptionTransportProvider(
        "PROVIDER_A", "https://provider.invalid/private", client=injected_client
    )
    assert adapter._owns_client is False  # noqa: SLF001

    adapter.close()

    assert injected_client.is_closed is False
    injected_client.close()


def test_close_is_idempotent_and_safe_to_call_multiple_times() -> None:
    adapter = SubscriptionTransportProvider("PROVIDER_A", "https://provider.invalid/private")

    adapter.close()
    adapter.close()
    adapter.close()

    assert adapter._client.is_closed is True  # noqa: SLF001


@dataclass(slots=True)
class _ClosableProvider:
    """Minimal stand-in for another owned provider, used only to prove it
    is still closed even when a sibling provider's close() fails."""

    close_calls: list[None] = field(default_factory=list)

    def close(self) -> None:
        self.close_calls.append(None)


def test_close_retries_correctly_when_the_owned_client_close_call_fails() -> None:
    """Independent review (PR #65, round 3): a self-tracked ``_closed``
    flag set *before* attempting ``self._client.close()`` would let a
    failed close be reported as a false success on a later retry. Drives
    the provider's own owned client through a failing close followed by a
    successful retry, proving the provider itself carries no such flag --
    it relies entirely on ``httpx.Client.close()``'s own safe-to-retry
    contract."""
    adapter = SubscriptionTransportProvider("PROVIDER_A", "https://provider.invalid/private")
    real_client = adapter._client  # noqa: SLF001
    calls: list[None] = []
    original_close = real_client.close

    def flaky_close() -> None:
        calls.append(None)
        if len(calls) == 1:
            raise RuntimeError("simulated transient close failure")
        original_close()

    real_client.close = flaky_close  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="simulated transient close failure"):
        adapter.close()
    assert len(calls) == 1
    assert real_client.is_closed is False  # the failed attempt never actually closed it

    adapter.close()  # retry: must actually retry the close, never silently no-op

    assert len(calls) == 2
    assert real_client.is_closed is True


def test_registry_close_surfaces_transport_close_failure_and_retries_it_only() -> None:
    """Reproduces the exact scenario from independent review (PR #65,
    round 3) at the ProviderRegistry level: a SubscriptionTransportProvider
    whose owned client's close() fails once must (1) surface that failure
    via ProviderRegistry.close()'s ExceptionGroup, (2) not prevent other
    owned providers from being closed in the same sweep, and (3) actually
    retry -- never silently mark itself successfully closed -- on the next
    registry.close() call."""
    adapter = SubscriptionTransportProvider("PROVIDER_A", "https://provider.invalid/private")
    real_client = adapter._client  # noqa: SLF001
    calls: list[None] = []
    original_close = real_client.close

    def flaky_close() -> None:
        calls.append(None)
        if len(calls) == 1:
            raise RuntimeError("simulated transient close failure")
        original_close()

    real_client.close = flaky_close  # type: ignore[method-assign]

    other = _ClosableProvider()
    defaults = build_registry(Settings())
    registry = ProviderRegistry(
        egress=adapter,  # type: ignore[arg-type]
        accounting=defaults.accounting,
        gateway=defaults.gateway,
        forwarder=defaults.forwarder,
        payment=defaults.payment,
        notify=defaults.notify,
        email=defaults.email,
        captcha=defaults.captcha,
        storage=defaults.storage,
        transport=other,  # type: ignore[arg-type]
    )

    with pytest.raises(ExceptionGroup):
        registry.close()
    assert len(calls) == 1
    assert real_client.is_closed is False
    assert len(other.close_calls) == 1  # closed despite the earlier failure elsewhere

    registry.close()  # retry: only the transport provider needed a second attempt

    assert len(calls) == 2
    assert real_client.is_closed is True
    assert len(other.close_calls) == 1  # never re-closed
