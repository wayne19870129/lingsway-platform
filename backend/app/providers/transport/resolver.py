"""DB-neutral, lazy ownership boundary for subscription transport providers."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from backend.app.core.secrets import SecretSnapshot
from backend.app.providers.transport.subscription import SubscriptionTransportProvider

_SAFE_CODE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class TransportProviderDescriptor:
    record_id: int
    code: str
    kind: str
    secret_ref: str
    implementation: str = "subscription"


class TransportResolutionError(RuntimeError):
    """Secret-safe fail-closed transport resolution error."""


class SubscriptionTransportResolver:
    def __init__(
        self,
        cache_root: Path,
    ) -> None:
        self._cache_root = cache_root
        self._providers: dict[
            int, tuple[TransportProviderDescriptor, int, SubscriptionTransportProvider]
        ] = {}
        self._codes: dict[str, int] = {}
        self._cache_paths: dict[Path, int] = {}
        self._closed_provider_ids: set[int] = set()
        self._closed = False

    @staticmethod
    def _validate(descriptor: TransportProviderDescriptor) -> None:
        if (
            descriptor.record_id <= 0
            or not descriptor.code.strip()
            or not descriptor.secret_ref.strip()
        ):
            raise TransportResolutionError("TRANSPORT_DESCRIPTOR_INVALID")
        if descriptor.kind != "SUBSCRIPTION":
            raise TransportResolutionError("TRANSPORT_KIND_UNSUPPORTED")
        if descriptor.implementation != "subscription":
            raise TransportResolutionError("TRANSPORT_IMPLEMENTATION_UNSUPPORTED")

    def _cache_path(self, descriptor: TransportProviderDescriptor) -> Path:
        safe_code = _SAFE_CODE.sub("_", descriptor.code).strip("._") or "provider"
        identity = "|".join(
            (str(descriptor.record_id), descriptor.code, descriptor.kind, descriptor.implementation)
        )
        digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
        return self._cache_root / f"{descriptor.record_id}-{safe_code}-{digest}.yaml"

    def resolve(
        self,
        descriptor: TransportProviderDescriptor,
        secret_loader: Callable[[str, str], SecretSnapshot],
    ) -> SubscriptionTransportProvider:
        self._validate(descriptor)
        existing = self._providers.get(descriptor.record_id)
        if existing is not None:
            old_descriptor, old_revision, provider = existing
            if old_descriptor != descriptor:
                raise TransportResolutionError("TRANSPORT_DESCRIPTOR_DRIFT")
            snapshot = secret_loader(
                descriptor.secret_ref, "TRANSPORT_SUBSCRIPTION_URL"
            )
            if snapshot.revision != old_revision:
                raise TransportResolutionError("TRANSPORT_SECRET_REVISION_DRIFT")
            return provider

        bound_record = self._codes.get(descriptor.code)
        if bound_record is not None and bound_record != descriptor.record_id:
            raise TransportResolutionError("TRANSPORT_CODE_COLLISION")
        cache_path = self._cache_path(descriptor)
        owner = self._cache_paths.get(cache_path)
        if owner is not None and owner != descriptor.record_id:
            raise TransportResolutionError("TRANSPORT_CACHE_IDENTITY_COLLISION")
        snapshot = secret_loader(descriptor.secret_ref, "TRANSPORT_SUBSCRIPTION_URL")
        provider = SubscriptionTransportProvider(
            descriptor.code,
            snapshot.value,
            cache_path=cache_path,
        )
        self._providers[descriptor.record_id] = (descriptor, snapshot.revision, provider)
        self._codes[descriptor.code] = descriptor.record_id
        self._cache_paths[cache_path] = descriptor.record_id
        return provider

    def close(self) -> None:
        if self._closed:
            return
        errors: list[Exception] = []
        for provider_id, provider_data in self._providers.items():
            if provider_id in self._closed_provider_ids:
                continue
            try:
                provider_data[2].close()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            else:
                self._closed_provider_ids.add(provider_id)
        if errors:
            raise ExceptionGroup("Subscription transport resolver close failed", errors)
        self._closed = True
