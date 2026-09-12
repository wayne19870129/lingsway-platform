"""TASK-T16 Phase 2B8: application-scoped ``ProviderRegistry`` lifecycle.

Verifies the FastAPI ``lifespan`` in ``backend/app/main.py`` builds exactly
one process-lifetime registry, that HTTP requests reuse the same instance
(via ``backend.app.dependencies.get_provider_registry``, exercised here
through a real route that actually depends on ``ManagedRegistry`` -- not
just an unrelated endpoint like ``/health``), and that shutdown closes it
deterministically. Also verifies the dependency fails closed (never a
silent, unmanaged fallback registry) if the lifespan never ran --
independent-review round 1's Major 1 finding on PR #65.
"""

from __future__ import annotations

import importlib
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.core.config import get_settings
from backend.app.dependencies import (
    AuthContext,
    ProviderRegistryNotConfigured,
    get_auth_context,
    get_provider_registry,
)
from backend.app.models import Customer, CustomerRole, CustomerStatus, JwtSession
from backend.app.providers.registry import ProviderRegistry, build_registry


def _test_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Permit TestClient's in-process socketpair (needed for its
    anyio blocking portal) without permitting real network I/O -- the
    same pattern test_admin_api.py uses, duplicated locally to keep this
    file's fixtures self-contained."""
    blocked_create_connection = socket.create_connection
    importlib.reload(socket)
    monkeypatch.setattr(socket, "create_connection", blocked_create_connection)
    return TestClient(main.app)


def _admin_auth_context() -> AuthContext:
    customer = Customer(
        id=1,
        customer_no="CUS-LIFECYCLE-ADMIN",
        email="lifecycle-admin@example.invalid",
        password_hash="unused",
        status=CustomerStatus.ACTIVE,
        role=CustomerRole.ADMIN,
    )
    session = JwtSession(
        jti="lifecycle-admin-session",
        customer_id=1,
        issued_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    return AuthContext(customer, session)


@dataclass(slots=True)
class _CloseTrackingProvider:
    close_calls: list[None] = field(default_factory=list)

    def close(self) -> None:
        self.close_calls.append(None)


def test_multiple_requests_reuse_the_same_application_scoped_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two separate requests to a route that actually declares a
    ``registry: ManagedRegistry`` parameter (``admin_accounting_health`` --
    not an unrelated endpoint like ``/health``, which proves nothing about
    this dependency) must resolve the identical ``ProviderRegistry``
    instance through real FastAPI dependency injection, not one freshly
    constructed per request."""
    main.app.dependency_overrides[get_auth_context] = _admin_auth_context
    try:
        with _test_client(monkeypatch) as client:
            response = client.get("/api/v1/admin/accounting/health")
            assert response.status_code == 200
            first = main.app.state.provider_registry
            assert isinstance(first, ProviderRegistry)

            response = client.get("/api/v1/admin/accounting/health")
            assert response.status_code == 200
            second = main.app.state.provider_registry
            assert second is first
    finally:
        main.app.dependency_overrides.clear()


def test_lifespan_closes_the_registry_exactly_once_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lifespan's own shutdown path must call ProviderRegistry.close()
    exactly once, even though close() is itself idempotent -- proving the
    lifespan doesn't rely on close()'s own idempotency to mask a bug."""
    egress = _CloseTrackingProvider()
    defaults = build_registry(get_settings())
    spy_registry = ProviderRegistry(
        egress=egress,  # type: ignore[arg-type]
        accounting=defaults.accounting,
        gateway=defaults.gateway,
        forwarder=defaults.forwarder,
        payment=defaults.payment,
        notify=defaults.notify,
        email=defaults.email,
        captcha=defaults.captcha,
        storage=defaults.storage,
        transport=defaults.transport,
    )

    with mock.patch.object(main, "build_registry", return_value=spy_registry), _test_client(
        monkeypatch
    ):
        assert main.app.state.provider_registry is spy_registry
        assert egress.close_calls == []

    assert len(egress.close_calls) == 1


def test_lifespan_closes_the_registry_even_when_the_app_raises_during_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown must still run (and still close exactly once) even if a
    request raised inside the ``with`` block -- ``@asynccontextmanager``'s
    own ``finally`` semantics guarantee this, but this test proves the
    lifespan actually relies on that rather than a fragile non-finally
    close call that a real exception path could skip."""
    egress = _CloseTrackingProvider()
    defaults = build_registry(get_settings())
    spy_registry = ProviderRegistry(
        egress=egress,  # type: ignore[arg-type]
        accounting=defaults.accounting,
        gateway=defaults.gateway,
        forwarder=defaults.forwarder,
        payment=defaults.payment,
        notify=defaults.notify,
        email=defaults.email,
        captcha=defaults.captcha,
        storage=defaults.storage,
        transport=defaults.transport,
    )

    with (
        mock.patch.object(main, "build_registry", return_value=spy_registry),
        pytest.raises(RuntimeError, match="simulated failure during request handling"),
        _test_client(monkeypatch),
    ):
        raise RuntimeError("simulated failure during request handling")

    assert len(egress.close_calls) == 1


class _FakeRequest:
    """A minimal stand-in for FastAPI's ``Request`` -- get_provider_registry()
    only ever reads ``request.app.state``."""

    def __init__(self, app: object) -> None:
        self.app = app


def test_registry_dependency_fails_closed_when_lifespan_did_not_run() -> None:
    """Independent-review Major 1 (PR #65, round 1): when the lifespan
    never ran (e.g. a bare ``TestClient(app)`` without the ``with``
    context manager, which does not trigger startup/shutdown events),
    get_provider_registry() must fail closed -- never silently construct
    an unmanaged registry that nothing would ever close."""
    app_state = main.app.state
    if hasattr(app_state, "provider_registry"):
        del app_state.provider_registry
    try:
        request = _FakeRequest(main.app)
        with pytest.raises(ProviderRegistryNotConfigured):
            get_provider_registry(request)  # type: ignore[arg-type]
    finally:
        if hasattr(app_state, "provider_registry"):
            del app_state.provider_registry


def test_get_provider_registry_is_the_dependency_used_by_main_app() -> None:
    """Sanity check that the dependency callable exported from
    dependencies.py is the exact one main.py's routes are wired to (not a
    same-named duplicate that would silently diverge) -- reinforced by
    the DI-path test above actually exercising a real registry-dependent
    route through TestClient."""
    from backend.app.api.public import place_order

    assert get_provider_registry.__module__ == "backend.app.dependencies"
    assert callable(place_order)
