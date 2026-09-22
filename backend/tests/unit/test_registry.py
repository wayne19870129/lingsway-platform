import socket
from dataclasses import dataclass, field

import pytest

from backend.app.core.config import Settings
from backend.app.providers.accounting.marzban import (
    MarzbanAccountingProvider,
    MarzbanContractError,
)
from backend.app.providers.accounting.mock import MockAccountingProvider
from backend.app.providers.captcha.noop import NoopCaptchaProvider
from backend.app.providers.egress.mock import MockEgressProvider
from backend.app.providers.email.noop import NoopEmailProvider
from backend.app.providers.forwarder.mock import MockForwarderProvider
from backend.app.providers.gateway.mock import MockGatewayProvider
from backend.app.providers.gateway.xray_composition import XrayCompositionError
from backend.app.providers.gateway.xray_file import MarzbanXrayRuntime, XrayFileProvider
from backend.app.providers.notify.noop import NoopNotifyProvider
from backend.app.providers.payment.mock import MockPaymentProvider
from backend.app.providers.registry import (
    ProviderConfigurationError,
    ProviderRegistry,
    build_registry,
)
from backend.app.providers.storage.mock import MockBlobStorage
from backend.app.providers.transport.mock import MockTransportProvider


def test_all_mock_registry_assembles_without_network_or_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("unit registry attempted network access")

    monkeypatch.setattr(socket, "socket", reject_network)
    settings = Settings.from_env(
        {
            "EGRESS_PROVIDER": "mock",
            "ACCOUNTING_PROVIDER": "mock",
            "GATEWAY_PROVIDER": "mock",
            "FORWARDER_PROVIDER": "mock",
            "PAYMENT_PROVIDER": "mock",
            "NOTIFY_PROVIDER": "noop",
            "EMAIL_PROVIDER": "noop",
            "CAPTCHA_PROVIDER": "noop",
            "STORAGE_PROVIDER": "mock",
            "TRANSPORT_PROVIDER_MODE": "mock",
        }
    )

    registry = build_registry(settings)

    assert isinstance(registry.egress, MockEgressProvider)
    assert isinstance(registry.accounting, MockAccountingProvider)
    assert isinstance(registry.gateway, MockGatewayProvider)
    assert isinstance(registry.forwarder, MockForwarderProvider)
    assert isinstance(registry.payment, MockPaymentProvider)
    assert isinstance(registry.notify, NoopNotifyProvider)
    assert isinstance(registry.email, NoopEmailProvider)
    assert isinstance(registry.captcha, NoopCaptchaProvider)
    assert isinstance(registry.storage, MockBlobStorage)
    assert isinstance(registry.transport, MockTransportProvider)


def test_registry_rejects_unimplemented_selection() -> None:
    with pytest.raises(ProviderConfigurationError, match="EGRESS_PROVIDER"):
        build_registry(Settings(egress_provider="webshare"))


def test_registry_selects_subscription_mode_without_constructing_provider() -> None:
    registry = build_registry(
        Settings(transport_provider_mode="subscription", transport_cache_root="cache")
    )

    assert registry.transport is None
    assert registry.transport_resolver is not None
    registry.close()


def test_registry_rejects_blank_subscription_cache_root() -> None:
    with pytest.raises(ValueError, match="TRANSPORT_CACHE_ROOT"):
        build_registry(Settings(transport_provider_mode="subscription", transport_cache_root=" "))


def test_mihomo_runtime_safety_requires_external_controller() -> None:
    with pytest.raises(ValueError, match="MIHOMO_EXTERNAL_CONTROLLER"):
        Settings(
            forwarder_provider="mihomo",
            mihomo_external_controller=" ",
        ).validate_runtime_safety()


def test_mihomo_runtime_safety_accepts_nonblank_external_controller() -> None:
    Settings(
        forwarder_provider="mihomo",
        mihomo_external_controller="203.0.113.10:9090",
    ).validate_runtime_safety()


def test_build_registry_still_rejects_mihomo_selection() -> None:
    with pytest.raises(ProviderConfigurationError, match="FORWARDER_PROVIDER"):
        build_registry(
            Settings(
                forwarder_provider="mihomo",
                mihomo_external_controller="203.0.113.10:9090",
            )
        )


# ---------------------------------------------------------------------------
# TASK-T16 Phase 2B8: ProviderRegistry ownership/lifecycle.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ClosableProvider:
    """A minimal stand-in for a future resource-owning provider (e.g. one
    holding an ``httpx.Client``): only tracks how many times ``close()``
    was called, so tests can assert exactly-once semantics without
    depending on any real provider's internals."""

    close_calls: list[None] = field(default_factory=list)

    def close(self) -> None:
        self.close_calls.append(None)


def _all_mock_registry() -> ProviderRegistry:
    return build_registry(Settings())


def test_all_mock_registry_close_is_a_safe_noop() -> None:
    """None of build_registry()'s mock/noop providers own a closeable
    resource -- close() must not raise just because most providers here
    have no close() method at all."""
    registry = _all_mock_registry()
    registry.close()
    registry.close()  # idempotent -- second call is also a safe no-op.


def test_registry_close_calls_each_owned_providers_close_exactly_once() -> None:
    egress = _ClosableProvider()
    accounting = _ClosableProvider()
    defaults = _all_mock_registry()
    registry = ProviderRegistry(
        egress=egress,  # type: ignore[arg-type]
        accounting=accounting,  # type: ignore[arg-type]
        gateway=defaults.gateway,
        forwarder=defaults.forwarder,
        payment=defaults.payment,
        notify=defaults.notify,
        email=defaults.email,
        captcha=defaults.captcha,
        storage=defaults.storage,
        transport=defaults.transport,
    )

    registry.close()

    assert len(egress.close_calls) == 1
    assert len(accounting.close_calls) == 1


def test_registry_close_is_idempotent_never_double_closes() -> None:
    egress = _ClosableProvider()
    defaults = _all_mock_registry()
    registry = ProviderRegistry(
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

    registry.close()
    registry.close()
    registry.close()

    assert len(egress.close_calls) == 1


def test_registry_context_manager_closes_on_exit() -> None:
    egress = _ClosableProvider()
    defaults = _all_mock_registry()
    registry = ProviderRegistry(
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

    with registry:
        assert egress.close_calls == []
    assert len(egress.close_calls) == 1


@dataclass(slots=True)
class _FlakyCloseProvider:
    """Raises on close() until ``fail_times`` calls have been consumed,
    then succeeds -- lets tests drive a provider through a failed close
    followed by a successful retry."""

    fail_times: int = 0
    close_calls: list[None] = field(default_factory=list)

    def close(self) -> None:
        self.close_calls.append(None)
        if len(self.close_calls) <= self.fail_times:
            raise RuntimeError("simulated close() failure")


def test_registry_close_still_closes_every_other_provider_when_one_raises() -> None:
    """Independent-review Major 2 (PR #65, round 1): one provider's
    close() raising must never skip the remaining providers -- the
    original implementation set `_closed = True` before iterating, so a
    failure on `egress` (iterated first) silently left `accounting` and
    every later provider permanently unclosed."""
    failing_egress = _FlakyCloseProvider(fail_times=999)  # always fails
    accounting = _ClosableProvider()
    transport = _ClosableProvider()
    defaults = _all_mock_registry()
    registry = ProviderRegistry(
        egress=failing_egress,  # type: ignore[arg-type]
        accounting=accounting,  # type: ignore[arg-type]
        gateway=defaults.gateway,
        forwarder=defaults.forwarder,
        payment=defaults.payment,
        notify=defaults.notify,
        email=defaults.email,
        captcha=defaults.captcha,
        storage=defaults.storage,
        transport=transport,  # type: ignore[arg-type]
    )

    with pytest.raises(ExceptionGroup):
        registry.close()

    assert len(failing_egress.close_calls) == 1
    assert len(accounting.close_calls) == 1
    assert len(transport.close_calls) == 1


def test_registry_close_retries_only_the_provider_that_previously_failed() -> None:
    """A second close() call after a partial failure must retry only the
    provider(s) that failed or were never attempted -- never re-close a
    provider that already succeeded."""
    flaky_egress = _FlakyCloseProvider(fail_times=1)  # fails once, then succeeds
    accounting = _ClosableProvider()
    defaults = _all_mock_registry()
    registry = ProviderRegistry(
        egress=flaky_egress,  # type: ignore[arg-type]
        accounting=accounting,  # type: ignore[arg-type]
        gateway=defaults.gateway,
        forwarder=defaults.forwarder,
        payment=defaults.payment,
        notify=defaults.notify,
        email=defaults.email,
        captcha=defaults.captcha,
        storage=defaults.storage,
        transport=defaults.transport,
    )

    with pytest.raises(ExceptionGroup):
        registry.close()
    assert len(flaky_egress.close_calls) == 1
    assert len(accounting.close_calls) == 1  # already succeeded on the first sweep

    registry.close()  # retry: only egress needed a second attempt

    assert len(flaky_egress.close_calls) == 2
    assert len(accounting.close_calls) == 1  # never re-closed


def test_registry_close_closes_a_shared_provider_object_only_once() -> None:
    """If the identical provider object fills more than one registry
    role, close() must call its close() exactly once, not once per role."""
    shared = _ClosableProvider()
    defaults = _all_mock_registry()
    registry = ProviderRegistry(
        egress=shared,  # type: ignore[arg-type]
        accounting=shared,  # type: ignore[arg-type]
        gateway=defaults.gateway,
        forwarder=defaults.forwarder,
        payment=defaults.payment,
        notify=defaults.notify,
        email=defaults.email,
        captcha=defaults.captcha,
        storage=defaults.storage,
        transport=defaults.transport,
    )

    registry.close()

    assert len(shared.close_calls) == 1


class _NoIoClient:
    def __init__(self) -> None:
        self.request_calls = 0
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1

    def request(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.request_calls += 1
        raise AssertionError("registry construction attempted HTTP")


def _marzban_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "accounting_provider": "marzban",
        "marzban_base_url": "https://marzban.example.invalid",
        "marzban_admin_username": "admin",
        "marzban_admin_password": "test-password",
        "marzban_default_protocol": "vless",
        "marzban_default_inbounds_json": '{"vless": ["inbound-vless"]}',
        "marzban_verify_tls": True,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_registry_selects_marzban_from_settings_without_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _NoIoClient()
    monkeypatch.setattr(
        "backend.app.providers.accounting.marzban.httpx.Client",
        lambda **_kwargs: client,
    )

    registry = build_registry(_marzban_settings())

    assert isinstance(registry.accounting, MarzbanAccountingProvider)
    assert client.request_calls == 0
    assert registry.accounting._base_url == "https://marzban.example.invalid"
    assert registry.accounting._admin_username == "admin"
    assert registry.accounting._client is None  # noqa: SLF001

    registry.close()
    registry.close()
    assert client.close_calls == 0



def _xray_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "gateway_provider": "xray_file",
        "marzban_base_url": "https://marzban.internal.invalid",
        "marzban_admin_username": "admin",
        "marzban_admin_password": "test-password",
        "marzban_default_protocol": "vless",
        "marzban_default_inbounds_json": '{"vless": ["inbound-vless"]}',
        "xray_reality_dest": "example.com:443",
        "xray_reality_server_name": "example.com",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_registry_selects_xray_file_without_external_io() -> None:
    registry = build_registry(_xray_settings())
    try:
        assert isinstance(registry.gateway, XrayFileProvider)
        assert isinstance(registry.gateway._runtime, MarzbanXrayRuntime)  # noqa: SLF001
        runtime = registry.gateway._runtime  # noqa: SLF001
        assert runtime.control_base_url == "https://marzban.internal.invalid"
        assert runtime.health_host == "marzban"
        assert runtime.health_port == 8443
    finally:
        registry.close()


def test_xray_registry_does_not_load_ca_or_touch_runtime_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "backend.app.providers.marzban_tls.ssl.create_default_context",
        lambda **_kwargs: pytest.fail("CA file must load only during an operation"),
    )
    monkeypatch.setattr(
        "backend.app.providers.gateway.xray_file.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("runtime command must not run"),
    )
    build_registry(_xray_settings())


@pytest.mark.parametrize(
    "overrides",
    [
        {"marzban_ca_cert_path": ""},
        {"marzban_verify_tls": False, "app_env": "production"},
        {"marzban_admin_password": "CHANGE_ME", "app_env": "production"},
    ],
)
def test_xray_runtime_safety_requirements_fail_closed(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        settings = _xray_settings(
            app_env=overrides.pop("app_env", "development"), **overrides
        )
        settings.validate_runtime_safety()

def test_unsupported_accounting_provider_fails_closed() -> None:
    with pytest.raises(ProviderConfigurationError, match="ACCOUNTING_PROVIDER"):
        build_registry(Settings(accounting_provider="unsupported"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"marzban_base_url": " "},
        {"marzban_admin_username": " "},
        {"marzban_admin_password": ""},
    ],
)
def test_blank_marzban_settings_fail_closed_before_client_creation(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, object]
) -> None:
    monkeypatch.setattr(
        "backend.app.providers.accounting.marzban.httpx.Client",
        lambda **_kwargs: pytest.fail("client must not be created for invalid settings"),
    )

    with pytest.raises(ValueError):
        build_registry(_marzban_settings(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"marzban_default_protocol": "invalid"},
        {"marzban_default_inbounds_json": "{}"},
    ],
)
def test_malformed_marzban_contract_fails_closed_before_client_creation(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, object]
) -> None:
    monkeypatch.setattr(
        "backend.app.providers.accounting.marzban.httpx.Client",
        lambda **_kwargs: pytest.fail("client must not be created for invalid contract"),
    )

    with pytest.raises(MarzbanContractError):
        build_registry(_marzban_settings(**overrides))


def test_production_marzban_requires_verified_https_before_construction() -> None:
    with pytest.raises(ValueError, match="verified HTTPS"):
        build_registry(
            _marzban_settings(
                app_env="production",
                jwt_secret="J" * 32,
                secret_encryption_key="S" * 44,
                marzban_base_url="http://marzban:8000",
            )
        )


def test_production_default_marzban_credential_fails_closed() -> None:
    with pytest.raises(ValueError, match="credentials"):
        Settings.from_env(
            {
                "APP_ENV": "production",
                "ACCOUNTING_PROVIDER": "marzban",
                "JWT_SECRET": "J" * 32,
                "SECRET_ENCRYPTION_KEY": "S" * 44,
                "MARZBAN_BASE_URL": "https://marzban.test",
                "MARZBAN_ADMIN_USERNAME": "admin",
                "MARZBAN_ADMIN_PASSWORD": "CHANGE_ME",
                "MARZBAN_DEFAULT_PROTOCOL": "vless",
                "MARZBAN_DEFAULT_INBOUNDS_JSON": '{"vless": ["inbound-vless"]}',
            }
        )

def _production_xray_env(**overrides: str) -> dict[str, str]:
    values = {
        "APP_ENV": "production",
        "JWT_SECRET": "J" * 32,
        "SECRET_ENCRYPTION_KEY": "S" * 44,
        "ACCOUNTING_PROVIDER": "mock",
        "GATEWAY_PROVIDER": "xray_file",
        "MARZBAN_BASE_URL": "https://marzban:8000",
        "MARZBAN_ADMIN_USERNAME": "production-admin",
        "MARZBAN_ADMIN_PASSWORD": "production-password",
        "MARZBAN_VERIFY_TLS": "true",
        "MARZBAN_CA_CERT_PATH": "/etc/lingsway/marzban/internal.crt",
        "XRAY_REALITY_DEST": "example.com:443",
        "XRAY_REALITY_SERVER_NAME": "example.com",
    }
    values.update(overrides)
    return values


def test_production_xray_file_is_constructible_without_external_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("production Xray construction attempted network access")

    monkeypatch.setattr(socket, "socket", reject_network)
    monkeypatch.setattr(
        "backend.app.providers.gateway.xray_file.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("Xray subprocess must be lazy"),
    )
    monkeypatch.setattr(
        "backend.app.providers.marzban_tls.ssl.create_default_context",
        lambda **_kwargs: pytest.fail("CA file must load only during an operation"),
    )

    settings = Settings.from_env(_production_xray_env())
    registry = build_registry(settings)
    try:
        assert settings.gateway_provider == "xray_file"
        assert isinstance(registry.gateway, XrayFileProvider)
        assert isinstance(registry.gateway._runtime, MarzbanXrayRuntime)  # noqa: SLF001
    finally:
        registry.close()


@pytest.mark.parametrize(
    ("name", "overrides", "expected"),
    [
        ("http Marzban URL", {"MARZBAN_BASE_URL": "http://marzban:8000"}, "verified HTTPS"),
        ("TLS verification disabled", {"MARZBAN_VERIFY_TLS": "false"}, "VERIFY_TLS"),
        ("default credentials", {"MARZBAN_ADMIN_PASSWORD": "CHANGE_ME"}, "credentials"),
        ("blank CA path", {"MARZBAN_CA_CERT_PATH": " "}, "CA_CERT_PATH"),
        (
            "example.invalid URL",
            {"MARZBAN_BASE_URL": "https://marzban.example.invalid"},
            "configured",
        ),
    ],
)
def test_production_xray_file_safety_guards_remain_fail_closed(
    name: str, overrides: dict[str, str], expected: str
) -> None:
    del name
    with pytest.raises(ValueError, match=expected):
        Settings.from_env(_production_xray_env(**overrides))


def test_production_xray_file_rejects_malformed_reality() -> None:
    with pytest.raises(XrayCompositionError, match="XRAY_REALITY_DEST"):
        build_registry(
            _xray_settings(
                app_env="production",
                jwt_secret="J" * 32,
                secret_encryption_key="S" * 44,
                marzban_base_url="https://marzban:8000",
                xray_reality_dest="",
            )
        )



def test_production_marzban_accounting_with_mock_gateway_remains_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "backend.app.providers.accounting.marzban.httpx.Client",
        lambda **_kwargs: _NoIoClient(),
    )
    settings = Settings.from_env(
        {
            "APP_ENV": "production",
            "JWT_SECRET": "J" * 32,
            "SECRET_ENCRYPTION_KEY": "S" * 44,
            "ACCOUNTING_PROVIDER": "marzban",
            "GATEWAY_PROVIDER": "mock",
            "MARZBAN_BASE_URL": "https://marzban.test",
            "MARZBAN_ADMIN_USERNAME": "admin",
            "MARZBAN_ADMIN_PASSWORD": "test-password",
            "MARZBAN_DEFAULT_PROTOCOL": "vless",
            "MARZBAN_DEFAULT_INBOUNDS_JSON": '{"vless": ["inbound-vless"]}',
            "MARZBAN_VERIFY_TLS": "true",
            "MARZBAN_CA_CERT_PATH": "/etc/lingsway/marzban/internal.crt",
        }
    )
    registry = build_registry(settings)
    try:
        assert isinstance(registry.accounting, MarzbanAccountingProvider)
        assert isinstance(registry.gateway, MockGatewayProvider)
    finally:
        registry.close()


def test_development_xray_file_remains_constructible_after_safety_gate() -> None:
    settings = Settings.from_env(
        {
            "APP_ENV": "development",
            "GATEWAY_PROVIDER": "xray_file",
            "MARZBAN_BASE_URL": "https://marzban.internal.invalid",
            "MARZBAN_ADMIN_USERNAME": "admin",
            "MARZBAN_ADMIN_PASSWORD": "test-password",
            "MARZBAN_DEFAULT_PROTOCOL": "vless",
            "MARZBAN_DEFAULT_INBOUNDS_JSON": '{"vless": ["inbound-vless"]}',
            "XRAY_REALITY_DEST": "example.com:443",
            "XRAY_REALITY_SERVER_NAME": "example.com",
        }
    )
    registry = build_registry(settings)
    try:
        assert isinstance(registry.gateway, XrayFileProvider)
    finally:
        registry.close()
