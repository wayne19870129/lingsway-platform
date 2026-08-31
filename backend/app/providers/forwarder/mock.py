from collections.abc import Mapping
from dataclasses import dataclass, field

from backend.app.providers.base import (
    ApplyResult,
    CandidateConfig,
    DesiredForwarderState,
    HealthReport,
)
from backend.app.providers.mock_support import raise_injected


@dataclass(slots=True)
class MockForwarderProvider:
    failures: Mapping[str, Exception] = field(default_factory=dict)
    applied_versions: list[str] = field(default_factory=list)

    def render(self, desired: DesiredForwarderState) -> CandidateConfig:
        raise_injected(self.failures, "render")
        return CandidateConfig({"listeners": dict(desired.listeners)}, "forwarder-mock-v1")

    def apply(self, candidate: CandidateConfig) -> ApplyResult:
        raise_injected(self.failures, "apply")
        self.applied_versions.append(candidate.version)
        return ApplyResult(True, candidate.version)

    def health(self) -> HealthReport:
        raise_injected(self.failures, "health")
        return HealthReport(True, {"provider": "mock"})
