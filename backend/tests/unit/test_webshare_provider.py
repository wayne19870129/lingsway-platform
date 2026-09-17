from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from backend.app.providers.egress.webshare import (
    WebshareApiError,
    WebshareContractError,
    WebshareProvider,
)


@dataclass
class FakeResponse:
    payload: object
    status_code: int = 200
    headers: dict[str, str] | None = None

    def json(self) -> object:
        return self.payload


class Transport:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, *args: object, **kwargs: object) -> FakeResponse:
        self.calls.append((*args, kwargs))
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


def test_list_endpoints_maps_documented_proxy_objects_and_pagination() -> None:
    transport = Transport(
        [
            FakeResponse(
                {
                    "count": 2,
                    "next": "https://proxy.invalid/api/v2/proxy/list/?page=2&mode=direct",
                    "results": [
                        {
                            "id": "d-1",
                            "proxy_address": "198.51.100.1",
                            "port": 8168,
                        }
                    ],
                }
            ),
            FakeResponse(
                {
                    "count": 2,
                    "next": None,
                    "results": [
                        {
                            "id": "d-2",
                            "proxy_address": "198.51.100.2",
                            "port": 1080,
                        }
                    ],
                }
            ),
        ]
    )
    provider = WebshareProvider("api-key-placeholder", transport)

    endpoints = provider.list_endpoints()

    assert [(item.endpoint_id, item.host, item.port) for item in endpoints] == [
        ("d-1", "198.51.100.1", 8168),
        ("d-2", "198.51.100.2", 1080),
    ]
    assert transport.calls[0][2]["params"] == {"mode": "direct", "page_size": 100}
    assert transport.calls[1][2]["params"] == {"page": "2", "mode": "direct"}


def test_list_endpoints_fails_closed_for_malformed_payload() -> None:
    provider = WebshareProvider(
        "api-key-placeholder",
        Transport([FakeResponse({"results": [{"id": "d-1"}]})]),
    )

    with pytest.raises(WebshareContractError):
        provider.list_endpoints()


def test_get_credentials_maps_documented_proxy_credentials_without_repr_leak() -> None:
    transport = Transport(
        [
            FakeResponse(
                {
                    "next": None,
                    "results": [
                        {
                            "id": "d-1",
                            "proxy_address": "198.51.100.1",
                            "port": 8168,
                            "username": "synthetic-user",
                            "password": "synthetic-password",
                        }
                    ],
                }
            )
        ]
    )
    provider = WebshareProvider("api-key-placeholder", transport)

    credentials = provider.get_credentials("7", "d-1")

    assert credentials.username == "synthetic-user"
    assert credentials.password == "synthetic-password"
    assert "synthetic-password" not in repr(credentials)
    assert transport.calls[0][2]["headers"]["X-Subuser"] == "7"


def test_tenant_write_responses_are_mapped_instead_of_fabricated() -> None:
    payload = {
        "id": 7,
        "label": "synthetic-tenant",
        "proxy_limit": 12.5,
        "max_thread_count": 42,
    }
    transport = Transport([FakeResponse(payload), FakeResponse(payload)])
    provider = WebshareProvider("api-key-placeholder", transport)

    created = provider.create_tenant(
        "synthetic-tenant", quota_gb=Decimal("12.5"), thread_limit=42
    )
    updated = provider.update_tenant_quota("7", quota_gb=Decimal("12.5"))

    assert created.tenant_id == updated.tenant_id == "7"
    assert created.quota_gb == updated.quota_gb
    assert transport.calls[0][2]["json"] == {
        "label": "synthetic-tenant",
        "proxy_limit": 12.5,
        "max_thread_count": 42,
    }
    assert transport.calls[1][2]["json"] == {"proxy_limit": 12.5}


def test_unresolved_capacity_and_usage_are_explicitly_gated() -> None:
    provider = WebshareProvider("api-key-placeholder", Transport([]))

    with pytest.raises(WebshareContractError, match="capacity mapping is gated"):
        provider.capacity()
    with pytest.raises(WebshareContractError, match="tenant usage mapping is gated"):
        provider.get_tenant_usage("7")


def test_api_error_redacts_credential_like_payload_fields() -> None:
    error = WebshareApiError(
        "Webshare API HTTP 400",
        400,
        {"username": "synthetic-user", "proxy_password": "synthetic-password"},
    )

    assert "synthetic-user" not in repr(error)
    assert "synthetic-password" not in repr(error)
    assert error.payload == {
        "username": "<redacted>",
        "proxy_password": "<redacted>",
    }

