"""Offline HTTP contract tests for ``MarzbanAccountingProvider``
(TASK-T16 Phase 2B7).

Every test drives the adapter through a real ``httpx.Client`` bound to
``httpx.MockTransport`` -- never a direct call into the response parser
-- so these tests actually exercise the HTTP boundary (method, path,
headers, form/JSON body, status handling) the way the real Marzban admin
API would be hit. No real Marzban instance, no real credentials, no
network access.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from backend.app.providers.accounting.marzban import (
    MarzbanAccountingProvider,
    MarzbanApiError,
    MarzbanContractError,
    MarzbanProviderClosedError,
)
from backend.app.providers.base import AccountingCreateEffect, AccountingCreateUserError

_BASE_URL = "https://marzban.internal.invalid"
_ADMIN_USERNAME = "admin"
_ADMIN_PASSWORD = "s3cret-admin-password"
_INBOUNDS_JSON = json.dumps({"vless": ["VLESS TCP REALITY"]})


def _token_response(token: str = "token-1", token_type: str | None = "bearer") -> httpx.Response:
    body: dict[str, object] = {"access_token": token}
    if token_type is not None:
        body["token_type"] = token_type
    return httpx.Response(200, json=body)


def _user_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "username": "alice",
        "status": "active",
        "data_limit": 53687091200,
        "expire": 0,
        "routing_principal": "42.alice",
        "used_traffic": 12345,
        "links": ["vless://uuid@host:443"],
    }
    payload.update(overrides)
    return payload


def _provider(
    handler: Callable[[httpx.Request], httpx.Response],
    **kwargs: Any,
) -> MarzbanAccountingProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    defaults: dict[str, object] = {
        "base_url": _BASE_URL,
        "admin_username": _ADMIN_USERNAME,
        "admin_password": _ADMIN_PASSWORD,
        "default_protocol": "vless",
        "default_inbounds_json": _INBOUNDS_JSON,
        "client": client,
    }
    defaults.update(kwargs)
    return MarzbanAccountingProvider(**defaults)  # type: ignore[arg-type]


def _recording_handler(
    requests: list[httpx.Request],
    *,
    user_response: Callable[[httpx.Request], httpx.Response] | None = None,
    token_response: Callable[[httpx.Request], httpx.Response] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/admin/token":
            return token_response(request) if token_response else _token_response()
        if user_response:
            return user_response(request)
        return httpx.Response(200, json=_user_payload())

    return handler


# ---------------------------------------------------------------------------
# 1-3: auth request shape, bearer token, cache reuse.
# ---------------------------------------------------------------------------


def test_authenticate_sends_oauth2_form_not_json() -> None:
    requests: list[httpx.Request] = []
    provider = _provider(_recording_handler(requests))

    provider.get_usage("alice")

    auth_request = requests[0]
    assert auth_request.method == "POST"
    assert auth_request.url.path == "/api/admin/token"
    content_type = auth_request.headers.get("content-type", "")
    assert content_type.startswith("application/x-www-form-urlencoded")
    body = auth_request.content.decode()
    assert f"username={_ADMIN_USERNAME}" in body
    assert "password=" in body
    with pytest.raises(json.JSONDecodeError):
        json.loads(body)


def test_bearer_token_is_added_to_authenticated_requests() -> None:
    requests: list[httpx.Request] = []
    provider = _provider(_recording_handler(requests))

    provider.get_usage("alice")

    user_request = next(r for r in requests if r.url.path != "/api/admin/token")
    assert user_request.headers["authorization"] == "Bearer token-1"


def test_cached_token_is_reused_across_multiple_calls() -> None:
    requests: list[httpx.Request] = []
    provider = _provider(_recording_handler(requests))

    provider.get_usage("alice")
    provider.get_usage("alice")
    provider.get_usage("alice")

    token_requests = [r for r in requests if r.url.path == "/api/admin/token"]
    assert len(token_requests) == 1


# ---------------------------------------------------------------------------
# 4-7: AccountingProvider health contract.
# ---------------------------------------------------------------------------


def test_health_check_success_uses_only_the_pinned_admin_auth_contract() -> None:
    requests: list[httpx.Request] = []
    provider = _provider(_recording_handler(requests))
    try:
        assert provider.health_check() is True
        assert [request.url.path for request in requests] == ["/api/admin/token"]
        assert "token-1" not in repr(provider)
    finally:
        provider.close()


def test_health_check_authentication_failure_is_false_and_redacts_credentials() -> None:
    requests: list[httpx.Request] = []

    def token_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    provider = _provider(_recording_handler(requests, token_response=token_response))
    try:
        assert provider.health_check() is False
        assert [request.url.path for request in requests] == ["/api/admin/token"]
        assert _ADMIN_PASSWORD not in repr(provider)
    finally:
        provider.close()


def test_health_check_transport_failure_is_false() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ConnectError("offline test transport failure", request=request)

    provider = _provider(handler)
    try:
        assert provider.health_check() is False
        assert [request.url.path for request in requests] == ["/api/admin/token"]
    finally:
        provider.close()


def test_health_check_malformed_auth_response_is_false() -> None:
    requests: list[httpx.Request] = []

    def token_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token_type": "bearer"})

    provider = _provider(_recording_handler(requests, token_response=token_response))
    try:
        assert provider.health_check() is False
        assert [request.url.path for request in requests] == ["/api/admin/token"]
    finally:
        provider.close()


def test_missing_ca_is_no_side_effect_for_create_user(tmp_path: Path) -> None:
    provider = MarzbanAccountingProvider(
        base_url=_BASE_URL,
        admin_username=_ADMIN_USERNAME,
        admin_password=_ADMIN_PASSWORD,
        default_protocol="vless",
        default_inbounds_json=_INBOUNDS_JSON,
        ca_cert_path=str(tmp_path / "missing-internal.crt"),
    )

    with pytest.raises(AccountingCreateUserError) as excinfo:
        provider.create_user("alice", 1000, None)

    assert excinfo.value.effect is AccountingCreateEffect.NO_SIDE_EFFECT
    assert "no user was created" in str(excinfo.value)
    provider.close()


# ---------------------------------------------------------------------------
# 8-9: 401 handling.
# ---------------------------------------------------------------------------


def test_first_401_reauthenticates_and_retries_once() -> None:
    requests: list[httpx.Request] = []
    state = {"tokens_issued": 0, "user_calls": 0}

    def token_response(_request: httpx.Request) -> httpx.Response:
        state["tokens_issued"] += 1
        return _token_response(token=f"token-{state['tokens_issued']}")

    def user_response(request: httpx.Request) -> httpx.Response:
        state["user_calls"] += 1
        if state["user_calls"] == 1:
            return httpx.Response(401)
        assert request.headers["authorization"] == "Bearer token-2"
        return httpx.Response(200, json=_user_payload())

    provider = _provider(
        _recording_handler(requests, user_response=user_response, token_response=token_response)
    )

    usage = provider.get_usage("alice")

    assert usage.used_bytes == 12345
    assert state["tokens_issued"] == 2
    assert state["user_calls"] == 2


def test_second_401_fails_closed() -> None:
    requests: list[httpx.Request] = []

    def user_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    provider = _provider(_recording_handler(requests, user_response=user_response))

    with pytest.raises(MarzbanApiError) as excinfo:
        provider.get_usage("alice")
    assert excinfo.value.status_code == 401

    token_requests = [r for r in requests if r.url.path == "/api/admin/token"]
    user_requests = [r for r in requests if r.url.path != "/api/admin/token"]
    # Exactly one re-authentication, exactly one retry -- never an
    # unbounded loop.
    assert len(token_requests) == 2
    assert len(user_requests) == 2


# ---------------------------------------------------------------------------
# 6-11: create_user() request/response contract.
# ---------------------------------------------------------------------------


def test_create_user_sends_correct_post_body() -> None:
    requests: list[httpx.Request] = []
    provider = _provider(_recording_handler(requests))

    provider.create_user(
        "alice", 53687091200, datetime(2027, 1, 1, tzinfo=UTC)
    )

    create_request = next(r for r in requests if r.url.path == "/api/user")
    assert create_request.method == "POST"
    body = json.loads(create_request.content)
    assert body["username"] == "alice"
    assert body["status"] == "active"
    assert body["data_limit"] == 53687091200
    assert body["data_limit_reset_strategy"] == "no_reset"
    assert body["expire"] == int(datetime(2027, 1, 1, tzinfo=UTC).timestamp())
    assert body["proxies"] == {"vless": {}}
    assert body["inbounds"] == {"vless": ["VLESS TCP REALITY"]}


def test_create_user_none_expire_maps_to_zero() -> None:
    requests: list[httpx.Request] = []
    provider = _provider(_recording_handler(requests))

    provider.create_user("alice", 1000, None)

    create_request = next(r for r in requests if r.url.path == "/api/user")
    body = json.loads(create_request.content)
    assert body["expire"] == 0


def test_create_user_naive_datetime_fails_closed() -> None:
    """ADR-018 round 2 (Major 1): a pre-dispatch request-construction
    failure (never reaching the network) must classify as
    AccountingCreateUserError(effect=NO_SIDE_EFFECT), not a bare
    MarzbanContractError -- otherwise ProvisioningService's generic
    exception branch would treat it as CREATED and blind-disable a
    username this call never even attempted to create."""
    requests: list[httpx.Request] = []
    provider = _provider(_recording_handler(requests))
    with pytest.raises(AccountingCreateUserError, match="timezone-aware") as excinfo:
        provider.create_user("alice", 1000, datetime(2027, 1, 1))  # noqa: DTZ001
    assert excinfo.value.effect is AccountingCreateEffect.NO_SIDE_EFFECT
    assert requests == []


def test_create_user_pre_epoch_datetime_fails_closed() -> None:
    """Major 2 (round 1) + Major 1 (round 2): a timezone-aware datetime
    whose epoch maps to <= 0 must never be silently sent as Marzban's
    "0 = unlimited" sentinel, and -- since this failure happens before
    any request is dispatched -- must classify as NO_SIDE_EFFECT."""
    requests: list[httpx.Request] = []
    provider = _provider(_recording_handler(requests))
    with pytest.raises(AccountingCreateUserError, match="non-positive") as excinfo:
        provider.create_user("alice", 1000, datetime(1969, 1, 1, tzinfo=UTC))
    assert excinfo.value.effect is AccountingCreateEffect.NO_SIDE_EFFECT
    assert requests == []


def test_create_response_maps_to_account_user_dto_with_routing_principal() -> None:
    provider = _provider(_recording_handler([]))

    account_user = provider.create_user("alice", 53687091200, None)

    assert account_user.username == "alice"
    assert account_user.quota_bytes == 53687091200
    assert account_user.routing_principal == "42.alice"
    assert account_user.expire_at is None
    assert account_user.enabled is True


def test_create_response_missing_routing_principal_fails_closed() -> None:
    def user_response(_request: httpx.Request) -> httpx.Response:
        payload = _user_payload()
        del payload["routing_principal"]
        return httpx.Response(200, json=payload)

    provider = _provider(_recording_handler([], user_response=user_response))

    with pytest.raises(AccountingCreateUserError, match="routing_principal") as excinfo:
        provider.create_user("alice", 1000, None)
    # ADR-018: HTTP 200 proves the user was created -- a postcondition
    # failure in the response body is CREATED, never NO_SIDE_EFFECT.
    assert excinfo.value.effect is AccountingCreateEffect.CREATED


def test_create_response_blank_routing_principal_fails_closed() -> None:
    def user_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_user_payload(routing_principal="   "))

    provider = _provider(_recording_handler([], user_response=user_response))

    with pytest.raises(AccountingCreateUserError, match="routing_principal") as excinfo:
        provider.create_user("alice", 1000, None)
    assert excinfo.value.effect is AccountingCreateEffect.CREATED


def test_create_response_username_mismatch_fails_closed() -> None:
    def user_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_user_payload(username="someone-else"))

    provider = _provider(_recording_handler([], user_response=user_response))

    with pytest.raises(AccountingCreateUserError, match="username") as excinfo:
        provider.create_user("alice", 1000, None)
    assert excinfo.value.effect is AccountingCreateEffect.CREATED


@pytest.mark.parametrize(
    "overrides",
    [
        {"data_limit": "not-a-number"},
        {"expire": "not-a-number"},
        {"expire": True},
        {"status": ""},
        {"status": 123},
        {"status": "unknown-status"},
        {"expire": -123},
    ],
)
def test_create_response_malformed_fields_fail_closed(overrides: dict[str, object]) -> None:
    def user_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_user_payload(**overrides))

    provider = _provider(_recording_handler([], user_response=user_response))

    with pytest.raises(AccountingCreateUserError) as excinfo:
        provider.create_user("alice", 1000, None)
    assert excinfo.value.effect is AccountingCreateEffect.CREATED


@pytest.mark.parametrize("status", ["disabled", "limited", "expired", "on_hold"])
def test_create_response_non_active_status_fails_closed_as_created(status: str) -> None:
    """Major 2: create_user() explicitly requests status="active" -- any
    other pinned status back means the just-created account is not in
    the requested usable state. This must never be treated as a plain
    success that continues into APPLY_GATEWAY."""

    def user_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_user_payload(status=status))

    provider = _provider(_recording_handler([], user_response=user_response))

    with pytest.raises(AccountingCreateUserError, match="active") as excinfo:
        provider.create_user("alice", 1000, None)
    assert excinfo.value.effect is AccountingCreateEffect.CREATED


# ---------------------------------------------------------------------------
# 12-13: 409 policy, no-retry-on-ambiguous-transport-failure policy.
# ---------------------------------------------------------------------------


def test_create_user_409_fails_closed_without_reconciliation() -> None:
    requests: list[httpx.Request] = []

    def user_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "User already exists"})

    provider = _provider(_recording_handler(requests, user_response=user_response))

    with pytest.raises(AccountingCreateUserError) as excinfo:
        provider.create_user("alice", 1000, None)
    # ADR-018/Major-1: exact-source proven pre-commit rejection (409 is
    # only raised after crud.create_user()'s IntegrityError -> rollback)
    # -- no user was ever created, so this is NO_SIDE_EFFECT, never
    # something that would justify disabling any username.
    assert excinfo.value.effect is AccountingCreateEffect.NO_SIDE_EFFECT

    # No GET-existing-user reconciliation call was made.
    user_calls = [r for r in requests if r.url.path == "/api/user" or "/api/user/" in r.url.path]
    assert len(user_calls) == 1


def test_create_user_400_fails_closed_as_no_side_effect() -> None:
    """Exact-source (app/routers/user.py::add_user): the unsupported-
    protocol 400 is raised before crud.create_user() is ever called."""
    requests: list[httpx.Request] = []

    def user_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "Protocol vless is disabled"})

    provider = _provider(_recording_handler(requests, user_response=user_response))

    with pytest.raises(AccountingCreateUserError) as excinfo:
        provider.create_user("alice", 1000, None)
    assert excinfo.value.effect is AccountingCreateEffect.NO_SIDE_EFFECT


def test_create_user_422_fails_closed_as_no_side_effect() -> None:
    """ADR-018 round 4: exact-source verified (pinned app/routers/
    user.py::add_user -- `new_user: UserCreate` is a plain Pydantic-model
    body parameter, not wrapped in Depends()) -- FastAPI's own request-
    validation pipeline raises RequestValidationError, converted to this
    422, before the route function body (and therefore before
    crud.create_user()'s db.commit()) ever runs. Must never be treated as
    AMBIGUOUS, never retried, never followed by a reconciliation GET, and
    must never trigger a PUT disable or DELETE."""
    requests: list[httpx.Request] = []

    def user_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"detail": [{"loc": ["body", "username"], "msg": "field required"}]},
        )

    provider = _provider(_recording_handler(requests, user_response=user_response))

    with pytest.raises(AccountingCreateUserError) as excinfo:
        provider.create_user("alice", 1000, None)
    assert excinfo.value.effect is AccountingCreateEffect.NO_SIDE_EFFECT

    user_calls = [r for r in requests if r.url.path == "/api/user" or "/api/user/" in r.url.path]
    assert len(user_calls) == 1
    assert all(r.method != "PUT" for r in requests)
    assert all(r.method != "DELETE" for r in requests)
    assert all(r.method != "GET" for r in requests)


def test_create_user_5xx_fails_closed_as_ambiguous_not_no_side_effect() -> None:
    """Major 1: crud.create_user() commits unconditionally as soon as it
    runs; a 5xx could still occur after that commit (background task
    scheduling, response serialization, audit reporting). A 5xx must
    never be assumed NO_SIDE_EFFECT."""
    requests: list[httpx.Request] = []

    def user_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    provider = _provider(_recording_handler(requests, user_response=user_response))

    with pytest.raises(AccountingCreateUserError) as excinfo:
        provider.create_user("alice", 1000, None)
    assert excinfo.value.effect is AccountingCreateEffect.AMBIGUOUS


def test_transport_error_after_create_post_is_not_retried() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/admin/token":
            return _token_response()
        raise httpx.ConnectTimeout("simulated ambiguous transport failure", request=request)

    provider = _provider(handler)

    with pytest.raises(AccountingCreateUserError, match="transport layer") as excinfo:
        provider.create_user("alice", 1000, None)
    assert excinfo.value.effect is AccountingCreateEffect.AMBIGUOUS

    post_attempts = [r for r in requests if r.url.path == "/api/user"]
    assert len(post_attempts) == 1


def test_authentication_failure_before_create_dispatch_is_no_side_effect() -> None:
    """A clean auth rejection (never even reaching the create endpoint
    with valid credentials) is always NO_SIDE_EFFECT -- never AMBIGUOUS,
    since the pinned Admin.get_current dependency always runs before
    add_user()'s body."""

    def token_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    provider = _provider(_recording_handler([], token_response=token_response))

    with pytest.raises(AccountingCreateUserError) as excinfo:
        provider.create_user("alice", 1000, None)
    assert excinfo.value.effect is AccountingCreateEffect.NO_SIDE_EFFECT


# ---------------------------------------------------------------------------
# 14-16: disable_user/set_quota/set_expire bodies.
# ---------------------------------------------------------------------------


def test_disable_user_puts_disabled_status_and_never_deletes() -> None:
    requests: list[httpx.Request] = []
    provider = _provider(_recording_handler(requests))

    provider.disable_user("alice")

    request = next(r for r in requests if r.url.path != "/api/admin/token")
    assert request.method == "PUT"
    assert request.url.path == "/api/user/alice"
    assert json.loads(request.content) == {"status": "disabled"}
    assert all(r.method != "DELETE" for r in requests)


def test_set_quota_body_is_exact() -> None:
    requests: list[httpx.Request] = []
    provider = _provider(_recording_handler(requests))

    provider.set_quota("alice", 999)

    request = next(r for r in requests if r.url.path != "/api/admin/token")
    assert request.method == "PUT"
    assert json.loads(request.content) == {"data_limit": 999}


def test_set_expire_body_is_exact() -> None:
    requests: list[httpx.Request] = []
    provider = _provider(_recording_handler(requests))

    provider.set_expire("alice", datetime(2028, 6, 1, tzinfo=UTC))

    request = next(r for r in requests if r.url.path != "/api/admin/token")
    assert request.method == "PUT"
    assert json.loads(request.content) == {
        "expire": int(datetime(2028, 6, 1, tzinfo=UTC).timestamp())
    }


def test_username_is_url_encoded_in_path() -> None:
    requests: list[httpx.Request] = []
    provider = _provider(_recording_handler(requests))

    provider.disable_user("weird user/name")

    request = next(r for r in requests if r.url.path != "/api/admin/token")
    assert "weird%20user%2Fname" in str(request.url)


# ---------------------------------------------------------------------------
# 17-18: get_connection_links / get_usage.
# ---------------------------------------------------------------------------


def test_get_connection_links_maps_and_validates() -> None:
    provider = _provider(_recording_handler([]))
    links = provider.get_connection_links("alice")
    assert links == ["vless://uuid@host:443"]


def test_get_routing_principal_reads_the_patched_field_from_marzban() -> None:
    requests: list[httpx.Request] = []
    provider = _provider(_recording_handler(requests))

    assert provider.get_routing_principal("alice") == "42.alice"
    user_request = next(request for request in requests if request.url.path != "/api/admin/token")
    assert user_request.method == "GET"
    assert user_request.url.path == "/api/user/alice"


@pytest.mark.parametrize(
    "payload",
    [
        {"username": "alice", "status": "active", "routing_principal": ""},
        {"username": "someone-else", "status": "active", "routing_principal": "42.alice"},
    ],
)
def test_get_routing_principal_fails_closed_on_invalid_identity(payload: dict[str, object]) -> None:
    def user_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    provider = _provider(_recording_handler([], user_response=user_response))

    with pytest.raises(MarzbanContractError, match="routing_principal|username"):
        provider.get_routing_principal("alice")


def test_get_connection_links_malformed_fails_closed() -> None:
    def user_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_user_payload(links="not-a-list"))

    provider = _provider(_recording_handler([], user_response=user_response))
    with pytest.raises(MarzbanContractError, match="links"):
        provider.get_connection_links("alice")


def test_get_connection_links_malformed_item_fails_closed() -> None:
    def user_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_user_payload(links=["ok", 123]))

    provider = _provider(_recording_handler([], user_response=user_response))
    with pytest.raises(MarzbanContractError, match="links"):
        provider.get_connection_links("alice")


def test_get_usage_maps_correctly() -> None:
    provider = _provider(_recording_handler([]))
    usage = provider.get_usage("alice")
    assert usage.tenant_id == "alice"
    assert usage.used_bytes == 12345
    assert usage.used_gb == Decimal(12345) / Decimal(1024**3)
    assert usage.status == "active"


def test_get_usage_malformed_used_traffic_fails_closed() -> None:
    def user_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_user_payload(used_traffic=-1))

    provider = _provider(_recording_handler([], user_response=user_response))
    with pytest.raises(MarzbanContractError, match="used_traffic"):
        provider.get_usage("alice")


def test_get_usage_uses_real_upstream_status_string() -> None:
    def user_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_user_payload(status="limited"))

    provider = _provider(_recording_handler([], user_response=user_response))
    usage = provider.get_usage("alice")
    assert usage.status == "limited"


def test_get_usage_unknown_status_fails_closed() -> None:
    def user_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_user_payload(status="not-a-real-status"))

    provider = _provider(_recording_handler([], user_response=user_response))
    with pytest.raises(MarzbanContractError, match="UserStatus"):
        provider.get_usage("alice")


def test_get_usage_negative_expire_response_fails_closed() -> None:
    """get_usage() itself does not read 'expire', but the shared
    _expire_from_marzban() helper (also used by create_user()'s response
    parsing) must fail closed on a negative value rather than treating it
    as unlimited -- covered directly since get_connection_links()/
    get_usage() never call _parse_account_user()."""
    from backend.app.providers.accounting.marzban import _expire_from_marzban

    with pytest.raises(MarzbanContractError, match="negative"):
        _expire_from_marzban(-1, operation="test")
    assert _expire_from_marzban(0, operation="test") is None


# ---------------------------------------------------------------------------
# 19: invalid default_inbounds_json fails at construction.
# ---------------------------------------------------------------------------


def test_invalid_default_inbounds_json_fails_at_construction() -> None:
    with pytest.raises(MarzbanContractError, match="JSON"):
        _provider(_recording_handler([]), default_inbounds_json="not-json")


def test_default_inbounds_json_not_an_object_fails_at_construction() -> None:
    with pytest.raises(MarzbanContractError):
        _provider(_recording_handler([]), default_inbounds_json="[1, 2, 3]")


def test_default_inbounds_missing_protocol_fails_at_construction() -> None:
    with pytest.raises(MarzbanContractError):
        _provider(
            _recording_handler([]),
            default_protocol="vless",
            default_inbounds_json=json.dumps({"vmess": ["VMess TCP"]}),
        )


def test_default_inbounds_blank_tag_fails_at_construction() -> None:
    with pytest.raises(MarzbanContractError):
        _provider(
            _recording_handler([]),
            default_inbounds_json=json.dumps({"vless": ["   "]}),
        )


def test_default_inbounds_non_list_tags_fails_at_construction() -> None:
    with pytest.raises(MarzbanContractError):
        _provider(
            _recording_handler([]),
            default_inbounds_json=json.dumps({"vless": "VLESS TCP REALITY"}),
        )


def test_unsupported_protocol_fails_at_construction() -> None:
    with pytest.raises(MarzbanContractError, match="not one of the Marzban-supported"):
        _provider(
            _recording_handler([]),
            default_protocol="wireguard",
            default_inbounds_json=json.dumps({"wireguard": ["tag"]}),
        )


# ---------------------------------------------------------------------------
# 20: secret redaction.
# ---------------------------------------------------------------------------


def test_repr_never_exposes_password_or_token() -> None:
    provider = _provider(_recording_handler([]))
    provider.get_usage("alice")  # populates the cached token

    representation = repr(provider)

    assert _ADMIN_PASSWORD not in representation
    assert "token-1" not in representation


def test_exceptions_never_expose_password_or_token() -> None:
    def user_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    provider = _provider(_recording_handler([], user_response=user_response))

    with pytest.raises(MarzbanApiError) as excinfo:
        provider.get_usage("alice")

    message = str(excinfo.value)
    assert _ADMIN_PASSWORD not in message
    assert "token-1" not in message


def test_exceptions_never_expose_connection_links() -> None:
    def user_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_user_payload(links="not-a-list"))

    provider = _provider(_recording_handler([], user_response=user_response))

    with pytest.raises(MarzbanContractError) as excinfo:
        provider.get_connection_links("alice")

    assert "vless://" not in str(excinfo.value)


def test_401_error_message_never_exposes_bearer_token() -> None:
    def user_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    provider = _provider(_recording_handler([], user_response=user_response))

    with pytest.raises(MarzbanApiError) as excinfo:
        provider.get_usage("alice")

    assert "token-1" not in str(excinfo.value)
    assert "token-2" not in str(excinfo.value)



def test_owned_client_loads_explicit_ca_only_on_first_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, float]] = []

    class _OwnedClient:
        def __init__(self, *, verify: object, timeout: float) -> None:
            calls.append((verify, timeout))

        def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
            return _token_response()

        def close(self) -> None:
            return None

    sentinel_context = object()

    def build_tls(*, verify_tls: bool, ca_cert_path: str) -> object:
        calls.append((ca_cert_path, float(verify_tls)))
        return sentinel_context

    monkeypatch.setattr(
        "backend.app.providers.accounting.marzban.build_marzban_tls_verify",
        build_tls,
    )
    monkeypatch.setattr(
        "backend.app.providers.accounting.marzban.httpx.Client",
        _OwnedClient,
    )
    provider = MarzbanAccountingProvider(
        base_url=_BASE_URL,
        admin_username=_ADMIN_USERNAME,
        admin_password=_ADMIN_PASSWORD,
        default_protocol="vless",
        default_inbounds_json=_INBOUNDS_JSON,
        ca_cert_path="/explicit/internal.crt",
        timeout_seconds=7.0,
    )

    assert calls == []
    assert provider.health_check() is True

    assert calls == [
        ("/explicit/internal.crt", 1.0),
        (sentinel_context, 7.0),
    ]
    provider.close()

# ---------------------------------------------------------------------------
# TASK-T16 Phase 2B8: provider resource lifecycle (_owns_client / close()).
# ---------------------------------------------------------------------------


def test_close_before_lazy_client_creation_is_safe() -> None:
    """No client or CA file is touched by construction or early close."""
    provider = MarzbanAccountingProvider(
        base_url=_BASE_URL,
        admin_username=_ADMIN_USERNAME,
        admin_password=_ADMIN_PASSWORD,
        default_protocol="vless",
        default_inbounds_json=_INBOUNDS_JSON,
    )
    assert provider._owns_client is True  # noqa: SLF001
    assert provider._client is None  # noqa: SLF001

    provider.close()
    provider.close()

    assert provider._client is None  # noqa: SLF001
    with pytest.raises(MarzbanProviderClosedError, match="closed"):
        provider._client_for_operation()  # noqa: SLF001
    assert provider._client is None  # noqa: SLF001


def test_close_never_closes_an_injected_client() -> None:
    """An externally injected client is the injecting caller's own
    resource -- close() must never close it out from under them."""
    injected_client = httpx.Client(transport=httpx.MockTransport(_recording_handler([])))
    provider = _provider(_recording_handler([]), client=injected_client)
    assert provider._owns_client is False  # noqa: SLF001

    provider.close()

    assert injected_client.is_closed is False
    injected_client.close()


def test_close_is_idempotent_and_safe_to_call_multiple_times() -> None:
    provider = MarzbanAccountingProvider(
        base_url=_BASE_URL,
        admin_username=_ADMIN_USERNAME,
        admin_password=_ADMIN_PASSWORD,
        default_protocol="vless",
        default_inbounds_json=_INBOUNDS_JSON,
    )

    provider.close()
    provider.close()
    provider.close()


class _FlakyTransport(httpx.BaseTransport):
    """A real ``httpx.BaseTransport`` whose ``close()`` raises for the
    first ``fail_times`` calls, then succeeds -- drives a real
    ``httpx.Client`` through its actual close order rather than a
    monkeypatched stand-in (see the identical helper and rationale in
    ``test_transport_provider.py``)."""

    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.close_calls = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"access_token": "token-1"})

    def close(self) -> None:
        self.close_calls += 1
        if self.close_calls <= self.fail_times:
            raise RuntimeError("simulated transport close failure")


def test_close_permanently_surfaces_failure_when_the_owned_transport_close_raises() -> None:
    """Independent review (PR #65, round 4): httpx 0.28.1's
    ``Client.close()`` sets its internal state to CLOSED before calling
    the transport's own ``close()``, so once a real close attempt has
    raised, the client is already marked closed and a later
    ``Client.close()`` call would silently no-op instead of retrying.
    ``MarzbanAccountingProvider.close()`` also keeps a permanent closed
    state after the first close attempt, while preserving the provider's
    existing permanent-failure behavior."""
    transport = _FlakyTransport(fail_times=1)
    provider = MarzbanAccountingProvider(
        base_url=_BASE_URL,
        admin_username=_ADMIN_USERNAME,
        admin_password=_ADMIN_PASSWORD,
        default_protocol="vless",
        default_inbounds_json=_INBOUNDS_JSON,
    )
    assert provider._owns_client is True  # noqa: SLF001
    # Swap in a real httpx.Client bound to the flaky transport while
    # keeping _owns_client True (the constructor has no way to inject a
    # custom transport for a self-owned client) so this exercises "the
    # client this provider owns happens to fail to close", not the
    # (owns_client=False) injection path.
    provider._client = httpx.Client(transport=transport)  # noqa: SLF001

    with pytest.raises(RuntimeError, match="simulated transport close failure"):
        provider.close()
    assert transport.close_calls == 1

    with pytest.raises(RuntimeError, match="previously failed to close"):
        provider.close()
    assert transport.close_calls == 1  # never actually retried -- httpx would just no-op

    assert provider._client.is_closed is True  # noqa: SLF001
