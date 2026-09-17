"""Webshare client with a non-bypassable write allowlist at its lowest layer."""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

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


class WebshareContractError(RuntimeError):
    """Raised when a response cannot be mapped without guessing its schema."""


class Response(Protocol):
    status_code: int

    def json(self) -> object: ...


@dataclass(slots=True)
class UrllibResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> object:
        import json

        return json.loads(self.body.decode("utf-8"))


class UrllibTransport:
    """Small transport used by procurement; policy remains in WebshareProvider."""

    def __init__(self, *, timeout: float = 20.0, proxy_url: str | None = None) -> None:
        self.timeout = timeout
        handler = ProxyHandler(
            {"http": proxy_url, "https": proxy_url} if proxy_url else {}
        )
        self._opener = build_opener(handler)

    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, object] | None,
        json: Mapping[str, object] | None,
    ) -> UrllibResponse:
        import json as json_module
        from urllib.parse import urlencode

        query = f"?{urlencode(params)}" if params else ""
        body = json_module.dumps(json).encode("utf-8") if json is not None else None
        request = Request(
            f"{url}{query}", data=body, headers=dict(headers), method=method
        )
        request.add_header("Accept", "application/json")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return UrllibResponse(
                    response.status, dict(response.headers.items()), response.read()
                )
        except HTTPError as error:
            return UrllibResponse(
                error.code, dict(error.headers.items()), error.read()
            )
        except URLError as error:
            raise ConnectionError(f"Webshare transport failed: {error.reason}") from error


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
        extra_headers: Mapping[str, str] | None = None,
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
        if extra_headers and any(key.lower() == "authorization" for key in extra_headers):
            raise WebshareGuardError("authorization header cannot be overridden")
        self._guard(normalized_method, normalized_path, dry_run=dry_run)
        self._general_limiter.acquire()
        if "/proxy/" in normalized_path:
            self._proxy_limiter.acquire()

        url = urljoin(self._base_url, normalized_path.lstrip("/"))
        for attempt in range(4):
            response = self._transport(
                normalized_method,
                url,
                headers={
                    "Authorization": f"Token {self._api_key}",
                    **(dict(extra_headers) if extra_headers else {}),
                },
                params=params,
                json=json,
            )
            if response.status_code != 429:
                return response
            if attempt == 3:
                import logging

                logging.getLogger(__name__).warning(
                    "webshare_429_exhausted attempts=%d", attempt + 1
                )
                raise WebshareRateLimitError("Webshare returned 429 after 3 retries")
            retry_after = _retry_after(response)
            self._sleep(retry_after if retry_after is not None else float(2**attempt))
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
        payloads = self._paginate(
            "/api/v2/proxy/list/", params={"mode": "direct", "page_size": 100}
        )
        return [_parse_endpoint(item) for item in payloads]

    def capacity(self) -> CapacityDTO:
        raise WebshareContractError(
            "capacity mapping is gated: Webshare does not expose the DTO's "
            "allocated_gb and reserved_gb fields in the recorded contract"
        )

    def create_tenant(self, label: str, quota_gb: Decimal, thread_limit: int) -> TenantDTO:
        response = self.request(
            "POST",
            "/api/v2/subuser/",
            json={
                "label": label,
                "proxy_limit": float(quota_gb),
                "max_thread_count": thread_limit,
            },
        )
        return _parse_tenant(response.json())

    def update_tenant_quota(self, tenant_id: str, quota_gb: Decimal) -> TenantDTO:
        response = self.request(
            "PATCH",
            f"/api/v2/subuser/{tenant_id}/",
            json={"proxy_limit": float(quota_gb)},
        )
        return _parse_tenant(response.json())

    def get_tenant_usage(self, tenant_id: str) -> UsageDTO:
        raise WebshareContractError(
            "tenant usage mapping is gated: the recorded Webshare aggregate-stats "
            "contract does not establish a byte unit for UsageDTO"
        )

    def get_credentials(self, tenant_id: str, endpoint_id: str) -> CredentialDTO:
        payloads = self._paginate(
            "/api/v2/proxy/list/",
            params={"mode": "direct", "page_size": 100},
            extra_headers={"X-Subuser": tenant_id},
        )
        for item in payloads:
            if item.get("id") == endpoint_id:
                username = _required_string(item, "username", "proxy credential")
                password = _required_string(item, "password", "proxy credential")
                return CredentialDTO(username, password)
        raise WebshareContractError("proxy endpoint was not found for credentials")

    def _paginate(
        self,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        next_path: str | None = path
        next_params = params
        while next_path:
            response = self.request(
                "GET", next_path, params=next_params, extra_headers=extra_headers
            )
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                raise WebshareContractError("Webshare paginated response is malformed")
            for item in payload["results"]:
                if not isinstance(item, dict):
                    raise WebshareContractError("Webshare result item is malformed")
                results.append(item)
            next_value = payload.get("next")
            if next_value:
                parsed_next = urlsplit(str(next_value))
                next_path = parsed_next.path
                next_params = dict(parse_qsl(parsed_next.query, keep_blank_values=True))
            else:
                next_path = None
                next_params = None
        return results

    def replace_endpoint(self, endpoint_id: str, dry_run: bool = True) -> ReplacementDTO:
        self.request("POST", f"/api/v3/proxy/replace/{endpoint_id}/", dry_run=dry_run)
        return ReplacementDTO(endpoint_id, None, dry_run)


class WebshareApiError(RuntimeError):
    """Parsed API error returned by the procurement adapter."""

    def __init__(self, message: str, status_code: int, payload: object) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = _redact_payload(payload)


def _required_string(item: Mapping[str, object], field: str, operation: str) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise WebshareContractError(f"{operation} field {field!r} is missing or malformed")
    return value


def _parse_endpoint(item: Mapping[str, object]) -> EgressEndpointDTO:
    endpoint_id = _required_string(item, "id", "proxy endpoint")
    host = _required_string(item, "proxy_address", "proxy endpoint")
    port = item.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise WebshareContractError("proxy endpoint field 'port' is missing or malformed")
    return EgressEndpointDTO(endpoint_id, host, port, protocol="socks5", public_ip=host)


def _parse_tenant(payload: object) -> TenantDTO:
    if not isinstance(payload, dict):
        raise WebshareContractError("Webshare sub-user response is malformed")
    tenant_id = payload.get("id")
    if isinstance(tenant_id, bool) or not isinstance(tenant_id, int):
        raise WebshareContractError("Webshare sub-user field 'id' is missing or malformed")
    label = _required_string(payload, "label", "sub-user")
    proxy_limit = payload.get("proxy_limit")
    if isinstance(proxy_limit, bool) or not isinstance(proxy_limit, (int, float, str)):
        raise WebshareContractError(
            "Webshare sub-user field 'proxy_limit' is missing or malformed"
        )
    thread_limit = payload.get("max_thread_count")
    if isinstance(thread_limit, bool) or not isinstance(thread_limit, int):
        raise WebshareContractError(
            "Webshare sub-user field 'max_thread_count' is missing or malformed"
        )
    try:
        quota_gb = Decimal(str(proxy_limit))
    except Exception as error:
        raise WebshareContractError(
            "Webshare sub-user field 'proxy_limit' is missing or malformed"
        ) from error
    if not quota_gb.is_finite() or quota_gb < 0:
        raise WebshareContractError(
            "Webshare sub-user field 'proxy_limit' is missing or malformed"
        )
    return TenantDTO(str(tenant_id), label, quota_gb, thread_limit)


def _redact_payload(payload: object) -> object:
    if isinstance(payload, dict):
        return {
            key: "<redacted>"
            if any(
                marker in key.lower()
                for marker in ("password", "username", "token", "api_key", "secret")
            )
            else _redact_payload(value)
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [_redact_payload(value) for value in payload]
    return payload


class WebshareReadOnlyAdapter:
    """Procurement facade: JSON and pagination, with a stricter write policy."""

    def __init__(self, provider: WebshareProvider) -> None:
        self._provider = provider

    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> object:
        parsed_url = urlsplit(path)
        normalized_path = _procurement_path(path)
        normalized_method = method.upper()
        dry_run = (
            json_body.get("dry_run") is True if json_body is not None else None
        )
        if normalized_method != "GET" and not (
            normalized_method == "POST"
            and normalized_path.startswith("/api/v3/proxy/replace/")
            and dry_run is True
        ):
            raise WebshareGuardError(
                "procurement adapter permits GET and dry_run replacement only"
            )
        response = self._provider.request(
            normalized_method,
            normalized_path,
            dry_run=dry_run,
            params=_merge_query_params(parsed_url.query, params),
            json=json_body,
        )
        payload = response.json()
        if response.status_code >= 400:
            raise WebshareApiError(
                f"Webshare API HTTP {response.status_code}",
                response.status_code,
                payload,
            )
        return payload

    def get(
        self, path: str, *, params: Mapping[str, object] | None = None
    ) -> object:
        return self.request_json("GET", path, params=params)

    def paginate(
        self, path: str, *, params: Mapping[str, object] | None = None
    ) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        next_path: str | None = path
        query = dict(params or {})
        while next_path:
            payload = self.get(next_path, params=query)
            query = {}
            if not isinstance(payload, dict):
                return results
            page = payload.get("results")
            if isinstance(page, list):
                results.extend(item for item in page if isinstance(item, dict))
            next_value = payload.get("next")
            next_path = str(next_value) if next_value else None
        return results


def _procurement_path(path: str) -> str:
    parsed = urlsplit(path)
    normalized = unquote(parsed.path)
    if normalized.startswith("/api/"):
        return normalized
    prefix = "/api/v3" if normalized.startswith(("/proxy/config", "/proxy/replace")) else "/api/v2"
    return f"{prefix}/{normalized.lstrip('/')}"


def _retry_after(response: Response) -> float | None:
    headers = getattr(response, "headers", {}) or {}
    value = next(
        (item for key, item in headers.items() if key.lower() == "retry-after"), None
    )
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _merge_query_params(
    query: str, params: Mapping[str, object] | None
) -> dict[str, object] | None:
    merged: dict[str, object] = dict(parse_qsl(query, keep_blank_values=True))
    if params:
        merged.update(params)
    return merged or None

