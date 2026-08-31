"""Webshare client with a non-bypassable write allowlist at its lowest layer."""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol
from urllib.parse import unquote, urljoin, urlsplit

from backend.app.providers.base import (
    CapacityDTO,
    CredentialDTO,
    EgressEndpointDTO,
    ReplacementDTO,
    TenantDTO,
    UsageDTO,
)


class WebshareGuardError(PermissionError):
    """Raised before transport when an operation is not explicitly allowed."""


class WebshareRateLimitError(RuntimeError):
    pass


class Response(Protocol):
    status_code: int

    def json(self) -> object: ...


Transport = Callable[..., Response]


@dataclass(slots=True)
class SlidingWindowLimiter:
    limit: int
    window_seconds: float = 60.0
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    _events: deque[float] = field(default_factory=deque)

    def acquire(self) -> None:
        now = self.clock()
        while self._events and self._events[0] <= now - self.window_seconds:
            self._events.popleft()
        if len(self._events) >= self.limit:
            wait = self.window_seconds - (now - self._events[0])
            self.sleep(max(wait, 0.0))
            now = self.clock()
            while self._events and self._events[0] <= now - self.window_seconds:
                self._events.popleft()
        self._events.append(now)


class WebshareProvider:
    name = "webshare"
    _SUBUSER = re.compile(r"^/api/v2/subuser/[^/]+/$")
    _DRY_REPLACEMENT = re.compile(r"^/api/v3/proxy/replace/.+")

    def __init__(
        self,
        api_key: str,
        transport: Transport,
        *,
        base_url: str = "https://proxy.webshare.io",
        sleep: Callable[[float], None] = time.sleep,
        general_limiter: SlidingWindowLimiter | None = None,
        proxy_limiter: SlidingWindowLimiter | None = None,
    ) -> None:
        self._api_key = api_key
        self._transport = transport
        self._base_url = base_url.rstrip("/") + "/"
        self._sleep = sleep
        self._general_limiter = general_limiter or SlidingWindowLimiter(240)
        self._proxy_limiter = proxy_limiter or SlidingWindowLimiter(60)

    def request(
        self,
        method: str,
        path: str,
        *,
        dry_run: bool | None = None,
        params: Mapping[str, object] | None = None,
        json: Mapping[str, object] | None = None,
        **options: object,
    ) -> Response:
        """Validate policy before rate limiting or invoking the transport.

        Unknown keyword arguments cannot change the policy result. Explicit bypass
        spellings are rejected so they can never become accidental escape hatches.
        """
        normalized_method = method.upper()
        normalized_path = unquote(urlsplit(path).path)
        if {"force", "override", "bypass"}.intersection(options):
            raise WebshareGuardError("force/override/bypass options are not supported")
        self._guard(normalized_method, normalized_path, dry_run=dry_run)
        self._general_limiter.acquire()
        if "/proxy/" in normalized_path:
            self._proxy_limiter.acquire()

        url = urljoin(self._base_url, normalized_path.lstrip("/"))
        for attempt in range(4):
            response = self._transport(
                normalized_method,
                url,
                headers={"Authorization": f"Token {self._api_key}"},
                params=params,
                json=json,
            )
            if response.status_code != 429:
                return response
            if attempt == 3:
                raise WebshareRateLimitError("Webshare returned 429 after 3 retries")
            self._sleep(float(2**attempt))
        raise AssertionError("unreachable")

    @classmethod
    def _guard(cls, method: str, path: str, *, dry_run: bool | None) -> None:
        if method == "GET":
            return
        forbidden = (
            "/subscription/purchase",
            "/subscription/renew",
            "/subscription/auto_renewal",
            "/payment/",
            "/billing/",
        )
        if any(blocked in path for blocked in forbidden):
            raise WebshareGuardError(f"Webshare write is permanently blocked: {path}")
        if "/proxy/replace/" in path and dry_run is not True:
            raise WebshareGuardError("replacement requires dry_run=true")
        if method == "POST" and path == "/api/v2/subuser/":
            return
        if method in {"PATCH", "PUT"} and cls._SUBUSER.fullmatch(path):
            return
        if method == "POST" and cls._DRY_REPLACEMENT.fullmatch(path) and dry_run is True:
            return
        raise WebshareGuardError(f"Webshare write is not allowlisted: {method} {path}")

    # Provider DTO adapters. All network operations still pass through request().
    def list_endpoints(self) -> list[EgressEndpointDTO]:
        self.request("GET", "/api/v2/proxy/list/")
        return []

    def capacity(self) -> CapacityDTO:
        self.request("GET", "/api/v2/subscription/")
        return CapacityDTO(Decimal(0), Decimal(0), Decimal(0))

    def create_tenant(self, label: str, quota_gb: Decimal, thread_limit: int) -> TenantDTO:
        self.request("POST", "/api/v2/subuser/", json={"label": label})
        return TenantDTO(label, label, quota_gb, thread_limit)

    def update_tenant_quota(self, tenant_id: str, quota_gb: Decimal) -> TenantDTO:
        self.request("PATCH", f"/api/v2/subuser/{tenant_id}/", json={"quota": str(quota_gb)})
        return TenantDTO(tenant_id, tenant_id, quota_gb, 0)

    def get_tenant_usage(self, tenant_id: str) -> UsageDTO:
        raise NotImplementedError("response mapping arrives with T5 migration")

    def get_credentials(self, tenant_id: str, endpoint_id: str) -> CredentialDTO:
        raise NotImplementedError("response mapping arrives with T5 migration")

    def replace_endpoint(self, endpoint_id: str, dry_run: bool = True) -> ReplacementDTO:
        self.request("POST", f"/api/v3/proxy/replace/{endpoint_id}/", dry_run=dry_run)
        return ReplacementDTO(endpoint_id, None, dry_run)
