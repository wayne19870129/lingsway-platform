"""Guard tests for Mihomo's post-reload health verification and rollback.

`reload()` returning without raising is not proof that Mihomo picked up the
new config -- these tests exercise the full apply/rollback matrix using a
fake `MihomoRuntime` with explicit failure-injection hooks, mirroring the
Xray guard coverage in `test_safe_reload.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from backend.app.providers.base import DesiredForwarderState, ForwarderListenerDTO, HealthReport
from backend.app.providers.forwarder.mihomo import (
    ControllerSecretSnapshot,
    MihomoCandidateConfig,
    MihomoForwarderProvider,
    MihomoRollbackUnknownError,
    MihomoRollbackVerifiedError,
    MihomoRuntimeError,
)
from backend.app.providers.forwarder.mihomo_projection import MihomoProjectionError


@dataclass
class Runtime:
    """Fake `MihomoRuntime` with one-shot failure-injection hooks.

    Each `*_error` hook fires at most once, the first time its method is
    called -- this lets a single instance model "the primary call raises,
    the rollback's call to the same method succeeds".
    """

    install_error: Exception | None = None
    reload_error: Exception | None = None
    restore_error: Exception | None = None
    rollback_reload_error: Exception | None = None
    health_values: list[bool | Exception] = field(default_factory=lambda: [True])
    events: list[str] = field(default_factory=list)
    reload_calls: int = 0
    installed: bytes | None = None
    restored: bool = False
    api_secret: str = "test-secret"
    secret_match_calls: int = 0

    def controller_secret_matches(self, secret: str) -> bool:
        self.secret_match_calls += 1
        return secret == self.api_secret

    def backup(self) -> Path:
        self.events.append("backup")
        return Path("backup.yaml")

    def install(self, document: bytes) -> None:
        self.events.append("install")
        if self.install_error is not None:
            error, self.install_error = self.install_error, None
            raise error
        self.installed = document

    def reload(self) -> None:
        self.reload_calls += 1
        self.events.append(f"reload:{self.reload_calls}")
        if self.reload_calls == 1 and self.reload_error is not None:
            raise self.reload_error
        if self.reload_calls == 2 and self.rollback_reload_error is not None:
            raise self.rollback_reload_error

    def restore(self, backup: Path) -> None:
        self.events.append("restore")
        if self.restore_error is not None:
            error, self.restore_error = self.restore_error, None
            raise error
        self.restored = True

    def health(self) -> HealthReport:
        self.events.append("health")
        value = self.health_values.pop(0)
        if isinstance(value, Exception):
            raise value
        return HealthReport(value)


class Resolver:
    def __init__(self, value: str = "test-secret") -> None:
        self.value = value

    def resolve(self, secret_ref: str) -> ControllerSecretSnapshot:
        return ControllerSecretSnapshot(secret_ref, 1, self.value)


def candidate(provider: MihomoForwarderProvider) -> MihomoCandidateConfig:
    template = provider.render(
        DesiredForwarderState(
            listener_specs=(ForwarderListenerDTO("listener", "socks", "127.0.0.1", 7891, "BLOCK"),),
            rules=({"match": "MATCH", "target": "BLOCK"},),
            deployment_constants={
                "external-controller": "172.30.0.10:9090",
                "api-secret-ref": "mihomo/api-secret",
                "api-secret-revision": 1,
            },
        )
    )
    return provider.finalize(template, Resolver())


def test_successful_apply_verifies_health_after_reload() -> None:
    runtime = Runtime()
    provider = MihomoForwarderProvider(runtime)

    result = provider.apply(candidate(provider))

    assert result.applied is True
    assert result.version
    assert runtime.secret_match_calls == 1
    assert runtime.events == ["backup", "install", "reload:1", "health"]


def test_candidate_fingerprint_is_secret_neutral() -> None:
    provider = MihomoForwarderProvider(Runtime())
    template = provider.render(
        DesiredForwarderState(
            listener_specs=(
                ForwarderListenerDTO("listener", "socks", "127.0.0.1", 7891, "BLOCK"),
            ),
            rules=({"match": "MATCH", "target": "BLOCK"},),
            deployment_constants={
                "external-controller": "172.30.0.10:9090",
                "api-secret-ref": "mihomo/api-secret",
                "api-secret-revision": 1,
            },
        )
    )

    first = provider.finalize(template, Resolver("secret-a"))
    second = provider.finalize(template, Resolver("secret-b"))

    assert first.version == second.version
    assert first.content["secret"] != second.content["secret"]
    assert first.version not in {"secret-a", "secret-b"}


def test_controller_secret_revision_rejects_bool() -> None:
    provider = MihomoForwarderProvider(Runtime())
    with pytest.raises(MihomoProjectionError, match="CONTROLLER_SECRET_REVISION_INVALID"):
        provider.render(
            DesiredForwarderState(
                listener_specs=(
                    ForwarderListenerDTO("listener", "socks", "127.0.0.1", 7891, "BLOCK"),
                ),
                rules=({"match": "MATCH", "target": "BLOCK"},),
                deployment_constants={
                    "external-controller": "172.30.0.10:9090",
                    "api-secret-ref": "mihomo/api-secret",
                    "api-secret-revision": True,
                },
            )
        )


def test_controller_secret_mismatch_fails_before_backup() -> None:
    runtime = Runtime(api_secret="configured-secret")
    provider = MihomoForwarderProvider(runtime)
    candidate_value = candidate(provider)

    with pytest.raises(MihomoRuntimeError, match="ROTATION_UNSUPPORTED") as error:
        provider.apply(candidate_value)

    assert runtime.events == []
    assert runtime.secret_match_calls == 1
    assert "configured-secret" not in str(error.value)
    assert "test-secret" not in str(error.value)


def test_install_exception_triggers_rollback_and_fails_closed() -> None:
    runtime = Runtime(install_error=RuntimeError("disk full"))
    provider = MihomoForwarderProvider(runtime)

    with pytest.raises(MihomoRollbackVerifiedError, match="MIHOMO_ROLLBACK_VERIFIED"):
        provider.apply(candidate(provider))

    assert runtime.events == ["backup", "install", "restore", "reload:1", "health"]
    assert runtime.restored is True


def test_reload_exception_triggers_rollback_and_fails_closed() -> None:
    runtime = Runtime(reload_error=RuntimeError("hot reload failed"))
    provider = MihomoForwarderProvider(runtime)

    with pytest.raises(MihomoRollbackVerifiedError, match="MIHOMO_ROLLBACK_VERIFIED"):
        provider.apply(candidate(provider))

    assert runtime.events == ["backup", "install", "reload:1", "restore", "reload:2", "health"]
    assert runtime.restored is True


def test_unhealthy_post_reload_triggers_rollback_and_fails_closed() -> None:
    """A `reload()` that returns cleanly but leaves Mihomo unhealthy must
    not be reported as a successful apply."""
    runtime = Runtime(health_values=[False, True])
    provider = MihomoForwarderProvider(runtime)

    with pytest.raises(MihomoRollbackVerifiedError, match="MIHOMO_ROLLBACK_VERIFIED"):
        provider.apply(candidate(provider))

    assert runtime.events == [
        "backup",
        "install",
        "reload:1",
        "health",
        "restore",
        "reload:2",
        "health",
    ]
    assert runtime.restored is True


def test_rollback_restore_exception_fails_closed_without_claiming_recovery() -> None:
    runtime = Runtime(health_values=[False], restore_error=RuntimeError("disk unavailable"))
    provider = MihomoForwarderProvider(runtime)

    with pytest.raises(
        MihomoRollbackUnknownError, match="MIHOMO_ROLLBACK_OUTCOME_UNKNOWN"
    ):
        provider.apply(candidate(provider))

    assert runtime.events == ["backup", "install", "reload:1", "health", "restore"]
    assert runtime.reload_calls == 1
    assert runtime.restored is False


def test_rollback_reload_exception_fails_closed_without_claiming_recovery() -> None:
    runtime = Runtime(
        health_values=[False],
        rollback_reload_error=RuntimeError("hot reload failed"),
    )
    provider = MihomoForwarderProvider(runtime)

    with pytest.raises(
        MihomoRollbackUnknownError, match="MIHOMO_ROLLBACK_OUTCOME_UNKNOWN"
    ):
        provider.apply(candidate(provider))

    assert runtime.events == [
        "backup",
        "install",
        "reload:1",
        "health",
        "restore",
        "reload:2",
    ]
    assert runtime.restored is True


def test_rollback_health_exception_fails_closed_without_claiming_recovery() -> None:
    runtime = Runtime(health_values=[False, RuntimeError("health probe crashed")])
    provider = MihomoForwarderProvider(runtime)

    with pytest.raises(
        MihomoRollbackUnknownError, match="MIHOMO_ROLLBACK_OUTCOME_UNKNOWN"
    ):
        provider.apply(candidate(provider))

    assert runtime.events == [
        "backup",
        "install",
        "reload:1",
        "health",
        "restore",
        "reload:2",
        "health",
    ]


def test_rollback_reload_succeeds_but_still_unhealthy_fails_closed() -> None:
    """Copying the backup back and reloading is not the same as a
    confirmed-safe runtime: if the rollback's own health check still
    reports unhealthy, this must fail closed rather than claim recovery."""
    runtime = Runtime(health_values=[False, False])
    provider = MihomoForwarderProvider(runtime)

    with pytest.raises(
        MihomoRollbackUnknownError, match="MIHOMO_ROLLBACK_OUTCOME_UNKNOWN"
    ):
        provider.apply(candidate(provider))

    assert runtime.events == [
        "backup",
        "install",
        "reload:1",
        "health",
        "restore",
        "reload:2",
        "health",
    ]
    assert runtime.restored is True
