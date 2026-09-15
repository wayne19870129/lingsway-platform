from collections.abc import Callable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest

from backend.app.providers.base import (
    CandidateConfig,
    CredentialDTO,
    DesiredRoutingState,
    HealthReport,
    XrayOutboundDTO,
)
from backend.app.providers.gateway.xray_composition import (
    XrayDeploymentConfig,
    XrayFullConfigInput,
    XrayRealityConfig,
    XrayStaticSkeleton,
    compose_xray_config,
)
from backend.app.providers.gateway.xray_file import (
    XrayFileProvider,
    XrayReloadError,
    XrayValidationError,
)


def config(
    *,
    routes: tuple[tuple[str, str], ...] = (("old-user", "egress-1"),),
) -> dict[str, object]:
    tags = tuple(dict.fromkeys(tag for _, tag in routes))
    desired = DesiredRoutingState(
        user_routes=dict(routes),
        outbounds=tuple(
            XrayOutboundDTO(
                tag,
                f"{tag}.example.invalid",
                1080,
                "socks",
                f"secret/{tag}",
            )
            for tag in tags
        ),
    )
    credentials = {
        f"secret/{tag}": CredentialDTO(f"user-{tag}", f"password-{tag}") for tag in tags
    }
    candidate = compose_xray_config(
        XrayFullConfigInput(
            XrayStaticSkeleton.canonical(),
            XrayDeploymentConfig(
                xray_log_level="warning",
                reality_dest="reality.example.invalid:443",
                reality_server_names=("reality.example.invalid",),
            ),
            XrayRealityConfig("private-key", ("short-id",)),
            desired,
            credentials,
        )
    )
    return deepcopy(dict(candidate.content))


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
        forbid_current: bool = False,
    ) -> None:
        self.xray_valid = xray_valid
        self.health_values: list[bool | Exception] = health or [True]
        self.install_error = install_error
        self.reload_errors = reload_errors or {}
        self.restore_error = restore_error
        self.forbid_current = forbid_current
        self.current_calls = 0
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
        self.current_calls += 1
        if self.forbid_current:
            raise AssertionError("validate() must not inspect runtime.current()")
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
        self._current = deepcopy(dict(content))

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
    assert runtime.events == ["backup"]


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
            CandidateConfig(
                config(routes=(("old-user", "egress-1"), ("new-user", "egress-1"))),
                "v2",
            ),
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
        config(routes=(("old-user", "egress-1"), ("new-user", "egress-1"))),
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
        config(routes=(("old-user", "egress-1"), ("new-user", "egress-1"))),
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
        config(routes=(("old-user", "egress-1"), ("new-user", "egress-1"))),
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
        config(routes=(("old-user", "egress-1"), ("new-user", "egress-1"))),
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
        config(routes=(("old-user", "egress-1"), ("new-user", "egress-1"))),
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
        config(routes=(("old-user", "egress-1"), ("new-user", "egress-1"))),
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
        config(routes=(("old-user", "egress-1"), ("new-user", "egress-1"))),
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


def test_validate_accepts_legal_route_and_outbound_deletion_without_current_read() -> None:
    runtime = Runtime(
        forbid_current=True,
    )
    candidate = config(routes=(("route-a", "egress-a"),))

    result = XrayFileProvider(runtime).validate(CandidateConfig(candidate, "v3"))

    assert result.valid
    assert runtime.current_calls == 0
    assert runtime.events == ["xray_test"]


def test_legal_deletion_is_installed_and_passes_exact_post_reload_check() -> None:
    runtime = Runtime()
    candidate = config(routes=(("route-a", "egress-a"),))

    result = XrayFileProvider(runtime).apply(CandidateConfig(candidate, "legal-delete"))

    assert result.applied
    current = cast(dict[str, object], runtime._current)
    outbounds = cast(list[dict[str, object]], current["outbounds"])
    assert {item["tag"] for item in outbounds} == {"BLOCK", "egress-a"}
    routing = cast(dict[str, object], current["routing"])
    rules = cast(list[dict[str, object]], routing["rules"])
    assert all(rule.get("outboundTag") != "egress-b" for rule in rules)
    assert all(rule.get("user") != ["route-b"] for rule in rules)


class InstalledMutatingRuntime(Runtime):
    def __init__(
        self,
        initial: dict[str, object],
        mutate_after_install: Callable[[dict[str, object]], None],
    ) -> None:
        super().__init__(health=[True, True])
        self._current = initial
        self._mutate_after_install = mutate_after_install

    def install(self, content: Mapping[str, object]) -> None:
        super().install(content)
        self._mutate_after_install(cast(dict[str, object], self._current))


def _assert_post_reload_mismatch_rolls_back(
    initial: dict[str, object],
    candidate: dict[str, object],
    mutate_after_install: Callable[[dict[str, object]], None],
) -> Runtime:
    runtime = InstalledMutatingRuntime(initial, mutate_after_install)
    audits: list[str] = []
    provider = XrayFileProvider(
        runtime,
        audit=lambda event, _details: audits.append(event),
    )

    with pytest.raises(XrayReloadError):
        provider.apply(CandidateConfig(candidate, "projection-mismatch"))

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
    assert runtime._current == runtime._backup
    assert "post_reload_projection_failed" in audits
    return runtime


def test_stale_extra_route_and_outbound_fail_exact_check_and_roll_back() -> None:
    def retain_deleted_state(current: dict[str, object]) -> None:
        current.clear()
        current.update(config(routes=(("route-a", "egress-a"), ("route-b", "egress-b"))))

    runtime = _assert_post_reload_mismatch_rolls_back(
        config(routes=(("route-a", "egress-a"), ("route-b", "egress-b"))),
        config(routes=(("route-a", "egress-a"),)),
        retain_deleted_state,
    )
    assert runtime.reload_calls == 2


def test_missing_candidate_owned_state_fails_exact_check_and_rolls_back() -> None:
    def drop_candidate_state(current: dict[str, object]) -> None:
        current.clear()
        current.update(config(routes=(("route-a", "egress-a"),)))

    runtime = _assert_post_reload_mismatch_rolls_back(
        config(routes=(("route-a", "egress-a"), ("route-b", "egress-b"))),
        config(routes=(("route-a", "egress-a"), ("route-b", "egress-b"))),
        drop_candidate_state,
    )
    assert runtime.reload_calls == 2


def test_post_reload_current_read_failure_rolls_back_closed() -> None:
    runtime = Runtime(health=[True, True], forbid_current=True)
    candidate = config()

    with pytest.raises(XrayReloadError):
        XrayFileProvider(runtime).apply(CandidateConfig(candidate, "current-read-failure"))

    assert runtime.current_calls == 1
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


def _mutate_projection_field(field: str) -> Callable[[dict[str, object]], None]:
    def mutate(config_value: dict[str, object]) -> None:
        inbound = cast(list[dict[str, object]], config_value["inbounds"])[0]
        settings = cast(dict[str, object], inbound["settings"])
        stream = cast(dict[str, object], inbound["streamSettings"])
        reality = cast(dict[str, object], stream["realitySettings"])
        if field == "protocol":
            inbound["protocol"] = "tcp"
        elif field == "listen":
            inbound["listen"] = "127.0.0.1"
        elif field == "port":
            inbound["port"] = 9443
        elif field == "loglevel":
            cast(dict[str, object], config_value["log"])["loglevel"] = "error"
        elif field == "dest":
            reality["dest"] = "other.example.invalid:443"
        elif field == "serverNames":
            reality["serverNames"] = ["other.example.invalid"]
        elif field == "privateKey":
            reality["privateKey"] = "drifted-private-key"
        elif field == "shortIds":
            reality["shortIds"] = ["drifted-short-id"]
        elif field == "tag":
            inbound["tag"] = "drifted-tag"
        elif field == "decryption":
            settings["decryption"] = "none-but-different"
        elif field == "network":
            stream["network"] = "ws"
        elif field == "security":
            stream["security"] = "tls"
        elif field == "show":
            reality["show"] = True
        elif field == "xver":
            reality["xver"] = 1
        elif field == "domainStrategy":
            cast(dict[str, object], config_value["routing"])["domainStrategy"] = "IPIfNonMatch"
        elif field == "clients":
            settings["clients"] = [{"id": "runtime-client"}]
        elif field == "unknown":
            cast(dict[str, object], config_value["log"])["unknown"] = True
        else:
            raise AssertionError(f"unhandled projection field: {field}")

    return mutate


@pytest.mark.parametrize(
    "field",
    [
        "protocol",
        "listen",
        "port",
        "loglevel",
        "dest",
        "serverNames",
        "privateKey",
        "shortIds",
        "tag",
        "decryption",
        "network",
        "security",
        "show",
        "xver",
        "domainStrategy",
        "clients",
        "unknown",
    ],
)
def test_full_file_projection_drift_fails_closed_and_rolls_back(field: str) -> None:
    candidate = config(routes=(("route-a", "egress-a"),))
    runtime = _assert_post_reload_mismatch_rolls_back(
        deepcopy(candidate), candidate, _mutate_projection_field(field)
    )
    assert runtime.reload_calls == 2


def _mutate_invalid_candidate(field: str) -> dict[str, object]:
    candidate = config(routes=(("route-a", "egress-a"), ("route-b", "egress-b")))
    inbound = cast(list[dict[str, object]], candidate["inbounds"])[0]
    settings = cast(dict[str, object], inbound["settings"])
    routing = cast(dict[str, object], candidate["routing"])
    rules = cast(list[dict[str, object]], routing["rules"])
    outbounds = cast(list[dict[str, object]], candidate["outbounds"])
    outbound_settings = cast(dict[str, object], outbounds[1]["settings"])
    servers = cast(list[dict[str, object]], outbound_settings["servers"])
    server = servers[0]
    if field == "unknown-top-level":
        candidate["unexpected"] = True
    elif field == "malformed-log":
        candidate["log"] = {"level": "warning"}
    elif field == "missing-inbound-protocol":
        del inbound["protocol"]
    elif field == "invalid-clients":
        settings["clients"] = [{"id": "runtime-client"}]
    elif field == "duplicate-outbound":
        outbounds.append(deepcopy(outbounds[1]))
    elif field == "missing-route-outbound":
        rules.insert(1, {"type": "field", "user": ["route-c"], "outboundTag": "missing"})
    elif field == "block-collision":
        outbounds[1]["tag"] = "BLOCK"
    elif field == "missing-block":
        del outbounds[0]
    elif field == "unsupported-outbound-protocol":
        outbounds[1]["protocol"] = "http"
    elif field == "invalid-host":
        server["address"] = ""
    elif field == "invalid-port":
        server["port"] = 0
    elif field == "invalid-principal":
        rules[1]["user"] = [""]
    elif field == "duplicate-principal":
        rules.insert(2, deepcopy(rules[1]))
    elif field == "direct-route":
        outbounds[1]["tag"] = "DIRECT"
    elif field == "private-block-order":
        rules[0], rules[1] = rules[1], rules[0]
    elif field == "fallback-order":
        rules[-1], rules[-2] = rules[-2], rules[-1]
    else:
        raise AssertionError(f"unhandled invalid candidate: {field}")
    return candidate


@pytest.mark.parametrize(
    "field",
    [
        "unknown-top-level",
        "malformed-log",
        "missing-inbound-protocol",
        "invalid-clients",
        "duplicate-outbound",
        "missing-route-outbound",
        "block-collision",
        "missing-block",
        "unsupported-outbound-protocol",
        "invalid-host",
        "invalid-port",
        "invalid-principal",
        "duplicate-principal",
        "direct-route",
        "private-block-order",
        "fallback-order",
    ],
)
def test_invalid_canonical_candidate_fails_closed_before_xray_test(field: str) -> None:
    candidate = _mutate_invalid_candidate(field)
    runtime = _apply_rejection(candidate)
    assert runtime.events == ["backup"]
    assert runtime.current_calls == 0


def _apply_rejection(
    candidate: dict[str, object],
    *,
    new_username: str | None = None,
    match: str | None = None,
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
    candidate = config()
    inbound = cast(list[dict[str, object]], candidate["inbounds"])[0]
    settings = cast(dict[str, object], inbound["settings"])
    settings["clients"] = [{"id": "old-client"}]

    with pytest.raises(XrayValidationError, match="clients skeleton"):
        XrayFileProvider(runtime).apply(CandidateConfig(candidate, "missing-client"))

    assert runtime.reload_calls == 0


def test_missing_complete_old_route_is_rejected_before_reload() -> None:
    candidate = config()
    routing = cast(dict[str, object], candidate["routing"])
    rules = cast(list[object], routing["rules"])
    rules.insert(0, {"unexpected": "route"})

    runtime = _apply_rejection(candidate, match="private BLOCK rule")
    assert runtime.reload_calls == 0


def test_missing_candidate_outbound_is_rejected_before_reload() -> None:
    candidate = config()
    routing = cast(dict[str, object], candidate["routing"])
    rules = cast(list[dict[str, object]], routing["rules"])
    rules.insert(1, {"type": "field", "user": ["new-user"], "outboundTag": "missing-egress"})

    runtime = _apply_rejection(candidate, match="missing outbound")
    assert runtime.reload_calls == 0


def test_direct_fallback_is_rejected_before_reload() -> None:
    candidate = config()
    outbounds = cast(list[dict[str, object]], candidate["outbounds"])
    outbounds[1]["tag"] = "DIRECT"

    runtime = _apply_rejection(candidate, match="DIRECT")
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
