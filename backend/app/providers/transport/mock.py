from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from backend.app.providers.base import TransportCapacityDTO, TransportEndpointDTO
from backend.app.providers.mock_support import raise_injected


@dataclass(slots=True)
class MockTransportProvider:
    """In-memory transport inventory for unit tests and local development."""

    failures: Mapping[str, Exception] = field(default_factory=dict)
    endpoints: list[TransportEndpointDTO] = field(default_factory=list)
    capacity_data: TransportCapacityDTO | None = None
    enabled: bool = True

    def health_check(self) -> bool:
        raise_injected(self.failures, "health_check")
        return self.enabled

    def sync_nodes(self) -> None:
        raise_injected(self.failures, "sync_nodes")

    def list_endpoints(self) -> list[TransportEndpointDTO]:
        raise_injected(self.failures, "list_endpoints")
        return list(self.endpoints)

    def get_endpoint(self, external_id: str) -> TransportEndpointDTO | None:
        raise_injected(self.failures, "get_endpoint")
        return next(
            (endpoint for endpoint in self.endpoints if endpoint.external_id == external_id),
            None,
        )

    def enable(self) -> None:
        raise_injected(self.failures, "enable")
        self.enabled = True

    def disable(self) -> None:
        raise_injected(self.failures, "disable")
        self.enabled = False

    def get_capacity(self) -> TransportCapacityDTO | None:
        raise_injected(self.failures, "get_capacity")
        return self.capacity_data
