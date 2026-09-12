"""Production-capable ``AccountingProvider`` HTTP adapter for a patched
pinned Marzban v0.8.4 admin API (ADR-016 Decision 3, Candidate B).

TASK-T16 Phase 2B7 scope: this module implements the adapter and its
offline HTTP contract tests only. **Nothing in this repository wires it
into ``backend/app/providers/registry.py`` or selects it via
``ACCOUNTING_PROVIDER=marzban`` yet** -- that is deliberately a separate,
later, explicit opt-in task, so merging this file produces zero real
external side effects. No real Marzban instance is contacted by any
code path in this repository today.

Pinned upstream: `github.com/Gozargah/Marzban` commit
`7f396db3e703d71a28060bc9ce4a532ec64cb1f4` (tag `v0.8.4`) -- the same
commit `infrastructure/marzban/patches/` builds its Candidate B source
patch and contract tests against (see that directory's ``README.md`` and
``pinned_upstream_manifest.py``, which remain this repository's single
source of truth for the pinned commit and its verified file hashes).
This module does not vendor or copy any Marzban source; it only speaks
the pinned, publicly documented HTTP contract:

- ``POST /api/admin/token`` (``app/routers/admin.py::admin_token``,
  ``response_model=Token`` from ``app/models/admin.py``) -- OAuth2
  password-grant form body, never JSON.
- ``POST /api/user`` (``app/routers/user.py::add_user``,
  ``response_model=UserResponse``) -- create.
- ``GET /api/user/{username}`` (``app/routers/user.py::get_user``,
  ``response_model=UserResponse``) -- read (used for usage/links).
- ``PUT /api/user/{username}`` (``app/routers/user.py::modify_user``,
  ``response_model=UserResponse``) -- disable/quota/expire updates.

``routing_principal`` on every ``UserResponse`` these three endpoints
return is the Candidate B patch field (ADR-016); this adapter only reads
it, never computes ``f"{id}.{username}"`` itself -- that formula is
Marzban's own responsibility (see ``app/xray/operations.py`` in the
pinned commit, and ``pinned_upstream_manifest.py``'s
``XRAY_EMAIL_FORMULA`` for its exact-source record).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import quote

import httpx

from backend.app.providers.base import AccountUserDTO, UsageDTO

#: Marzban's own documented httpx default is far too generous for a
#: synchronous provisioning request path; an explicit, finite timeout is
#: required everywhere this adapter opens its own client (ADR-016/TASK-T16
#: Phase 2B7 -- "不使用无超时请求").
DEFAULT_TIMEOUT_SECONDS = 20.0

#: The four protocols the pinned Marzban commit's `ProxyTypes` enum
#: (`app/models/proxy.py`) supports. Recorded here as plain string
#: literals (not vendored code) purely to fail closed on a configured
#: protocol Marzban does not support -- see `_parse_default_inbounds`.
_SUPPORTED_PROTOCOLS = frozenset({"vmess", "vless", "trojan", "shadowsocks"})

#: Pinned `UserStatus` values (`app/models/user.py`) this adapter treats
#: as "the account is meant to be usable" when mapping a response's
#: `status` string to `AccountUserDTO.enabled`. `disabled`/`limited`/
#: `expired` all map to `enabled=False`.
_ENABLED_STATUSES = frozenset({"active", "on_hold"})


class MarzbanApiError(RuntimeError):
    """A Marzban HTTP call failed: transport failure, an unexpected
    status code, or a malformed response body.

    ``operation`` and ``status_code`` (when known) are always attached
    for callers/logs; the exception's own message is a stable,
    non-sensitive summary -- it must never embed the admin password, a
    bearer token, an ``Authorization`` header, subscription links, or a
    full response payload (which could itself carry any of those).
    """

    def __init__(
        self, message: str, *, operation: str, status_code: int | None = None
    ) -> None:
        self.operation = operation
        self.status_code = status_code
        super().__init__(message)


class MarzbanContractError(MarzbanApiError):
    """A Marzban response did not satisfy the pinned/ADR-016 contract
    this adapter depends on: a missing/blank/non-string
    ``routing_principal``, a username that doesn't match the request, a
    malformed field type, or (at construction time) an invalid
    ``marzban_default_inbounds_json``/unsupported protocol. Always
    fail-closed, never a fallback to a locally-computed or guessed
    value.
    """


def _quote_username(username: str) -> str:
    return quote(username, safe="")


def _parse_default_inbounds(
    protocol: str, default_inbounds_json: str
) -> tuple[str, tuple[str, ...]]:
    """Strictly parse+validate ``marzban_default_inbounds_json`` for one
    configured protocol. Fails closed (``MarzbanContractError``) on any
    of: invalid JSON, a non-object top level, an unsupported protocol, a
    missing/empty/non-list inbound-tag value for that protocol, or any
    blank/non-string tag. Never falls back to an arbitrary inbound."""
    protocol = protocol.strip()
    if protocol not in _SUPPORTED_PROTOCOLS:
        raise MarzbanContractError(
            f"configured protocol {protocol!r} is not one of the Marzban-supported "
            f"protocols {sorted(_SUPPORTED_PROTOCOLS)}",
            operation="configure",
        )
    try:
        parsed = json.loads(default_inbounds_json)
    except (TypeError, ValueError) as exc:
        raise MarzbanContractError(
            "marzban_default_inbounds_json is not valid JSON", operation="configure"
        ) from exc
    if not isinstance(parsed, dict):
        raise MarzbanContractError(
            "marzban_default_inbounds_json must be a JSON object", operation="configure"
        )
    tags_raw = parsed.get(protocol)
    if not isinstance(tags_raw, list) or not tags_raw:
        raise MarzbanContractError(
            f"marzban_default_inbounds_json has no non-empty inbound tag list for "
            f"protocol {protocol!r}",
            operation="configure",
        )
    tags: list[str] = []
    for tag in tags_raw:
        if not isinstance(tag, str) or not tag.strip():
            raise MarzbanContractError(
                "marzban_default_inbounds_json contains a blank or non-string "
                "inbound tag",
                operation="configure",
            )
        tags.append(tag)
    return protocol, tuple(tags)


def _map_expire_to_marzban(expire_at: datetime | None) -> int:
    """``datetime | None`` -> Marzban's ``UserCreate``/``UserModify``
    ``expire`` contract (Unix timestamp seconds, or ``0``).

    ``None`` maps to ``0`` -- the pinned ``POST /api/user`` endpoint's own
    docstring (`app/routers/user.py::add_user`, exact pinned source)
    states plainly: "expire: UTC timestamp for account expiration. Use 0
    for unlimited." A naive (tzinfo-less) datetime is rejected rather
    than silently interpreted in the local timezone -- this repository's
    existing contracts never document a "naive means UTC/local" rule for
    this field, so guessing one would be exactly the kind of silent
    fallback ADR-016 forbids.
    """
    if expire_at is None:
        return 0
    if expire_at.utcoffset() is None:
        raise MarzbanContractError(
            "expire_at must be timezone-aware; a naive datetime cannot be safely "
            "interpreted as a specific UTC instant",
            operation="expire_mapping",
        )
    return int(expire_at.astimezone(UTC).timestamp())


def _expire_from_marzban(value: object, *, operation: str) -> datetime | None:
    """Reverse of :func:`_map_expire_to_marzban` for response parsing.
    Both ``null`` and ``0`` map back to ``None`` (no expiration), for
    symmetry with the "0 for unlimited" contract on the way in. Anything
    other than ``None``/a plain ``int`` (a bool, float, or negative value)
    fails closed."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise MarzbanContractError(
            f"Marzban {operation} response 'expire' was not an integer or null",
            operation=operation,
        )
    if value <= 0:
        return None
    return datetime.fromtimestamp(value, UTC)


def _json_object(response: httpx.Response, *, operation: str) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise MarzbanContractError(
            f"Marzban {operation} response was not valid JSON", operation=operation
        ) from exc
    if not isinstance(payload, dict):
        raise MarzbanContractError(
            f"Marzban {operation} response was not a JSON object", operation=operation
        )
    return payload


def _require_str(payload: Mapping[str, object], key: str, *, operation: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MarzbanContractError(
            f"Marzban {operation} response is missing a usable {key!r} field",
            operation=operation,
        )
    return value


def _require_non_negative_int(payload: Mapping[str, object], key: str, *, operation: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool):
        raise MarzbanContractError(
            f"Marzban {operation} response {key!r} was a bool, not an integer",
            operation=operation,
        )
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or value < 0:
        raise MarzbanContractError(
            f"Marzban {operation} response {key!r} was not a non-negative integer",
            operation=operation,
        )
    return value


def _parse_account_user(
    payload: Mapping[str, object], *, expected_username: str, operation: str
) -> AccountUserDTO:
    """Shared, minimal response parser for the one path that needs a full
    ``AccountUserDTO`` today (``create_user()``'s ``POST /api/user``
    response) -- kept separate from the smaller field-level helpers above
    so a future caller needing the same parsing from ``GET
    /api/user/{username}`` (a different pinned endpoint returning the
    identical, same-patched ``UserResponse`` shape) can reuse it without
    a second, independently-drifting ``routing_principal`` contract.
    ``get_connection_links()``/``get_usage()`` do not need an
    ``AccountUserDTO`` and read their own one or two fields directly
    rather than going through this."""
    username = _require_str(payload, "username", operation=operation)
    if username != expected_username:
        raise MarzbanContractError(
            "Marzban response username did not match the requested username",
            operation=operation,
        )
    routing_principal = payload.get("routing_principal")
    if not isinstance(routing_principal, str) or not routing_principal.strip():
        # Missing, null, non-string, empty, or whitespace-only -- all one
        # fail-closed outcome. ADR-016 forbids falling back to username
        # or a locally-computed principal here under any circumstance.
        raise MarzbanContractError(
            "Marzban response is missing a usable routing_principal (the "
            "Candidate B patch may not be applied to this Marzban image)",
            operation=operation,
        )
    quota_bytes = _require_non_negative_int(payload, "data_limit", operation=operation)
    expire_at = _expire_from_marzban(payload.get("expire"), operation=operation)
    status = _require_str(payload, "status", operation=operation)
    return AccountUserDTO(
        username=username,
        quota_bytes=quota_bytes,
        expire_at=expire_at,
        routing_principal=routing_principal,
        enabled=status in _ENABLED_STATUSES,
    )


class MarzbanAccountingProvider:
    """``AccountingProvider`` backed by a patched, pinned Marzban v0.8.4
    admin API. See this module's docstring for the exact endpoints used
    and ADR-016 Decision 3 for the ``routing_principal`` contract.

    Not registered with ``providers/registry.py`` -- constructing this
    class is safe (no I/O happens in ``__init__`` beyond validating
    ``default_inbounds_json``), but nothing in this repository does so
    outside its own tests yet.
    """

    def __init__(
        self,
        *,
        base_url: str,
        admin_username: str,
        admin_password: str,
        default_protocol: str,
        default_inbounds_json: str,
        verify_tls: bool = True,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        base_url = base_url.strip().rstrip("/")
        if not base_url:
            raise MarzbanContractError("base_url must not be blank", operation="configure")
        self._base_url = base_url
        self._admin_username = admin_username
        self._admin_password = admin_password
        self._protocol, self._inbound_tags = _parse_default_inbounds(
            default_protocol, default_inbounds_json
        )
        self._owns_client = client is None
        self._client = client or httpx.Client(verify=verify_tls, timeout=timeout_seconds)
        self._token: str | None = None

    def __repr__(self) -> str:
        # Deliberately not the dataclass-style "every field" repr: the
        # admin password and any cached bearer token must never appear in
        # a repr, exception message, log line, or test snapshot.
        return (
            f"{type(self).__name__}(base_url={self._base_url!r}, "
            f"admin_username={self._admin_username!r}, protocol={self._protocol!r})"
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -- auth -----------------------------------------------------------

    def _authenticate(self) -> str:
        try:
            response = self._client.post(
                f"{self._base_url}/api/admin/token",
                data={"username": self._admin_username, "password": self._admin_password},
            )
        except httpx.HTTPError as exc:
            raise MarzbanApiError(
                "Marzban admin token request failed at the transport layer",
                operation="authenticate",
            ) from exc
        if response.status_code != 200:
            raise MarzbanApiError(
                "Marzban admin authentication was rejected",
                operation="authenticate",
                status_code=response.status_code,
            )
        payload = _json_object(response, operation="authenticate")
        token = payload.get("access_token")
        if not isinstance(token, str) or not token.strip():
            raise MarzbanApiError(
                "Marzban admin token response is missing access_token",
                operation="authenticate",
            )
        token_type = payload.get("token_type")
        if token_type is not None and not (
            isinstance(token_type, str) and token_type.lower() == "bearer"
        ):
            raise MarzbanApiError(
                "Marzban admin token response token_type was not 'bearer'",
                operation="authenticate",
            )
        return token

    def _dispatch(
        self, method: str, path: str, *, operation: str, json_body: Mapping[str, object] | None
    ) -> httpx.Response:
        try:
            return self._client.request(
                method,
                f"{self._base_url}{path}",
                json=json_body,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.HTTPError as exc:
            raise MarzbanApiError(
                f"Marzban {operation} request failed at the transport layer",
                operation=operation,
            ) from exc

    def _call(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        json_body: Mapping[str, object] | None = None,
    ) -> httpx.Response:
        """Send one authenticated request. On a ``401`` response (and
        only a ``401`` response -- never a transport exception), clears
        the cached token, re-authenticates exactly once, and retries the
        *same* request exactly once; a second ``401`` fails closed. No
        other automatic retry exists anywhere in this adapter: a
        transport-level failure (timeout, connection reset, etc.) always
        propagates immediately, so a POST/PUT with an ambiguous external
        result (e.g. ``create_user()``'s ``POST /api/user`` failing after
        the request may already have reached the server) is never
        silently repeated.
        """
        if self._token is None:
            self._token = self._authenticate()
        response = self._dispatch(method, path, operation=operation, json_body=json_body)
        if response.status_code == 401:
            self._token = None
            self._token = self._authenticate()
            response = self._dispatch(method, path, operation=operation, json_body=json_body)
            if response.status_code == 401:
                raise MarzbanApiError(
                    "Marzban rejected the re-authenticated request (401 twice)",
                    operation=operation,
                    status_code=401,
                )
        return response

    # -- AccountingProvider contract -------------------------------------

    def create_user(
        self, username: str, quota_bytes: int, expire_at: datetime | None
    ) -> AccountUserDTO:
        body = {
            "username": username,
            "status": "active",
            "data_limit": quota_bytes,
            "data_limit_reset_strategy": "no_reset",
            "expire": _map_expire_to_marzban(expire_at),
            # Pinned VLESSSettings/VMessSettings/TrojanSettings/
            # ShadowsocksSettings (app/models/proxy.py, exact source
            # verified) all default every field via `default_factory`, so
            # an empty object is a valid, verified request for any of the
            # four supported protocols -- Marzban generates the
            # UUID/password server-side. Lingsway never generates this
            # material itself.
            "proxies": {self._protocol: {}},
            "inbounds": {self._protocol: list(self._inbound_tags)},
        }
        response = self._call("POST", "/api/user", operation="create_user", json_body=body)
        if response.status_code == 409:
            # No 409-reconciliation idempotency policy exists in this
            # adapter -- ADR-016/TASK-T16 Phase 2B7 explicitly reserves
            # that as a separate, unmade architecture decision. Fail
            # closed; ProvisioningService's own CREATE_ACCOUNTING_USER
            # compensation handles this exception like any other.
            raise MarzbanApiError(
                "Marzban already has a user with this username (409); no "
                "reconciliation is implemented, failing closed",
                operation="create_user",
                status_code=409,
            )
        if response.status_code != 200:
            raise MarzbanApiError(
                "Marzban rejected create_user",
                operation="create_user",
                status_code=response.status_code,
            )
        payload = _json_object(response, operation="create_user")
        return _parse_account_user(payload, expected_username=username, operation="create_user")

    def disable_user(self, username: str) -> None:
        # AGENTS.md 铁律 4 / ADR-016: disable, never DELETE.
        response = self._call(
            "PUT",
            f"/api/user/{_quote_username(username)}",
            operation="disable_user",
            json_body={"status": "disabled"},
        )
        if response.status_code != 200:
            raise MarzbanApiError(
                "Marzban rejected disable_user",
                operation="disable_user",
                status_code=response.status_code,
            )

    def set_quota(self, username: str, quota_bytes: int) -> None:
        response = self._call(
            "PUT",
            f"/api/user/{_quote_username(username)}",
            operation="set_quota",
            json_body={"data_limit": quota_bytes},
        )
        if response.status_code != 200:
            raise MarzbanApiError(
                "Marzban rejected set_quota",
                operation="set_quota",
                status_code=response.status_code,
            )

    def set_expire(self, username: str, expire_at: datetime) -> None:
        response = self._call(
            "PUT",
            f"/api/user/{_quote_username(username)}",
            operation="set_expire",
            json_body={"expire": _map_expire_to_marzban(expire_at)},
        )
        if response.status_code != 200:
            raise MarzbanApiError(
                "Marzban rejected set_expire",
                operation="set_expire",
                status_code=response.status_code,
            )

    def get_connection_links(self, username: str) -> list[str]:
        response = self._call(
            "GET", f"/api/user/{_quote_username(username)}", operation="get_connection_links"
        )
        if response.status_code != 200:
            raise MarzbanApiError(
                "Marzban rejected get_connection_links",
                operation="get_connection_links",
                status_code=response.status_code,
            )
        payload = _json_object(response, operation="get_connection_links")
        links = payload.get("links")
        if not isinstance(links, list) or not all(isinstance(item, str) for item in links):
            raise MarzbanContractError(
                "Marzban get_connection_links response 'links' was not a list of "
                "strings",
                operation="get_connection_links",
            )
        return list(links)

    def get_usage(self, username: str) -> UsageDTO:
        response = self._call(
            "GET", f"/api/user/{_quote_username(username)}", operation="get_usage"
        )
        if response.status_code != 200:
            raise MarzbanApiError(
                "Marzban rejected get_usage",
                operation="get_usage",
                status_code=response.status_code,
            )
        payload = _json_object(response, operation="get_usage")
        used_bytes = _require_non_negative_int(payload, "used_traffic", operation="get_usage")
        status = _require_str(payload, "status", operation="get_usage")
        return UsageDTO(
            tenant_id=username,
            used_gb=Decimal(used_bytes) / Decimal(1024**3),
            measured_at=datetime.now(UTC),
            used_bytes=used_bytes,
            status=status,
        )
