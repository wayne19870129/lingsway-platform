from collections.abc import Mapping
from dataclasses import dataclass, field

from backend.app.providers.base import (
    ApplyResult,
    CandidateConfig,
    CredentialResolver,
    DesiredRoutingState,
    HealthReport,
    ValidationResult,
)
from backend.app.providers.mock_support import raise_injected


@dataclass(slots=True)
class MockGatewayProvider:
    failures: Mapping[str, Exception] = field(default_factory=dict)
    applied_versions: list[str] = field(default_factory=list)

    def render(
        self, desired: DesiredRoutingState, resolver: CredentialResolver
    ) -> CandidateConfig:
        del resolver
        raise_injected(self.failures, "render")
        return CandidateConfig(
            {"user_routes": dict(desired.user_routes), "outbound_tags": desired.outbound_tags},
            "gateway-mock-v1",
        )

    def validate(self, candidate: CandidateConfig) -> ValidationResult:
        raise_injected(self.failures, "validate")
        return ValidationResult(valid=bool(candidate.version))

    def apply(self, candidate: CandidateConfig) -> ApplyResult:
        raise_injected(self.failures, "apply")
        self.applied_versions.append(candidate.version)
        return ApplyResult(True, candidate.version)

    def health(self) -> HealthReport:
        raise_injected(self.failures, "health")
        return HealthReport(True, {"provider": "mock"})
