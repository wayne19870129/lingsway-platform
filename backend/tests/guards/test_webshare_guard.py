from dataclasses import dataclass

import pytest

from backend.app.providers.egress.webshare import (
    SlidingWindowLimiter,
    WebshareGuardError,
    WebshareProvider,
    WebshareRateLimitError,
)


@dataclass
class FakeResponse:
    status_code: int = 200

    def json(self) -> object:
        return {}


class Transport:
    def __init__(self, statuses: list[int] | None = None) -> None:
        self.statuses = statuses or [200]
        self.calls: list[tuple[object, ...]] = []

    def __call__(self, *args: object, **kwargs: object) -> FakeResponse:
        self.calls.append((*args, kwargs))
        index = min(len(self.calls) - 1, len(self.statuses) - 1)
        return FakeResponse(self.statuses[index])


@pytest.mark.parametrize(
    ("method", "path", "dry_run"),
    [
        ("POST", "/subscription/purchase", None),
        ("POST", "/subscription/renew", None),
        ("POST", "/subscription/auto_renewal", None),
        ("POST", "/payment/charge", None),
        ("PATCH", "/billing/account", None),
        ("POST", "/api/v3/proxy/replace/endpoint-1/", None),
        ("POST", "/api/v3/proxy/replace/endpoint-1/", False),
    ],
)
def test_every_forbidden_path_is_blocked_before_transport(
    method: str, path: str, dry_run: bool | None
) -> None:
    transport = Transport()
    provider = WebshareProvider("not-a-real-key", transport)

    with pytest.raises(WebshareGuardError):
        provider.request(method, path, dry_run=dry_run)

    assert transport.calls == []


@pytest.mark.parametrize("bypass_name", ["force", "override", "bypass"])
def test_bypass_keywords_cannot_override_a_block(bypass_name: str) -> None:
    transport = Transport()
    provider = WebshareProvider("not-a-real-key", transport)

    with pytest.raises(WebshareGuardError):
        if bypass_name == "force":
            provider.request("POST", "/subscription/purchase", force=True)
        elif bypass_name == "override":
            provider.request("POST", "/subscription/purchase", override=True)
        else:
            provider.request("POST", "/subscription/purchase", bypass=True)

    assert transport.calls == []


@pytest.mark.parametrize(
    ("method", "path", "dry_run"),
    [
        ("GET", "/anything", None),
        ("POST", "/api/v2/subuser/", None),
        ("PATCH", "/api/v2/subuser/tenant-1/", None),
        ("PUT", "/api/v2/subuser/tenant-1/", None),
        ("POST", "/api/v3/proxy/replace/endpoint-1/", True),
    ],
)
def test_allowlist_reaches_transport(method: str, path: str, dry_run: bool | None) -> None:
    transport = Transport()
    provider = WebshareProvider("not-a-real-key", transport)

    provider.request(method, path, dry_run=dry_run)

    assert len(transport.calls) == 1


def test_non_allowlisted_write_is_blocked() -> None:
    transport = Transport()
    provider = WebshareProvider("not-a-real-key", transport)
    with pytest.raises(WebshareGuardError):
        provider.request("DELETE", "/api/v2/subuser/tenant-1/")
    assert transport.calls == []


def test_429_uses_three_exponential_retries() -> None:
    sleeps: list[float] = []
    transport = Transport([429, 429, 429, 200])
    provider = WebshareProvider("not-a-real-key", transport, sleep=sleeps.append)

    provider.request("GET", "/api/v2/proxy/list/")

    assert sleeps == [1.0, 2.0, 4.0]
    assert len(transport.calls) == 4


def test_fourth_429_fails() -> None:
    transport = Transport([429, 429, 429, 429])
    provider = WebshareProvider("not-a-real-key", transport, sleep=lambda _delay: None)
    with pytest.raises(WebshareRateLimitError):
        provider.request("GET", "/api/v2/proxy/list/")


def test_general_and_proxy_limiters_are_both_applied() -> None:
    events: list[str] = []

    class RecordingLimiter(SlidingWindowLimiter):
        def acquire(self) -> None:
            events.append(str(self.limit))

    provider = WebshareProvider(
        "not-a-real-key",
        Transport(),
        general_limiter=RecordingLimiter(240),
        proxy_limiter=RecordingLimiter(60),
    )
    provider.request("GET", "/api/v2/proxy/list/")
    assert events == ["240", "60"]
