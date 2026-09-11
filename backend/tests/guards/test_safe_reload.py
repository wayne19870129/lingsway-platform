from collections.abc import Mapping
from pathlib import Path

import pytest

from backend.app.providers.base import CandidateConfig, HealthReport
from backend.app.providers.gateway.xray_file import (
    XrayFileProvider,
    XrayReloadError,
    XrayValidationError,
)


def config(*, users: tuple[str, ...] = ("old-user",)) -> dict[str, object]:
    return {
        "routing": {
            "rules": [
                {"type": "field", "ip": ["geoip:private"], "outboundTag": "BLOCK"},
                *(
                    {"type": "field", "user": [user], "outboundTag": "egress-1"}
                    for user in users
                ),
                {
                    "type": "field",
                    "network": "tcp,udp",
                    "outboundTag": "BLOCK",
                },
            ]
        },
        "outbounds": [
            {"tag": "BLOCK", "protocol": "blackhole"},
            {"tag": "egress-1", "protocol": "socks"},
        ],
    }


class Runtime:
    """Fake `XrayRuntime` with explicit failure-injection hooks.

    Every hook is `None` by default (no injected failure). Each hook fires
    at most once, the first time its corresponding method is called, so a
    single `Runtime` instance can model "this call raises, the next
    (rollback) call to the same method succeeds" -- e.g. `reload_errors`
    injects a failure on a specific 1-indexed `reload()` call number, since
    the same method is called once for the primary apply and again (if
    reached) during rollback.
    """

    def __init__(
        self,
        *,
        xray_valid: bool = True,
        health: list[bool | Exception] | None = None,
        install_error: Exception | None = None,
        reload_errors: dict[int, Exception] | None = None,
        restore_error: Exception | None = None,
    ) -> None:
        self.xray_valid = xray_valid
        self.health_values: list[bool | Exception] = health or [True]
        self.install_error = install_error
        self.reload_errors = reload_errors or {}
        self.restore_error = restore_error
        self.events: list[str] = []
        self.timeline: list[str] = []
        self.reload_calls = 0
        self._current: Mapping[str, object] = config()
        self._backup: Mapping[str, object] = self._current

    def backup(self) -> object:
        self.events.append("backup")
        self.timeline.append("backup")
        self._backup = self._current
        return Path("backup.json")

    def current(self) -> Mapping[str, object]:
        return self._current

    def xray_test(self, content: Mapping[str, object]) -> bool:
        self.events.append("xray_test")
        self.timeline.append("xray_test")
        return self.xray_valid

    def install(self, content: Mapping[str, object]) -> None:
        self.events.append("install")
        self.timeline.append("install")
        if self.install_error is not None:
            error, self.install_error = self.install_error, None
            raise error
        self._current = content

    def reload(self) -> None:
        self.reload_calls += 1
        self.events.append(f"reload:{self.reload_calls}")
        self.timeline.append(f"reload:{self.reload_calls}")
        if self.reload_calls in self.reload_errors:
            raise self.reload_errors[self.reload_calls]

    def health(self) -> HealthReport:
        self.events.append("health")
        self.timeline.append("health")
        value = self.health_values.pop(0)
        if isinstance(value, Exception):
            raise value
        return HealthReport(value)

    def restore(self, backup: object) -> None:
        self.events.append("restore")
        self.timeline.append("restore")
        if self.restore_error is not None:
            error, self.restore_error = self.restore_error, None
            raise error
        self._current = self._backup


def test_invalid_candidate_stops_before_reload() -> None:
    runtime = Runtime(xray_valid=False)
    provider = XrayFileProvider(runtime)
    candidate = CandidateConfig(config(), "invalid")

    with pytest.raises(XrayValidationError):
        provider.apply(candidate, new_username="new-user")

    assert runtime.reload_calls == 0
    assert not any(event.startswith("reload:") for event in runtime.events)
    assert runtime.events == ["backup", "xray_test"]


def test_failed_post_reload_health_executes_complete_rollback_sequence() -> None:
    runtime = Runtime(health=[False, True])
    disabled: list[str] = []
    alerts: list[str] = []
    audits: list[str] = []

    def disable(username: str) -> None:
        disabled.append(username)
        runtime.timeline.append("disable")

    def alert(message: str) -> None:
        alerts.append(message)
        runtime.timeline.append("alert")

    provider = XrayFileProvider(
        runtime,
        disable_user=disable,
        alert=alert,
        audit=lambda event, _details: audits.append(event),
    )

    with pytest.raises(XrayReloadError):
        provider.apply(
            CandidateConfig(config(users=("old-user", "new-user")), "v2"),
            new_username="new-user",
        )

    # Exact operational order: restore -> second reload -> reverify -> disable -> alert.
    assert runtime.events == [
        "backup",
        "xray_test",
        "install",
        "reload:1",
        "health",
        "restore",
        "reload:2",
        "health",
    ]
    assert runtime.reload_calls == 2
    assert disabled == ["new-user"]
    assert len(alerts) == 1
    restore_index = runtime.timeline.index("restore")
    assert runtime.timeline[restore_index : restore_index + 5] == [
        "restore",
        "reload:2",
        "health",
        "disable",
        "alert",
    ]
    assert audits[-5:] == [
        "backup_restored",
        "rollback_reload",
        "rollback_reverified",
        "new_user_disabled",
        "alert_sent",
    ]


def _apply_with_recorders(
    runtime: Runtime, candidate: dict[str, object], *, new_username: str | None
) -> tuple[list[str], list[str], list[str], XrayReloadError]:
    """Run `apply()` expecting failure; return (disabled, alerts, audits, exc)."""
    disabled: list[str] = []
    alerts: list[str] = []
    audits: list[str] = []
    provider = XrayFileProvider(
        runtime,
        disable_user=lambda u: disabled.append(u),
        alert=lambda m: alerts.append(m),
        audit=lambda event, _details: audits.append(event),
    )
    with pytest.raises(XrayReloadError) as excinfo:
        provider.apply(CandidateConfig(candidate, "v2"), new_username=new_username)
    return disabled, alerts, audits, excinfo.value


def test_install_exception_triggers_full_rollback_and_fails_closed() -> None:
    """`install()` raising must not leave a bare exception, nor be treated
    as a successful apply -- rollback must run and, if it succeeds, the
    call must still fail closed rather than silently reporting success."""
    original_error = RuntimeError("disk full")
    runtime = Runtime(install_error=original_error)
    disabled, alerts, audits, raised = _apply_with_recorders(
        runtime,
        config(users=("old-user", "new-user")),
        new_username="new-user",
    )

    # install() raised before any reload was attempted; rollback still runs
    # the full restore -> reload -> health sequence and confirms success.
    assert runtime.events == [
        "backup",
        "xray_test",
        "install",
        "restore",
        "reload:1",
        "health",
    ]
    assert runtime.reload_calls == 1
    assert disabled == ["new-user"]
    assert len(alerts) == 1
    assert audits == [
        "backup_created",
        "apply_mutation_failed",
        "backup_restored",
        "rollback_reload",
        "rollback_reverified",
        "new_user_disabled",
        "alert_sent",
    ]
    # The candidate never took effect: current config equals the pre-apply
    # backup, not the candidate that failed to install.
    assert runtime._current == runtime._backup
    # Original install() failure is preserved in the exception chain.
    assert raised.__context__ is original_error


def test_first_reload_exception_triggers_full_rollback_and_fails_closed() -> None:
    """The first `reload()` (after a successful `install()`) raising must
    trigger the same fail-closed rollback path as a post-reload health
    failure -- a successful recovery must still not be reported as apply
    success."""
    original_error = RuntimeError("systemctl reload failed")
    runtime = Runtime(reload_errors={1: original_error})
    disabled, alerts, audits, raised = _apply_with_recorders(
        runtime,
        config(users=("old-user", "new-user")),
        new_username="new-user",
    )

    assert runtime.events == [
        "backup",
        "xray_test",
        "install",
        "reload:1",
        "restore",
        "reload:2",
        "health",
    ]
    assert runtime.reload_calls == 2
    assert disabled == ["new-user"]
    assert len(alerts) == 1
    assert audits == [
        "backup_created",
        "apply_mutation_failed",
        "backup_restored",
        "rollback_reload",
        "rollback_reverified",
        "new_user_disabled",
        "alert_sent",
    ]
    assert runtime._current == runtime._backup
    assert raised.__context__ is original_error


def test_main_post_reload_health_exception_triggers_full_rollback_and_fails_closed() -> None:
    """The primary post-reload `health()` call (distinct from rollback's own
    `health()` call) raising must also be treated as a pre-health mutating
    exception: install() and reload() have already mutated state, and a
    crashing health probe leaves that state unconfirmed. This must trigger
    the same fail-closed rollback path as install()/reload() raising or a
    health() result of False, never a bare propagated exception."""
    original_error = RuntimeError("health probe crashed")
    runtime = Runtime(health=[original_error, True])
    disabled, alerts, audits, raised = _apply_with_recorders(
        runtime,
        config(users=("old-user", "new-user")),
        new_username="new-user",
    )

    assert runtime.events == [
        "backup",
        "xray_test",
        "install",
        "reload:1",
        "health",
        "restore",
        "reload:2",
        "health",
    ]
    assert runtime.reload_calls == 2
    assert disabled == ["new-user"]
    assert len(alerts) == 1
    assert audits == [
        "backup_created",
        "reload",
        "apply_mutation_failed",
        "backup_restored",
        "rollback_reload",
        "rollback_reverified",
        "new_user_disabled",
        "alert_sent",
    ]
    assert runtime._current == runtime._backup
    assert raised.__context__ is original_error


def test_rollback_restore_exception_fails_closed_without_claiming_recovery() -> None:
    """If `restore()` itself raises during rollback, the system state is
    unknown: this must never be reported as a successful rollback, must
    never reach `rollback_reverified`, and must still disable the new user
    and alert."""
    original_error = RuntimeError("disk unavailable")
    runtime = Runtime(health=[False], restore_error=original_error)
    disabled, alerts, audits, raised = _apply_with_recorders(
        runtime,
        config(users=("old-user", "new-user")),
        new_username="new-user",
    )

    assert runtime.events == [
        "backup",
        "xray_test",
        "install",
        "reload:1",
        "health",
        "restore",
    ]
    # restore() raised: no second reload, no rollback health check was ever
    # attempted, and rollback must never be recorded as verified.
    assert runtime.reload_calls == 1
    assert "rollback_reload" not in audits
    assert "rollback_reverified" not in audits
    assert disabled == ["new-user"]
    assert len(alerts) == 1
    assert audits == [
        "backup_created",
        "reload",
        "rollback_restore_failed",
        "new_user_disabled",
        "alert_sent",
    ]
    assert raised.__cause__ is original_error
    assert "unknown" in str(raised).lower()


def test_rollback_second_reload_exception_fails_closed_without_claiming_recovery() -> None:
    """If the rollback's own `reload()` raises after a successful
    `restore()`, the runtime state is unknown: must never claim recovery,
    must never reach `rollback_reverified`, and must still disable the new
    user and alert."""
    original_error = RuntimeError("systemctl reload failed")
    runtime = Runtime(health=[False], reload_errors={2: original_error})
    disabled, alerts, audits, raised = _apply_with_recorders(
        runtime,
        config(users=("old-user", "new-user")),
        new_username="new-user",
    )

    assert runtime.events == [
        "backup",
        "xray_test",
        "install",
        "reload:1",
        "health",
        "restore",
        "reload:2",
    ]
    assert runtime.reload_calls == 2
    assert "rollback_reverified" not in audits
    assert disabled == ["new-user"]
    assert len(alerts) == 1
    assert audits == [
        "backup_created",
        "reload",
        "backup_restored",
        "rollback_reload_failed",
        "new_user_disabled",
        "alert_sent",
    ]
    assert raised.__cause__ is original_error
    assert "unknown" in str(raised).lower()


def test_rollback_health_check_exception_fails_closed_without_claiming_recovery() -> None:
    """If the rollback's own `health()` call raises (rather than returning
    an unhealthy result), that must also fail closed without claiming
    `rollback_reverified`."""
    original_error = RuntimeError("health probe crashed")
    runtime = Runtime(health=[False, original_error])
    disabled, alerts, audits, raised = _apply_with_recorders(
        runtime,
        config(users=("old-user", "new-user")),
        new_username="new-user",
    )

    assert runtime.events == [
        "backup",
        "xray_test",
        "install",
        "reload:1",
        "health",
        "restore",
        "reload:2",
        "health",
    ]
    assert "rollback_reverified" not in audits
    assert disabled == ["new-user"]
    assert len(alerts) == 1
    assert audits == [
        "backup_created",
        "reload",
        "backup_restored",
        "rollback_reload",
        "rollback_health_check_failed",
        "new_user_disabled",
        "alert_sent",
    ]
    assert raised.__cause__ is original_error
    assert "unknown" in str(raised).lower()


def test_rollback_reload_succeeds_but_rollback_health_unhealthy_fails_closed() -> None:
    """Copying the backup back to disk is not the same as a confirmed-safe
    runtime: if rollback's own health check reports unhealthy, that must
    still fail closed, still disable the new user, still alert -- and must
    not be misreported as a healthy `rollback_reverified`."""
    runtime = Runtime(health=[False, False])
    disabled, alerts, audits, raised = _apply_with_recorders(
        runtime,
        config(users=("old-user", "new-user")),
        new_username="new-user",
    )

    assert runtime.events == [
        "backup",
        "xray_test",
        "install",
        "reload:1",
        "health",
        "restore",
        "reload:2",
        "health",
    ]
    assert runtime.reload_calls == 2
    assert disabled == ["new-user"]
    assert len(alerts) == 1
    assert audits == [
        "backup_created",
        "reload",
        "backup_restored",
        "rollback_reload",
        "rollback_reverified",
        "new_user_disabled",
        "alert_sent",
    ]
    assert "unhealthy" in str(raised).lower()


def test_preservation_rejects_missing_existing_user_and_outbound() -> None:
    runtime = Runtime()
    candidate = config(users=())
    candidate["outbounds"] = [{"tag": "BLOCK", "protocol": "blackhole"}]
    result = XrayFileProvider(runtime).validate(CandidateConfig(candidate, "v3"))
    assert not result.valid
    assert any("missing existing users" in error for error in result.errors)
    assert any("missing existing outbounds" in error for error in result.errors)


def _apply_rejection(
    candidate: dict[str, object],
    *,
    new_username: str | None = None,
    match: str = "",
) -> Runtime:
    runtime = Runtime()
    with pytest.raises(XrayValidationError, match=match):
        XrayFileProvider(runtime).apply(
            CandidateConfig(candidate, "new-preservation-check"),
            new_username=new_username,
        )
    assert runtime.reload_calls == 0
    return runtime


def test_missing_inbound_client_is_rejected_before_reload() -> None:
    runtime = Runtime()
    runtime._current = {
        **config(),
        "inbounds": [{"settings": {"clients": [{"id": "old-client"}]}}],
    }
    candidate = dict(runtime._current)
    candidate["inbounds"] = [{"settings": {"clients": []}}]

    with pytest.raises(XrayValidationError, match="inbound clients"):
        XrayFileProvider(runtime).apply(CandidateConfig(candidate, "missing-client"))

    assert runtime.reload_calls == 0


def test_missing_complete_old_route_is_rejected_before_reload() -> None:
    runtime = Runtime()
    candidate = config()
    candidate["outbounds"] = [
        {"tag": "BLOCK", "protocol": "blackhole"},
        {"tag": "egress-1", "protocol": "socks"},
        {"tag": "egress-2", "protocol": "socks"},
    ]
    candidate["routing"] = {
        "rules": [
            {"type": "field", "ip": ["geoip:private"], "outboundTag": "BLOCK"},
            {"type": "field", "user": ["old-user"], "outboundTag": "egress-2"},
            {"type": "field", "network": "tcp,udp", "outboundTag": "BLOCK"},
        ]
    }

    runtime = _apply_rejection(candidate, match="existing routing rules")
    assert runtime.reload_calls == 0


def test_missing_candidate_outbound_is_rejected_before_reload() -> None:
    candidate = config()
    candidate["routing"] = {
        "rules": [
            {"type": "field", "ip": ["geoip:private"], "outboundTag": "BLOCK"},
            {"type": "field", "user": ["old-user"], "outboundTag": "egress-1"},
            {"type": "field", "user": ["new-user"], "outboundTag": "missing-egress"},
            {"type": "field", "network": "tcp,udp", "outboundTag": "BLOCK"},
        ]
    }

    runtime = _apply_rejection(candidate, match="missing outbounds")
    assert runtime.reload_calls == 0


def test_direct_fallback_is_rejected_before_reload() -> None:
    candidate = config()
    candidate["outbounds"] = [
        {"tag": "BLOCK", "protocol": "blackhole"},
        {"tag": "egress-1", "protocol": "socks"},
        {"tag": "DIRECT", "protocol": "freedom"},
    ]
    candidate["routing"] = {
        "rules": [
            {"type": "field", "ip": ["geoip:private"], "outboundTag": "BLOCK"},
            {"type": "field", "user": ["old-user"], "outboundTag": "egress-1"},
            {"type": "field", "domain": ["example.com"], "outboundTag": "DIRECT"},
            {"type": "field", "network": "tcp,udp", "outboundTag": "BLOCK"},
        ]
    }

    runtime = _apply_rejection(candidate, match="DIRECT fallback")
    assert runtime.reload_calls == 0


def test_missing_new_user_route_is_rejected_before_reload() -> None:
    runtime = _apply_rejection(
        config(), new_username="new-user", match="new user route"
    )
    assert runtime.reload_calls == 0


def test_backend_dockerfile_copies_and_checks_all_xray_assets() -> None:
    dockerfile = Path("backend/Dockerfile").read_text(encoding="utf-8")
    for path in (
        "/usr/local/bin/xray",
        "/usr/local/share/xray/geoip.dat",
        "/usr/local/share/xray/geosite.dat",
    ):
        assert f"COPY --from=xray {path} {path}" in dockerfile
        assert path in dockerfile
    assert "XRAY_LOCATION_ASSET=/usr/local/share/xray" in dockerfile
    assert "xray version" in dockerfile
