"""Provider assembly driven exclusively by :class:`Settings`."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType

from backend.app.core.config import Settings
from backend.app.providers.accounting.mock import MockAccountingProvider
from backend.app.providers.base import (
    AccountingProvider,
    BlobStorage,
    CaptchaProvider,
    EgressProvider,
    EmailProvider,
    ForwarderProvider,
    GatewayProvider,
    NotifyProvider,
    PaymentProvider,
    TransportProvider,
)
from backend.app.providers.captcha.noop import NoopCaptchaProvider
from backend.app.providers.egress.mock import MockEgressProvider
from backend.app.providers.email.noop import NoopEmailProvider
from backend.app.providers.forwarder.mock import MockForwarderProvider
from backend.app.providers.gateway.mock import MockGatewayProvider
from backend.app.providers.gateway.xray_file import LocalXrayRuntime, XrayFileProvider
from backend.app.providers.notify.noop import NoopNotifyProvider
from backend.app.providers.payment.mock import MockPaymentProvider
from backend.app.providers.storage.mock import MockBlobStorage
from backend.app.providers.transport.mock import MockTransportProvider

#: TASK-T16 Phase 2C1: the only non-mock gateway selection accepted by
#: build_registry() so far. Any other value still fails closed via
#: _unsupported() below -- adding a new real provider elsewhere must not
#: silently start being accepted here.
_SUPPORTED_GATEWAY_PROVIDERS = frozenset({"mock", "xray_file"})


class ProviderConfigurationError(ValueError):
    """Raised when settings select an unavailable provider implementation."""


@dataclass(frozen=True, slots=True)
class ProviderRegistry:
    """The complete set of providers selected for one process/application
    lifetime.

    TASK-T16 Phase 2B8: this is also the sole ownership boundary for any
    resource a provider creates for itself (e.g. an ``httpx.Client`` a
    provider builds because none was injected). ``ProviderRegistry`` does
    not need to know which concrete providers own a closeable resource --
    :meth:`close` duck-types: any provider exposing a callable ``close``
    attribute gets it called once. Mock/noop providers (everything
    ``build_registry()`` assembles today) simply have no ``close`` method
    and are silently skipped, so this costs nothing before a real,
    resource-owning provider is ever wired in.

    Still ``frozen=True`` (independent-review round 4, Minor 1): the
    public provider fields (``egress``, ``accounting``, ...) must stay
    immutable so a caller can never reassign one to a fresh, unclosed
    provider after a successful ``close()`` -- that would let
    ``self._closed`` (already ``True``) permanently hide the replacement
    from ever being closed. Only the private lifecycle bookkeeping
    (``_closed``, ``_closed_provider_ids``) needs to change after
    construction; ``_closed`` is updated via ``object.__setattr__`` for
    that reason, and ``_closed_provider_ids`` is a set mutated in place
    (``.add()``), which a frozen dataclass never prevents.
    """

    egress: EgressProvider
    accounting: AccountingProvider
    gateway: GatewayProvider
    forwarder: ForwarderProvider
    payment: PaymentProvider
    notify: NotifyProvider
    email: EmailProvider
    captcha: CaptchaProvider
    storage: BlobStorage
    transport: TransportProvider
    _closed: bool = field(default=False, init=False, repr=False, compare=False)
    #: ids of providers already successfully closed (or found to have no
    #: closeable resource at all) across every close() call so far --
    #: never re-closed, and never blocking a *different*, still-failing
    #: provider's close attempt (independent-review Major 2).
    _closed_provider_ids: set[int] = field(
        default_factory=set, init=False, repr=False, compare=False
    )

    def close(self) -> None:
        """Close every provider this registry holds that owns a
        closeable resource.

        Deliberately **not** short-circuited by one provider's ``close()``
        raising: a resource-owning provider is never skipped just because
        an earlier one in iteration order failed -- otherwise a single
        broken provider would permanently leak every provider after it.
        Idempotent per-provider (each provider's ``close()`` is called at
        most once across the lifetime of this registry, tracked by
        identity so the same object filling two roles is still only
        closed once) and idempotent as a whole once every provider has
        succeeded (``self._closed`` becomes ``True`` only then). Calling
        this again after a partial failure retries only the providers
        that failed or were not yet attempted -- never the ones that
        already closed successfully.

        Raises an ``ExceptionGroup`` wrapping every provider's ``close()``
        failure if any occurred, only after every other provider has had
        its own close attempted -- cleanup failures are surfaced, never
        silently swallowed.
        """
        if self._closed:
            return
        seen_this_call: set[int] = set()
        errors: list[Exception] = []
        for provider in (
            self.egress,
            self.accounting,
            self.gateway,
            self.forwarder,
            self.payment,
            self.notify,
            self.email,
            self.captcha,
            self.storage,
            self.transport,
        ):
            provider_id = id(provider)
            if provider_id in self._closed_provider_ids or provider_id in seen_this_call:
                # Already closed in a previous call, or the identical
                # object fills more than one role in this registry --
                # either way, close it at most once.
                continue
            seen_this_call.add(provider_id)
            closer = getattr(provider, "close", None)
            if not callable(closer):
                self._closed_provider_ids.add(provider_id)
                continue
            try:
                closer()
            except Exception as exc:  # noqa: BLE001 -- one provider's failure must never skip the rest
                errors.append(exc)
            else:
                self._closed_provider_ids.add(provider_id)
        if errors:
            raise ExceptionGroup(
                "ProviderRegistry.close() failed to close one or more providers", errors
            )
        object.__setattr__(self, "_closed", True)

    def __enter__(self) -> ProviderRegistry:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.close()


def build_registry(settings: Settings) -> ProviderRegistry:
    """Build all providers without network, Docker, filesystem, or shell access."""
    if settings.egress_provider != "mock":
        raise _unsupported("EGRESS_PROVIDER", settings.egress_provider)
    if settings.accounting_provider != "mock":
        raise _unsupported("ACCOUNTING_PROVIDER", settings.accounting_provider)
    if settings.gateway_provider not in _SUPPORTED_GATEWAY_PROVIDERS:
        raise _unsupported("GATEWAY_PROVIDER", settings.gateway_provider)
    if settings.forwarder_provider != "mock":
        raise _unsupported("FORWARDER_PROVIDER", settings.forwarder_provider)
    if settings.payment_provider != "mock":
        raise _unsupported("PAYMENT_PROVIDER", settings.payment_provider)
    if settings.notify_provider != "noop":
        raise _unsupported("NOTIFY_PROVIDER", settings.notify_provider)
    if settings.email_provider != "noop":
        raise _unsupported("EMAIL_PROVIDER", settings.email_provider)
    if settings.captcha_provider != "noop":
        raise _unsupported("CAPTCHA_PROVIDER", settings.captcha_provider)
    if settings.storage_provider != "mock":
        raise _unsupported("STORAGE_PROVIDER", settings.storage_provider)
    if settings.transport_provider_mode != "mock":
        raise _unsupported("TRANSPORT_PROVIDER_MODE", settings.transport_provider_mode)

    return ProviderRegistry(
        egress=MockEgressProvider(),
        accounting=MockAccountingProvider(),
        gateway=_build_gateway(settings),
        forwarder=MockForwarderProvider(),
        payment=MockPaymentProvider(),
        notify=NoopNotifyProvider(),
        email=NoopEmailProvider(),
        captcha=NoopCaptchaProvider(),
        storage=MockBlobStorage(),
        transport=MockTransportProvider(),
    )


def _build_gateway(settings: Settings) -> GatewayProvider:
    """Assemble the selected gateway provider from parsed Settings values only.

    TASK-T16 Phase 2C1: this must never perform real filesystem, network,
    subprocess, or reload IO. ``Path(...)`` below only wraps a string (no
    filesystem access), and ``LocalXrayRuntime``/``XrayFileProvider``
    construction is pure attribute assignment (see
    docs/82-tasks/TASK-T16-real-provider-registry-wiring.md's "构造 vs.
    组装 vs. 运行期" section) -- every IO-performing method on the
    resulting runtime/provider is only ever invoked later, by a caller
    that explicitly chooses to call ``apply()``/``reload()``/etc.
    """
    if settings.gateway_provider == "mock":
        return MockGatewayProvider()
    runtime = LocalXrayRuntime(
        config_path=Path(settings.xray_runtime_config_path),
        backup_dir=Path(settings.xray_backup_dir),
        xray_binary=Path(settings.xray_binary_path),
        asset_dir=Path(settings.xray_asset_dir),
        reload_command=tuple(settings.xray_reload_command.split()),
    )
    return XrayFileProvider(runtime)


def _unsupported(variable: str, value: str) -> ProviderConfigurationError:
    return ProviderConfigurationError(
        f"{variable}={value!r} is not implemented in T2; use the mock/noop selection"
    )
