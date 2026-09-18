"""DB-neutral, lazy ownership boundary for subscription transport providers."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from backend.app.core.secrets import (
    SecretSnapshot,
    SecretStoreError,
    reveal_secret_snapshot_for_purpose,
)
from backend.app.models import TransportProviderKind
from backend.app.providers.transport.subscription import SubscriptionTransportProvider

TRANSPORT_SUBSCRIPTION_URL_PURPOSE = "TRANSPORT_SUBSCRIPTION_URL"
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
        session_factory: Callable[[], object],
    ) -> None:
        self._cache_root = cache_root
        self._session_factory = session_factory
        self._providers: dict[
            int, tuple[TransportProviderDescriptor, int, SubscriptionTransportProvider]
        ] = {}
        self._codes: dict[str, int] = {}
        self._cache_paths: dict[Path, int] = {}

    @staticmethod
    def descriptor_for(record: object) -> TransportProviderDescriptor:
        descriptor = TransportProviderDescriptor(
            record_id=int(record.id),
            code=str(record.code),
            kind=str(record.kind.value if hasattr(record.kind, "value") else record.kind),
            secret_ref=str(record.secret_ref),
        )
        if (
            descriptor.record_id <= 0
            or not descriptor.code.strip()
            or not descriptor.secret_ref.strip()
        ):
            raise TransportResolutionError("TRANSPORT_DESCRIPTOR_INVALID")
        if descriptor.kind != TransportProviderKind.SUBSCRIPTION.value:
            raise TransportResolutionError("TRANSPORT_KIND_UNSUPPORTED")
        if descriptor.implementation != "subscription":
            raise TransportResolutionError("TRANSPORT_IMPLEMENTATION_UNSUPPORTED")
        return descriptor

    def _cache_path(self, descriptor: TransportProviderDescriptor) -> Path:
        safe_code = _SAFE_CODE.sub("_", descriptor.code).strip("._") or "provider"
        identity = "|".join(
            (str(descriptor.record_id), descriptor.code, descriptor.kind, descriptor.implementation)
        )
        digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
        return self._cache_root / f"{descriptor.record_id}-{safe_code}-{digest}.yaml"

    def resolve(self, descriptor: TransportProviderDescriptor) -> SubscriptionTransportProvider:
        existing = self._providers.get(descriptor.record_id)
        if existing is not None:
            old_descriptor, old_revision, provider = existing
            if old_descriptor != descriptor:
                raise TransportResolutionError("TRANSPORT_DESCRIPTOR_DRIFT")
            snapshot = self._snapshot(descriptor.secret_ref)
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
        snapshot = self._snapshot(descriptor.secret_ref)
        provider = SubscriptionTransportProvider(
            descriptor.code,
            snapshot.value,
            cache_path=cache_path,
        )
        self._providers[descriptor.record_id] = (descriptor, snapshot.revision, provider)
        self._codes[descriptor.code] = descriptor.record_id
        self._cache_paths[cache_path] = descriptor.record_id
        return provider

    def _snapshot(self, secret_ref: str) -> SecretSnapshot:
        db = self._session_factory()
        try:
            return reveal_secret_snapshot_for_purpose(
                db, secret_ref, TRANSPORT_SUBSCRIPTION_URL_PURPOSE  # type: ignore[arg-type]
            )
        except SecretStoreError as exc:
            raise TransportResolutionError("TRANSPORT_SECRET_UNAVAILABLE") from exc
        finally:
            db.close()  # type: ignore[attr-defined]

    def close(self) -> None:
        errors: list[Exception] = []
        for provider in self._providers.values():
            try:
                provider[2].close()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        if errors:
            raise ExceptionGroup("Subscription transport resolver close failed", errors)
