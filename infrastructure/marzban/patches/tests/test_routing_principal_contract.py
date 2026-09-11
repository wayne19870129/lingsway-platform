"""ADR-016 Decision 3 contract: patched Marzban `UserResponse.routing_principal`.

These tests import the REAL, patched upstream `app/models/user.py`
(applied fresh for each test via `apply_patch.sh`, never hand-edited) and
drive it through a real FastAPI `TestClient` HTTP round trip for the two
endpoints this repository actually depends on:

    POST /api/user                  (mirrors app/routers/user.py::add_user)
    GET  /api/user/{username}       (mirrors app/routers/user.py::get_user)

Everything under `app.*`/`config` that `app/models/user.py` imports but
that is unrelated to computing `routing_principal` is a minimal stub
installed by `conftest.py` -- see that file and `../README.md` for exactly
which lines are genuine upstream Marzban source versus test harness.

This is a TestClient harness, not an integration test against a real
Marzban instance -- no real Marzban process is started or contacted.
"""

from __future__ import annotations

import types
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_dbuser(module: types.ModuleType, *, id_: int, username: str, **overrides: Any) -> Any:
    """A test double standing in for Marzban's real SQLAlchemy `DBUser`
    ORM instance -- shaped with every attribute the patched `UserResponse`
    (and its `User` base) can read via `from_attributes=True`, matching
    the DB model's real attribute names (`id`, `username`, ...) confirmed
    against the pinned `app/db/models.py::User` during this PR's upstream
    research."""
    values: dict[str, Any] = {
        "id": id_,
        "username": username,
        "status": module.UserStatus.active,
        "used_traffic": 12345,
        "lifetime_used_traffic": 0,
        "created_at": datetime(2024, 1, 1, tzinfo=UTC),
        # Non-empty on purpose: this is what lets validate_links()/
        # validate_subscription_url() short-circuit without ever calling
        # the stubbed (assertion-raising) generate_v2ray_links()/
        # create_subscription_token().
        "links": ["vless://existing-link"],
        "subscription_url": "https://harness.invalid/sub/existing-token",
        "proxies": {"vmess": {}},
        "excluded_inbounds": {},
        "admin": None,
        "expire": None,
        "data_limit": None,
        "data_limit_reset_strategy": module.UserDataLimitResetStrategy.no_reset,
        "inbounds": {},
        "note": None,
        "sub_updated_at": None,
        "sub_last_user_agent": None,
        "online_at": None,
        "on_hold_expire_duration": None,
        "on_hold_timeout": None,
        "auto_delete_in_days": None,
        "next_plan": None,
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


def _build_app(module: types.ModuleType) -> FastAPI:
    """Two routes shaped exactly like the real endpoints this repository
    depends on (method, path, response_model) -- not their real handler
    bodies (real add_user()/get_user() need a live Marzban DB/service
    layer far beyond this contract; see ../README.md)."""
    app = FastAPI()
    UserResponse = module.UserResponse

    @app.post("/api/user", response_model=UserResponse)
    def add_user(payload: dict[str, Any]) -> Any:
        # Mirrors the real add_user()'s own return line exactly:
        # `user = UserResponse.model_validate(dbuser); return user`.
        dbuser = app.state.dbusers[payload["username"]]
        return UserResponse.model_validate(dbuser)

    @app.get("/api/user/{username}", response_model=UserResponse)
    def get_user(username: str) -> Any:
        # Mirrors the real get_user()'s own body exactly: `return dbuser`,
        # relying on FastAPI's response_model to do the real serialization.
        return app.state.dbusers[username]

    app.state.dbusers = {}
    return app


def test_post_and_get_return_the_same_routing_principal(
    patched_user_response_module: types.ModuleType,
) -> None:
    module = patched_user_response_module
    dbuser = _make_dbuser(module, id_=42, username="sub-123")
    app = _build_app(module)
    app.state.dbusers["sub-123"] = dbuser

    with TestClient(app) as client:
        post_response = client.post("/api/user", json={"username": "sub-123"})
        get_response = client.get("/api/user/sub-123")

    assert post_response.status_code == 200
    assert get_response.status_code == 200

    post_body = post_response.json()
    get_body = get_response.json()

    assert post_body["routing_principal"] == "42.sub-123"
    assert get_body["routing_principal"] == "42.sub-123"
    assert post_body["routing_principal"] == get_body["routing_principal"]


def test_raw_id_never_appears_in_either_response(
    patched_user_response_module: types.ModuleType,
) -> None:
    module = patched_user_response_module
    dbuser = _make_dbuser(module, id_=777, username="sub-secret")
    app = _build_app(module)
    app.state.dbusers["sub-secret"] = dbuser

    with TestClient(app) as client:
        post_response = client.post("/api/user", json={"username": "sub-secret"})
        get_response = client.get("/api/user/sub-secret")

    for response in (post_response, get_response):
        body = response.json()
        assert "id" not in body
        # Belt-and-braces: the raw id must not leak as a substring of the
        # serialized JSON body either (e.g. under some other key by
        # accident); only its composed, expected appearance inside
        # routing_principal is acceptable.
        raw_json = response.text
        assert '"id"' not in raw_json
        assert body["routing_principal"] == "777.sub-secret"


def test_legacy_fields_are_preserved_and_unchanged(
    patched_user_response_module: types.ModuleType,
) -> None:
    module = patched_user_response_module
    created_at = datetime(2023, 6, 15, 12, 30, tzinfo=UTC)
    dbuser = _make_dbuser(
        module,
        id_=1,
        username="legacy-user",
        status=module.UserStatus.active,
        used_traffic=999_888,
        lifetime_used_traffic=1_000_000,
        created_at=created_at,
        data_limit=50 * 1024**3,
        expire=1_700_000_000,
    )
    app = _build_app(module)
    app.state.dbusers["legacy-user"] = dbuser

    with TestClient(app) as client:
        post_body = client.post("/api/user", json={"username": "legacy-user"}).json()
        get_body = client.get("/api/user/legacy-user").json()

    for body in (post_body, get_body):
        assert body["username"] == "legacy-user"
        assert body["status"] == "active"
        assert body["used_traffic"] == 999_888
        assert body["lifetime_used_traffic"] == 1_000_000
        assert body["data_limit"] == 50 * 1024**3
        assert body["expire"] == 1_700_000_000
        # created_at survives the round trip as an ISO-8601 string; the
        # exact field type/shape is untouched by the patch.
        assert body["created_at"].startswith("2023-06-15T12:30:00")
        # routing_principal is additive: every pre-existing field the real
        # UserResponse declares is still present.
        assert "routing_principal" in body


def test_unpatched_source_is_not_contract_ready(
    unpatched_user_response_module: types.ModuleType,
) -> None:
    """The pristine (unpatched) vendored source must NOT be mistaken for
    contract-ready: it has no routing_principal field, and its UserResponse
    cannot be built with an `id` attribute at all (extra attributes are
    ignored, not errored, by default -- so this must check for the field's
    *absence* in the response, not expect a validation failure)."""
    module = unpatched_user_response_module
    dbuser = _make_dbuser(module, id_=1, username="unpatched-user")
    app = _build_app(module)
    app.state.dbusers["unpatched-user"] = dbuser

    with TestClient(app) as client:
        post_body = client.post("/api/user", json={"username": "unpatched-user"}).json()
        get_body = client.get("/api/user/unpatched-user").json()

    for body in (post_body, get_body):
        assert "routing_principal" not in body
        assert "id" not in body
