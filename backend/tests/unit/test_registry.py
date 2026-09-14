import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from backend.app.core.config import Settings
from backend.app.providers.accounting.mock import MockAccountingProvider
from backend.app.providers.captcha.noop import NoopCaptchaProvider
from backend.app.providers.egress.mock import MockEgressProvider
from backend.app.providers.email.noop import NoopEmailProvider
from backend.app.providers.forwarder.mock import MockForwarderProvider
from backend.app.providers.gateway.mock import MockGatewayProvider
from backend.app.providers.gateway.xray_file import LocalXrayRuntime, XrayFileProvider
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


def test_registry_rejects_unimplemented_transport_selection() -> None:
    with pytest.raises(ProviderConfigurationError, match="TRANSPORT_PROVIDER_MODE"):
        build_registry(Settings(transport_provider_mode="subscription"))


# ---------------------------------------------------------------------------
# TASK-T16 Phase 2C1: explicit opt-in Xray gateway registry wiring.
# ---------------------------------------------------------------------------


def _reject_side_effect(name: str) -> Callable[..., None]:
    def _raise(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"build_registry() attempted {name} during construction")

    return _raise


def _xray_runtime(gateway: object) -> LocalXrayRuntime:
    assert isinstance(gateway, XrayFileProvider)
    runtime = gateway._runtime  # noqa: SLF001 -- test-only introspection
    assert isinstance(runtime, LocalXrayRuntime)
    return runtime


def test_default_settings_still_select_the_mock_gateway() -> None:
    """Default Settings() (no GATEWAY_PROVIDER override) must keep
    building the same all-mock/noop registry as before this change."""
    registry = build_registry(Settings())

    assert isinstance(registry.gateway, MockGatewayProvider)


def test_explicit_xray_file_gateway_selection_returns_xray_file_provider() -> None:
    settings = Settings(gateway_provider="xray_file")

    registry = build_registry(settings)

    assert isinstance(registry.gateway, XrayFileProvider)
    assert isinstance(_xray_runtime(registry.gateway), LocalXrayRuntime)


def test_settings_runtime_paths_and_command_propagate_exactly_into_local_xray_runtime() -> None:
    settings = Settings(
        gateway_provider="xray_file",
        xray_config_path="/opt/lingsway/xray/config.json",
        xray_backup_dir="/opt/lingsway/xray/backups",
        xray_binary_path="/opt/xray/bin/xray",
        xray_asset_dir="/opt/xray/share",
        xray_reload_command="systemctl reload xray-custom",
    )

    registry = build_registry(settings)

    runtime = _xray_runtime(registry.gateway)
    assert runtime.config_path == Path("/opt/lingsway/xray/config.json")
    assert runtime.backup_dir == Path("/opt/lingsway/xray/backups")
    assert runtime.xray_binary == Path("/opt/xray/bin/xray")
    assert runtime.asset_dir == Path("/opt/xray/share")
    assert runtime.reload_command == ("systemctl", "reload", "xray-custom")


def test_xray_file_gateway_defaults_match_local_xray_runtime_own_defaults() -> None:
    """Opting in without overriding any of the new xray_* fields must
    produce the exact same LocalXrayRuntime a caller would get by
    constructing it directly with only config_path/backup_dir set."""
    settings = Settings(gateway_provider="xray_file")
    direct = LocalXrayRuntime(
        config_path=Path(settings.xray_config_path),
        backup_dir=Path(settings.xray_backup_dir),
    )

    runtime = _xray_runtime(build_registry(settings).gateway)

    assert runtime.xray_binary == direct.xray_binary
    assert runtime.asset_dir == direct.asset_dir
    assert runtime.reload_command == direct.reload_command


def test_build_registry_performs_no_io_or_subprocess_when_selecting_xray(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Constructing the Xray gateway provider must be pure object assembly:
    no filesystem reads/writes, no subprocess, no socket, no reload."""
    monkeypatch.setattr(subprocess, "run", _reject_side_effect("subprocess.run"))
    monkeypatch.setattr(socket, "socket", _reject_side_effect("socket.socket"))
    monkeypatch.setattr(
        socket, "create_connection", _reject_side_effect("socket.create_connection")
    )
    monkeypatch.setattr(Path, "read_text", _reject_side_effect("Path.read_text"))
    monkeypatch.setattr(Path, "write_text", _reject_side_effect("Path.write_text"))
    monkeypatch.setattr(Path, "mkdir", _reject_side_effect("Path.mkdir"))
    monkeypatch.setattr(Path, "open", _reject_side_effect("Path.open"))

    settings = Settings(gateway_provider="xray_file")
    registry = build_registry(settings)

    assert isinstance(registry.gateway, XrayFileProvider)


def test_registry_rejects_unknown_gateway_selection() -> None:
    with pytest.raises(ProviderConfigurationError, match="GATEWAY_PROVIDER"):
        build_registry(Settings(gateway_provider="mihomo"))


def test_xray_gateway_selection_does_not_wire_any_other_real_provider() -> None:
    """Selecting the Xray gateway must not accidentally change any other
    provider category away from its mock/noop default -- Phase 2C1 wires
    exactly one category, nothing else."""
    registry = build_registry(Settings(gateway_provider="xray_file"))

    assert isinstance(registry.egress, MockEgressProvider)
    assert isinstance(registry.accounting, MockAccountingProvider)
    assert isinstance(registry.forwarder, MockForwarderProvider)
    assert isinstance(registry.payment, MockPaymentProvider)
    assert isinstance(registry.notify, NoopNotifyProvider)
    assert isinstance(registry.email, NoopEmailProvider)
    assert isinstance(registry.captcha, NoopCaptchaProvider)
    assert isinstance(registry.storage, MockBlobStorage)
    assert isinstance(registry.transport, MockTransportProvider)


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
