from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from backend.app.providers.base import (
    CapacityDTO,
    CredentialDTO,
    EgressEndpointDTO,
    ReplacementDTO,
    TenantDTO,
    UsageDTO,
)
from backend.app.providers.mock_support import raise_injected


@dataclass(slots=True)
class MockEgressProvider:
    failures: Mapping[str, Exception] = field(default_factory=dict)
    name: str = "mock"
    endpoints: list[EgressEndpointDTO] = field(
        default_factory=lambda: [
            EgressEndpointDTO("egress-1", "127.0.0.1", 1080, public_ip="192.0.2.10")
        ]
    )
    tenants: dict[str, TenantDTO] = field(default_factory=dict)
    total_gb: Decimal = Decimal("1000")
    reserved_gb: Decimal = Decimal("100")

    def list_endpoints(self) -> list[EgressEndpointDTO]:
        raise_injected(self.failures, "list_endpoints")
        return list(self.endpoints)

    def capacity(self) -> CapacityDTO:
        raise_injected(self.failures, "capacity")
        allocated = sum((tenant.quota_gb for tenant in self.tenants.values()), Decimal())
        return CapacityDTO(self.total_gb, allocated, self.reserved_gb)

    def create_tenant(
        self, label: str, quota_gb: Decimal, thread_limit: int
    ) -> TenantDTO:
        raise_injected(self.failures, "create_tenant")
        tenant = TenantDTO(f"tenant-{len(self.tenants) + 1}", label, quota_gb, thread_limit)
        self.tenants[tenant.tenant_id] = tenant
        return tenant

    def update_tenant_quota(self, tenant_id: str, quota_gb: Decimal) -> TenantDTO:
        raise_injected(self.failures, "update_tenant_quota")
        current = self.tenants[tenant_id]
        updated = TenantDTO(current.tenant_id, current.label, quota_gb, current.thread_limit)
        self.tenants[tenant_id] = updated
        return updated

    def get_tenant_usage(self, tenant_id: str) -> UsageDTO:
        raise_injected(self.failures, "get_tenant_usage")
        return UsageDTO(tenant_id, Decimal(), datetime.now(UTC))

    def get_credentials(self, tenant_id: str, endpoint_id: str) -> CredentialDTO:
        raise_injected(self.failures, "get_credentials")
        return CredentialDTO(f"mock-{tenant_id}-{endpoint_id}", "mock-password")

    def replace_endpoint(
        self, endpoint_id: str, dry_run: bool = True
    ) -> ReplacementDTO:
        raise_injected(self.failures, "replace_endpoint")
        replacement_id = f"{endpoint_id}-replacement" if not dry_run else None
        return ReplacementDTO(endpoint_id, replacement_id, dry_run)
