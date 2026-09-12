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


class _FlakyTransport(httpx.BaseTransport):
    """A real ``httpx.BaseTransport`` whose ``close()`` raises for the
    first ``fail_times`` calls, then succeeds -- used to drive a real
    ``httpx.Client`` through its actual ``state=CLOSED ->
    transport.close()`` close order (confirmed in the installed httpx
    0.28.1), rather than monkeypatching ``Client.close`` itself before it
    ever runs."""

    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.close_calls = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, text="proxies: []\n")

    def close(self) -> None:
        self.close_calls += 1
        if self.close_calls <= self.fail_times:
            raise RuntimeError("simulated transport close failure")


def test_close_permanently_surfaces_failure_when_the_owned_transport_close_raises() -> None:
    """Independent review (PR #65, round 4): httpx 0.28.1's
    ``Client.close()`` sets its internal state to CLOSED *before* calling
    the transport's own ``close()`` -- so once a real close attempt has
    raised, the client is already marked closed and a later
    ``Client.close()`` call silently no-ops instead of retrying. Round 3's
    fix (trusting ``httpx.Client.close()`` to be a safe, genuine retry)
    does not hold against the real library. This test drives a real
    ``httpx.Client`` through a ``BaseTransport`` whose ``close()`` raises,
    so it exercises httpx's actual close order instead of a monkeypatched
    stand-in."""
    transport = _FlakyTransport(fail_times=1)
    adapter = SubscriptionTransportProvider("PROVIDER_A", "https://provider.invalid/private")
    assert adapter._owns_client is True  # noqa: SLF001
    # Swap in a real httpx.Client bound to the flaky transport while
    # keeping _owns_client True -- the provider's own constructor has no
    # way to inject a custom transport for a self-owned client, so this
    # replicates "the client this provider owns happens to fail to close"
    # without going through the (owns_client=False) injection path.
    adapter._client = httpx.Client(transport=transport)  # noqa: SLF001

    with pytest.raises(RuntimeError, match="simulated transport close failure"):
        adapter.close()
    assert transport.close_calls == 1

    # httpx has already marked its Client CLOSED internally even though the
    # transport close raised -- a naive retry of `self._client.close()`
    # would now silently no-op and be mistaken for success. The provider
    # must keep surfacing the original failure instead of pretending it
    # succeeded.
    with pytest.raises(RuntimeError, match="previously failed to close"):
        adapter.close()
    assert transport.close_calls == 1  # never actually retried -- httpx would just no-op


def test_registry_close_never_falsely_reports_the_transport_provider_closed() -> None:
    """Reproduces the exact scenario from independent review (PR #65,
    round 4) at the ProviderRegistry level, using a real
    ``httpx.Client``/``BaseTransport`` rather than a monkeypatched close:
    the transport provider's close failure must surface via
    ``ExceptionGroup``, a later-iterated provider must still be closed,
    and a second ``registry.close()`` must never mark the transport
    provider as successfully closed -- since httpx's own close() can
    never truly retry a failed transport close, the provider must keep
    failing rather than silently succeeding."""
    transport = _FlakyTransport(fail_times=999)  # always fails
    adapter = SubscriptionTransportProvider("PROVIDER_A", "https://provider.invalid/private")
    assert adapter._owns_client is True  # noqa: SLF001
    adapter._client = httpx.Client(transport=transport)  # noqa: SLF001
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
    assert transport.close_calls == 1
    assert len(other.close_calls) == 1  # closed despite the earlier failure elsewhere

    with pytest.raises(ExceptionGroup):
        registry.close()  # retry: must keep failing, never silently succeed

    assert transport.close_calls == 1  # httpx never actually retries the transport close
    assert len(other.close_calls) == 1  # never re-closed
