from dataclasses import dataclass
from typing import Any

import pytest

from backend.app.providers.egress.webshare import (
    WebshareGuardError,
    WebshareProvider,
    WebshareReadOnlyAdapter,
)


@dataclass
class FakeResponse:
    status_code: int = 200
    payload: object = None
    headers: dict[str, str] | None = None

    def json(self) -> object:
        return {} if self.payload is None else self.payload


class Transport:
    def __init__(self, responses: list[FakeResponse] | None = None) -> None:
        self.responses = responses or [FakeResponse()]
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, *args: object, **kwargs: object) -> FakeResponse:
        self.calls.append((*args, kwargs))
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]


def test_adapter_rejects_forbidden_request_before_transport() -> None:
    transport = Transport()
    adapter = WebshareReadOnlyAdapter(WebshareProvider("key", transport))

    with pytest.raises(WebshareGuardError):
        adapter.request_json("POST", "/subscription/purchase")

    assert transport.calls == []


def test_procurement_adapter_cannot_use_provider_subuser_write_allowlist() -> None:
    transport = Transport()
    adapter = WebshareReadOnlyAdapter(WebshareProvider("key", transport))

    with pytest.raises(WebshareGuardError):
        adapter.request_json("POST", "/api/v2/subuser/", json_body={"label": "x"})

    assert transport.calls == []


def test_adapter_returns_json_and_follows_pagination() -> None:
    transport = Transport(
        [
            FakeResponse(payload={"results": [{"id": 1}], "next": "/proxy/list/?page=2"}),
            FakeResponse(payload={"results": [{"id": 2}], "next": None}),
        ]
    )
    adapter = WebshareReadOnlyAdapter(WebshareProvider("key", transport))

    assert adapter.paginate("/proxy/list/", params={"page_size": 100}) == [
        {"id": 1},
        {"id": 2},
    ]
    assert transport.calls[0][1].endswith("/api/v2/proxy/list/")
    assert transport.calls[1][1].endswith("/api/v2/proxy/list/")
    assert transport.calls[1][2]["params"] == {"page": "2"}


def test_retry_after_header_is_used_as_exact_delay() -> None:
    sleeps: list[float] = []
    transport = Transport(
        [
            FakeResponse(429, headers={"Retry-After": "90"}),
            FakeResponse(200),
        ]
    )
    provider = WebshareProvider("key", transport, sleep=sleeps.append)

    provider.request("GET", "/api/v2/proxy/list/")

    assert sleeps == [90.0]


def test_missing_retry_after_uses_one_two_four_seconds() -> None:
    sleeps: list[float] = []
    transport = Transport(
        [FakeResponse(429), FakeResponse(429), FakeResponse(429), FakeResponse(200)]
    )
    provider = WebshareProvider("key", transport, sleep=sleeps.append)

    provider.request("GET", "/api/v2/proxy/list/")

    assert sleeps == [1.0, 2.0, 4.0]
