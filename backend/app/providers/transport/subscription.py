from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import yaml

from backend.app.providers.base import (
    TransportCapacityDTO,
    TransportEndpointDTO,
)

SUPPORTED_URI_SCHEMES = {"vless", "vmess", "trojan", "ss", "socks", "socks5", "http", "https"}


def parse_subscription_userinfo(
    value: str | None, measured_at: datetime | None = None
) -> TransportCapacityDTO | None:
    if not value:
        return None
    fields: dict[str, int] = {}
    try:
        for item in value.split(";"):
            key, raw = item.strip().split("=", 1)
            fields[key.strip().lower()] = int(raw.strip())
        quota = fields["total"]
        used = fields.get("upload", 0) + fields.get("download", 0)
    except (KeyError, ValueError):
        return None
    expire = fields.get("expire")
    return TransportCapacityDTO(
        quota_bytes=quota,
        used_bytes=used,
        measured_at=measured_at or datetime.now(UTC),
        expire_at=datetime.fromtimestamp(expire, UTC) if expire else None,
    )


def endpoint_id(provider_code: str, protocol: str, host: str, port: int, name: str) -> str:
    value = f"{provider_code}|{protocol}|{host}|{port}|{name}".encode()
    return hashlib.sha256(value).hexdigest()[:24]


def normalize_clash_proxy(
    provider_code: str, item: dict[str, object]
) -> TransportEndpointDTO:
    protocol = str(item.get("type", "unknown")).lower()
    host = str(item.get("server", ""))
    port = int(cast(str | int, item.get("port", 0)))
    name = str(item.get("name", f"{protocol}-{host}:{port}"))
    external_id = endpoint_id(provider_code, protocol, host, port, name)
    network = item.get("network") or item.get("transport")
    tls_mode = "tls" if item.get("tls") else None
    return TransportEndpointDTO(
        external_id=external_id,
        name=name,
        region="UNKNOWN",
        protocol=protocol,
        host=host,
        port=port,
        transport=str(network) if network else None,
        tls_mode=tls_mode,
        auth_secret_ref=f"transport/{provider_code}/endpoint/{external_id}",
        raw_metadata={"udp": bool(item.get("udp", False))},
    )


def decode_base64_text(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding).decode("utf-8")


def normalize_uri(provider_code: str, uri: str) -> TransportEndpointDTO | None:
    if uri.startswith("vmess://"):
        try:
            body = json.loads(decode_base64_text(uri.removeprefix("vmess://")))
            return normalize_clash_proxy(
                provider_code,
                {
                    "name": body.get("ps", "vmess"),
                    "type": "vmess",
                    "server": body["add"],
                    "port": int(body["port"]),
                    "network": body.get("net"),
                    "tls": body.get("tls") == "tls",
                },
            )
        except (ValueError, KeyError, json.JSONDecodeError, UnicodeDecodeError):
            return None
    parsed = urlparse(uri)
    if parsed.scheme not in SUPPORTED_URI_SCHEMES or not parsed.hostname or not parsed.port:
        return None
    query = parse_qs(parsed.query)
    name = unquote(parsed.fragment) or f"{parsed.scheme}-{parsed.hostname}:{parsed.port}"
    return normalize_clash_proxy(
        provider_code,
        {
            "name": name,
            "type": parsed.scheme,
            "server": parsed.hostname,
            "port": parsed.port,
            "network": query.get("type", [None])[0],
            "tls": query.get("security", [None])[0] in {"tls", "reality"},
        },
    )


def parse_subscription(provider_code: str, content: str) -> list[TransportEndpointDTO]:
    if not content.strip():
        raise ValueError("Subscription content is empty")
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ValueError("Subscription is not valid YAML or encoded URI data") from exc
    if isinstance(document, dict) and isinstance(document.get("proxies"), list):
        endpoints: list[TransportEndpointDTO] = []
        for item in document["proxies"]:
            if not isinstance(item, dict) or not item.get("server") or not item.get("port"):
                raise ValueError("Subscription contains a malformed proxy entry")
            try:
                endpoints.append(normalize_clash_proxy(provider_code, item))
            except (TypeError, ValueError):
                raise ValueError("Subscription contains a malformed proxy entry") from None
        if not endpoints:
            raise ValueError("Subscription contains no supported endpoints")
        return endpoints
    decoded = content.strip()
    if "://" not in decoded:
        try:
            decoded = decode_base64_text(decoded)
        except (ValueError, UnicodeDecodeError):
            raise ValueError("Subscription is not valid base64 URI data") from None
    endpoints: list[TransportEndpointDTO] = []
    for line in decoded.splitlines():
        value = line.strip()
        if not value:
            continue
        try:
            endpoint = normalize_uri(provider_code, value)
        except (TypeError, ValueError):
            raise ValueError("Subscription contains an unsupported or malformed URI") from None
        if endpoint is None:
            raise ValueError("Subscription contains an unsupported or malformed URI")
        endpoints.append(endpoint)
    if not endpoints:
        raise ValueError("Subscription contains no supported endpoints")
    return endpoints


def validate_mihomo_provider_document(content: str) -> None:
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ValueError("Subscription is not valid YAML for a Mihomo file provider") from exc
    if not isinstance(document, dict) or not isinstance(document.get("proxies"), list):
        raise ValueError("Subscription is not a Mihomo-compatible proxies document")
    if not document["proxies"]:
        raise ValueError("Subscription provider file contains no proxies")


def write_provider_cache(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as temporary:
            temporary_name = temporary.name
            os.chmod(temporary.name, 0o600)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
    finally:
        if temporary_name is not None:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name)


class SubscriptionTransportProvider:
    def __init__(
        self,
        provider_code: str,
        subscription_url: str,
        client: httpx.Client | None = None,
        cache_path: Path | None = None,
    ) -> None:
        self.provider_code = provider_code
        self._url = subscription_url
        # TASK-T16 Phase 2B8: this provider must never close a client it
        # did not create itself -- an injected client (e.g. a shared
        # pool, or a test's httpx.MockTransport-backed client) outlives
        # this provider and is the injecting caller's own resource.
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=20.0, follow_redirects=True)
        self._cache_path = cache_path
        self._enabled = True
        self._endpoints: list[TransportEndpointDTO] = []
        self._capacity: TransportCapacityDTO | None = None
        self._close_failed = False

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(provider_code={self.provider_code!r}, "
            f"cache_path={self._cache_path!r})"
        )

    def close(self) -> None:
        """Closes the self-created client, never a client this provider
        did not create.

        Independent review (PR #65, round 4): round 3 removed this
        provider's own "already closed" flag on the theory that
        ``httpx.Client.close()`` is always safe to call again, including
        as a genuine retry after a prior failure. That is false for the
        real library: ``httpx.Client.close()`` (confirmed in the
        installed 0.28.1) sets its internal state to CLOSED *before*
        calling the underlying transport's ``close()``. If the transport
        close then raises, the client is already marked CLOSED, and a
        later ``self._client.close()`` call sees CLOSED and returns
        without ever retrying the transport cleanup -- silently turning a
        still-failed close into an apparent success.

        Because of that, once a close attempt has raised, this provider
        can never truly retry the same client's cleanup -- httpx will not
        let it. So this method tracks a permanent ``_close_failed`` flag
        set only when a close attempt actually raises, and keeps
        re-raising on every subsequent call rather than calling
        ``self._client.close()`` again (which would just no-op and look
        like success). This is deliberately less convenient than a real
        retry, but it never lies about whether cleanup succeeded.
        """
        if not self._owns_client:
            return
        if self._close_failed:
            raise RuntimeError(
                "SubscriptionTransportProvider's owned httpx.Client previously "
                "failed to close; httpx.Client.close() cannot be safely retried "
                "once it has raised (it marks its internal state CLOSED before "
                "actually closing the transport), so this failure is permanent "
                "for this provider instance and must keep surfacing rather than "
                "being silently treated as success"
            )
        try:
            self._client.close()
        except Exception:
            self._close_failed = True
            raise

    def health_check(self) -> bool:
        return self._enabled and bool(self._endpoints)

    def sync_nodes(self) -> None:
        # Providers commonly negotiate a Mihomo-compatible YAML document from
        # the Clash.Meta user agent. A generic "mihomo" UA may instead return
        # a base64 URI list, which cannot be consumed by a file provider.
        try:
            response = self._client.get(
                self._url, headers={"User-Agent": "clash.meta/1.19"}
            )
            response.raise_for_status()
        except httpx.HTTPError:
            raise RuntimeError("Subscription fetch failed") from None
        capacity = parse_subscription_userinfo(
            response.headers.get("subscription-userinfo"), datetime.now(UTC)
        )
        endpoints = parse_subscription(self.provider_code, response.text)
        if self._cache_path is not None:
            validate_mihomo_provider_document(response.text)
            write_provider_cache(self._cache_path, response.content)
        # Publish the new snapshot only after parsing and cache persistence succeed.
        self._endpoints = endpoints
        self._capacity = capacity

    def list_endpoints(self) -> list[TransportEndpointDTO]:
        return list(self._endpoints)

    def get_endpoint(self, external_id: str) -> TransportEndpointDTO | None:
        return next((item for item in self._endpoints if item.external_id == external_id), None)

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    def get_capacity(self) -> TransportCapacityDTO | None:
        return self._capacity
