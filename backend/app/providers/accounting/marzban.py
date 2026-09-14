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

from backend.app.providers.base import (
    AccountingCreateEffect,
    AccountingCreateUserError,
    AccountUserDTO,
    UsageDTO,
)

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

#: The complete, exact-source pinned `UserStatus` enum (`app/models/user.py`).
#: A response `status` field outside this set fails closed
#: (`MarzbanContractError`) rather than being accepted as an opaque
#: passthrough string -- this repository must never treat an unrecognized
#: status as safe to interpret either way.
_PINNED_USER_STATUSES = frozenset({"active", "disabled", "limited", "expired", "on_hold"})

#: Values within `_PINNED_USER_STATUSES` this adapter treats as "the
#: account is meant to be usable" when mapping a response's `status`
#: string to `AccountUserDTO.enabled`. `disabled`/`limited`/`expired` all
#: map to `enabled=False`.
_ENABLED_STATUSES = frozenset({"active", "on_hold"})

#: `create_user()` always sends `status="active"` in its request body
#: (see its own body construction) -- exact-source pinned `UserStatusCreate`
#: only allows `active`/`on_hold`, and this adapter never requests
#: `on_hold`. A create response reporting any other status therefore means
#: the just-created account is not in the state this request asked for --
#: an ADR-018 `CREATED`-effect postcondition failure, not a usable success.
_CREATE_SUCCESS_STATUS = "active"


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


class MarzbanAuthenticationError(MarzbanApiError):
    """The admin token request itself failed, or an authenticated
    request was rejected with ``401`` twice in a row (re-authentication
    did not help). ADR-018: pinned exact source
    (``app/models/admin.py::Admin.get_current``, a FastAPI ``Depends()``
    on every admin-only route including ``POST /api/user``) proves this
    dependency always runs -- and can therefore always reject the
    request -- *before* that route's own body (and thus before
    ``crud.create_user()``) ever executes. Any failure of this class
    therefore means the operation it was raised for was never dispatched
    to the server with valid credentials: for ``create_user()`` this is
    always ``AccountingCreateEffect.NO_SIDE_EFFECT``, never ``AMBIGUOUS``.
    """


class MarzbanTransportError(MarzbanApiError):
    """An already-dispatched authenticated request (not the admin-token
    request itself, and not a clean ``401`` rejection) failed at the
    transport layer -- a timeout, connection reset, or otherwise lost
    response. Unlike :class:`MarzbanAuthenticationError`, the request may
    have already reached the server: for ``create_user()`` this is always
    ``AccountingCreateEffect.AMBIGUOUS``, never ``NO_SIDE_EFFECT``.
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
    epoch_seconds = int(expire_at.astimezone(UTC).timestamp())
    if epoch_seconds <= 0:
        # `0` is reserved by the pinned contract to mean "unlimited" (see
        # the `None` branch above) -- silently sending a computed epoch
        # of `0` or a negative value for a real, timezone-aware datetime
        # would be misinterpreted by Marzban as "no expiration" instead
        # of failing loudly. Fail closed rather than guess.
        raise MarzbanContractError(
            "expire_at maps to a non-positive Marzban epoch timestamp "
            f"({epoch_seconds}); Marzban's 'expire=0' sentinel means "
            "unlimited, so a real expiration before/at the Unix epoch "
            "cannot be safely represented",
            operation="expire_mapping",
        )
    return epoch_seconds


def _expire_from_marzban(value: object, *, operation: str) -> datetime | None:
    """Reverse of :func:`_map_expire_to_marzban` for response parsing.
    ``null``/``0`` both map back to ``None`` (no expiration), for
    symmetry with the "0 for unlimited" contract on the way in. A
    negative value has no defined meaning in the pinned contract and
    fails closed rather than being silently treated as unlimited.
    Anything other than ``None``/a plain ``int`` (a bool or a float)
    also fails closed."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise MarzbanContractError(
            f"Marzban {operation} response 'expire' was not an integer or null",
            operation=operation,
        )
    if value == 0:
        return None
    if value < 0:
        raise MarzbanContractError(
            f"Marzban {operation} response 'expire' was negative ({value}); "
            "the pinned contract has no defined meaning for a negative "
            "expire value",
            operation=operation,
        )
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


def _require_pinned_status(payload: Mapping[str, object], key: str, *, operation: str) -> str:
    """Like :func:`_require_str`, but additionally requires the value be
    one of the exact-source pinned ``UserStatus`` enum members
    (`_PINNED_USER_STATUSES`). An unrecognized status string fails closed
    rather than being passed through as an opaque value this codebase
    cannot reason about."""
    value = _require_str(payload, key, operation=operation)
    if value not in _PINNED_USER_STATUSES:
        raise MarzbanContractError(
            f"Marzban {operation} response {key!r} {value!r} is not one of the "
            f"pinned UserStatus values {sorted(_PINNED_USER_STATUSES)}",
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
    status = _require_pinned_status(payload, "status", operation=operation)
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
        self._close_failed = False

    def __repr__(self) -> str:
        # Deliberately not the dataclass-style "every field" repr: the
        # admin password and any cached bearer token must never appear in
        # a repr, exception message, log line, or test snapshot.
        return (
            f"{type(self).__name__}(base_url={self._base_url!r}, "
            f"admin_username={self._admin_username!r}, protocol={self._protocol!r})"
        )

    def close(self) -> None:
        """Closes the self-created client, never a client this provider
        did not create.

        Independent review (PR #65, round 4): ``httpx.Client.close()``
        (confirmed in the installed 0.28.1) sets its internal state to
        CLOSED *before* calling the underlying transport's ``close()``.
        If the transport close then raises, the client is already marked
        CLOSED, so a later ``self._client.close()`` call would silently
        no-op instead of retrying -- turning a still-failed close into an
        apparent success. Once a close attempt has actually raised, this
        provider can never truly retry the same client's cleanup, so it
        tracks a permanent ``_close_failed`` flag and keeps re-raising on
        every later call instead of calling ``self._client.close()``
        again (see ``SubscriptionTransportProvider.close()`` for the same
        fix and the full rationale)."""
        if not self._owns_client:
            return
        if self._close_failed:
            raise RuntimeError(
                "MarzbanAccountingProvider's owned httpx.Client previously failed "
                "to close; httpx.Client.close() cannot be safely retried once it "
                "has raised (it marks its internal state CLOSED before actually "
                "closing the transport), so this failure is permanent for this "
                "provider instance and must keep surfacing rather than being "
                "silently treated as success"
            )
        try:
            self._client.close()
        except Exception:
            self._close_failed = True
            raise

    # -- auth -----------------------------------------------------------

    def _authenticate(self) -> str:
        try:
            response = self._client.post(
                f"{self._base_url}/api/admin/token",
                data={"username": self._admin_username, "password": self._admin_password},
            )
        except httpx.HTTPError as exc:
            raise MarzbanAuthenticationError(
                "Marzban admin token request failed at the transport layer",
                operation="authenticate",
            ) from exc
        if response.status_code != 200:
            raise MarzbanAuthenticationError(
                "Marzban admin authentication was rejected",
                operation="authenticate",
                status_code=response.status_code,
            )
        payload = _json_object(response, operation="authenticate")
        token = payload.get("access_token")
        if not isinstance(token, str) or not token.strip():
            raise MarzbanAuthenticationError(
                "Marzban admin token response is missing access_token",
                operation="authenticate",
            )
        token_type = payload.get("token_type")
        if token_type is not None and not (
            isinstance(token_type, str) and token_type.lower() == "bearer"
        ):
            raise MarzbanAuthenticationError(
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
            raise MarzbanTransportError(
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
        *same* request exactly once; a second ``401`` fails closed
        (:class:`MarzbanAuthenticationError` -- ADR-018: a ``401`` is
        always rejected by the pinned ``Admin.get_current`` dependency
        before the route body runs, so it never has an ambiguous side
        effect). No other automatic retry exists anywhere in this
        adapter: a transport-level failure (timeout, connection reset,
        etc., :class:`MarzbanTransportError`) always propagates
        immediately, so a POST/PUT with an ambiguous external result
        (e.g. ``create_user()``'s ``POST /api/user`` failing after the
        request may already have reached the server) is never silently
        repeated.
        """
        if self._token is None:
            self._token = self._authenticate()
        response = self._dispatch(method, path, operation=operation, json_body=json_body)
        if response.status_code == 401:
            self._token = None
            self._token = self._authenticate()
            response = self._dispatch(method, path, operation=operation, json_body=json_body)
            if response.status_code == 401:
                raise MarzbanAuthenticationError(
                    "Marzban rejected the re-authenticated request (401 twice)",
                    operation=operation,
                    status_code=401,
                )
        return response

    # -- AccountingProvider contract -------------------------------------

    def health_check(self) -> bool:
        """Probe the pinned admin authentication contract without user mutation."""
        try:
            self._token = self._authenticate()
        except MarzbanApiError:
            self._token = None
            return False
        return True

    def create_user(
        self, username: str, quota_bytes: int, expire_at: datetime | None
    ) -> AccountUserDTO:
        # ADR-018: classify create_user()'s failure modes by
        # exact-source-proven side-effect certainty instead of a single
        # blanket exception, so ProvisioningService can compensate
        # safely (never blind-disabling a username this call did not
        # actually create). This includes failures that happen while
        # still *constructing* the request -- a naive/pre-epoch
        # expire_at, or any other future local/preflight validation --
        # which by definition happen before anything is ever dispatched
        # to the network, and are therefore always NO_SIDE_EFFECT, never
        # left as an unclassified MarzbanContractError that would fall
        # through to ProvisioningService's conservative "may have been
        # created" default.
        try:
            mapped_expire = _map_expire_to_marzban(expire_at)
        except MarzbanContractError as exc:
            raise AccountingCreateUserError(
                str(exc), effect=AccountingCreateEffect.NO_SIDE_EFFECT
            ) from exc

        body = {
            "username": username,
            "status": "active",
            "data_limit": quota_bytes,
            "data_limit_reset_strategy": "no_reset",
            "expire": mapped_expire,
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
        try:
            response = self._call("POST", "/api/user", operation="create_user", json_body=body)
        except MarzbanAuthenticationError as exc:
            # Pinned `Admin.get_current` (a FastAPI Depends()) always
            # rejects an unauthenticated/re-rejected request before
            # add_user()'s body -- and therefore before
            # crud.create_user()'s db.commit() -- ever runs.
            raise AccountingCreateUserError(
                "Marzban rejected authentication before create_user could "
                "reach the server; no user was created",
                effect=AccountingCreateEffect.NO_SIDE_EFFECT,
            ) from exc
        except MarzbanTransportError as exc:
            # The request was dispatched but its outcome is unknown --
            # never assume either NO_SIDE_EFFECT or CREATED.
            raise AccountingCreateUserError(
                "Marzban create_user request failed at the transport layer "
                "after being dispatched; the server-side outcome cannot be "
                "determined",
                effect=AccountingCreateEffect.AMBIGUOUS,
            ) from exc

        if response.status_code in (400, 409, 422):
            # Exact-source verified (pinned app/routers/user.py::add_user,
            # app/db/crud.py::create_user): a `400` (unsupported protocol)
            # is raised before `crud.create_user()` is ever called; a
            # `409` is raised only after `crud.create_user()`'s
            # `db.commit()` itself raised `IntegrityError` and was rolled
            # back (`db.rollback()`); a `422` is FastAPI's own request
            # -validation response for `add_user(new_user: UserCreate,
            # ...)` -- `new_user` is a plain Pydantic-model body parameter
            # (not wrapped in `Depends()`), so FastAPI's request pipeline
            # validates/parses it (raising `RequestValidationError`,
            # converted to this `422`) before the route function body --
            # and therefore before `crud.create_user()` -- ever runs; this
            # is a framework-level FastAPI guarantee, not Marzban's own
            # logic, exactly like the `401`/`Admin.get_current` case
            # above. All three are proven pre-commit rejections with zero
            # server-side side effect. No reconciliation is implemented
            # for `409` (ADR-018 explicitly reserves that as a separate,
            # unmade idempotency decision).
            raise AccountingCreateUserError(
                f"Marzban rejected create_user before creating any user "
                f"(status {response.status_code}); no side effect occurred",
                effect=AccountingCreateEffect.NO_SIDE_EFFECT,
            )
        if response.status_code != 200:
            # Any other non-200 status (including 5xx): pinned
            # `crud.create_user()` calls `db.commit()` unconditionally as
            # soon as it runs, with no further validation afterwards --
            # `add_user()`'s own post-commit steps (background task
            # scheduling, `UserResponse.model_validate()`, `report.
            # user_created()`) could still fail and surface as a 5xx
            # *after* the user already exists. This status code alone
            # cannot prove which happened, so it is never treated as
            # NO_SIDE_EFFECT.
            raise AccountingCreateUserError(
                f"Marzban create_user returned an unexpected status "
                f"{response.status_code}; the server-side outcome cannot "
                "be determined from this status code alone",
                effect=AccountingCreateEffect.AMBIGUOUS,
            )

        try:
            payload = _json_object(response, operation="create_user")
            status = _require_pinned_status(payload, "status", operation="create_user")
            if status != _CREATE_SUCCESS_STATUS:
                # The request explicitly asked for status="active"; any
                # other pinned status back means the just-created account
                # is not in the state this call requested -- a
                # postcondition failure on an account that does exist.
                raise MarzbanContractError(
                    f"Marzban create_user response status was {status!r}, "
                    f"not {_CREATE_SUCCESS_STATUS!r} as requested",
                    operation="create_user",
                )
            return _parse_account_user(
                payload, expected_username=username, operation="create_user"
            )
        except MarzbanContractError as exc:
            # HTTP 200 proves the user was created; a contract violation
            # in the response body is a postcondition failure on an
            # account that does exist -- ownership is established.
            raise AccountingCreateUserError(
                str(exc), effect=AccountingCreateEffect.CREATED
            ) from exc

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
        status = _require_pinned_status(payload, "status", operation="get_usage")
        return UsageDTO(
            tenant_id=username,
            used_gb=Decimal(used_bytes) / Decimal(1024**3),
            measured_at=datetime.now(UTC),
            used_bytes=used_bytes,
            status=status,
        )
