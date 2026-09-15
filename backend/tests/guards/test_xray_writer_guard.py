import json
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
from backend.app.providers.gateway.xray_baseline import (
    XrayAppliedStateStore,
    XrayBaselineError,
)
from backend.app.providers.gateway.xray_composition import (
    XrayDeploymentConfig,
    XrayFullConfigInput,
    XrayRealityConfig,
    XrayStaticSkeleton,
    compose_xray_config,
    repo_owned_xray_fingerprint,
)
from backend.app.providers.gateway.xray_file import (
    XrayFileProvider,
    XrayReloadError,
    XrayValidationError,
)
from ops.gateway.render_xray_routes import XrayRenderError, write_rendered_config


def config(
    *,
    routes: tuple[tuple[str, str], ...] = (("route-a", "egress-a"),),
) -> dict[str, object]:
    tags = tuple(dict.fromkeys(tag for _, tag in routes))
    desired = DesiredRoutingState(
        user_routes=dict(routes),
        outbounds=tuple(
            XrayOutboundDTO(tag, f"{tag}.example.invalid", 1080, "socks", f"secret/{tag}")
            for tag in tags
        ),
    )
    credentials = {
        f"secret/{tag}": CredentialDTO(f"user-{tag}", f"password-{tag}") for tag in tags
    }
    return deepcopy(
        dict(
            compose_xray_config(
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
            ).content
        )
    )


class Runtime:
    def __init__(
        self,
        current: Mapping[str, object] | None,
        *,
        health: list[bool | Exception] | None = None,
        install_error: Exception | None = None,
        restore_error: Exception | None = None,
        reload_errors: dict[int, Exception] | None = None,
        mutate_after_install: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self._exists = current is not None
        self._current = deepcopy(dict(current)) if current is not None else None
        self._backup: dict[str, object] | None = None
        self.health_values = health or [True]
        self.install_error = install_error
        self.restore_error = restore_error
        self.reload_errors = reload_errors or {}
        self.mutate_after_install = mutate_after_install
        self.events: list[str] = []
        self.reload_calls = 0
        self.install_calls = 0

    def backup(self) -> object:
        self.events.append("backup")
        self._backup = deepcopy(self._current)
        return deepcopy(self._backup)

    def config_exists(self) -> bool:
        return self._exists

    def current(self) -> Mapping[str, object]:
        self.events.append("current")
        if not self._exists or self._current is None:
            raise XrayValidationError("current Xray config is unavailable")
        return deepcopy(self._current)

    def xray_test(self, _content: Mapping[str, object]) -> bool:
        self.events.append("xray_test")
        return True

    def install(self, content: Mapping[str, object]) -> None:
        self.events.append("install")
        self.install_calls += 1
        if self.install_error is not None:
            error, self.install_error = self.install_error, None
            raise error
        self._exists = True
        self._current = deepcopy(dict(content))
        if self.mutate_after_install is not None:
            self.mutate_after_install(self._current)

    def reload(self) -> None:
        self.reload_calls += 1
        self.events.append(f"reload:{self.reload_calls}")
        if self.reload_calls in self.reload_errors:
            raise self.reload_errors[self.reload_calls]

    def health(self) -> HealthReport:
        self.events.append("health")
        value = self.health_values.pop(0)
        if isinstance(value, Exception):
            raise value
        return HealthReport(value)

    def restore(self, backup: object) -> None:
        self.events.append("restore")
        if self.restore_error is not None:
            error, self.restore_error = self.restore_error, None
            raise error
        if backup is None:
            self._exists = False
            self._current = None
        else:
            self._exists = True
            self._current = deepcopy(cast(dict[str, object], backup))


def make_provider(runtime: Runtime, path: Path) -> XrayFileProvider:
    return XrayFileProvider(runtime, baseline_store=XrayAppliedStateStore(path))


def seed_baseline(path: Path, value: Mapping[str, object]) -> str:
    store = XrayAppliedStateStore(path)
    store.save(value)
    return cast(str, store.load())


def test_normal_business_change_advances_baseline(tmp_path: Path) -> None:
    path = tmp_path / "xray_config.last_applied.json"
    initial = config()
    old_digest = seed_baseline(path, initial)
    runtime = Runtime(initial)
    candidate = config(routes=(("route-a", "egress-b"),))

    result = make_provider(runtime, path).apply(CandidateConfig(candidate, "B"))

    assert result.applied
    assert old_digest != XrayAppliedStateStore(path).load()
    assert XrayAppliedStateStore(path).load() == repo_owned_xray_fingerprint(candidate)
    assert runtime.events == [
        "backup",
        "current",
        "xray_test",
        "install",
        "reload:1",
        "health",
        "current",
    ]


def test_drift_a_b_c_fails_before_install_and_preserves_baseline(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    initial = config()
    seed_baseline(path, initial)
    disk = config(routes=(("route-a", "egress-b"),))
    desired = config(routes=(("route-a", "egress-c"),))
    runtime = Runtime(disk)

    with pytest.raises(XrayValidationError, match="drift"):
        make_provider(runtime, path).apply(CandidateConfig(desired, "C"))

    assert runtime.install_calls == 0
    assert runtime.reload_calls == 0
    assert XrayAppliedStateStore(path).load() == repo_owned_xray_fingerprint(initial)
    assert runtime.events == ["backup", "current"]


def test_no_change_apply_is_allowed(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    value = config()
    seed_baseline(path, value)
    runtime = Runtime(value)

    assert make_provider(runtime, path).apply(CandidateConfig(value, "A")).applied
    assert runtime.reload_calls == 1


def test_missing_baseline_with_existing_file_requires_explicit_bootstrap(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    runtime = Runtime(config())

    with pytest.raises(XrayValidationError, match="explicit bootstrap"):
        make_provider(runtime, path).apply(CandidateConfig(config(), "A"))

    assert runtime.install_calls == 0
    assert runtime.reload_calls == 0


def test_missing_baseline_and_missing_file_allows_first_apply(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    runtime = Runtime(None)
    candidate = config()

    assert make_provider(runtime, path).apply(CandidateConfig(candidate, "first")).applied
    assert runtime.config_exists()
    assert XrayAppliedStateStore(path).load() == repo_owned_xray_fingerprint(candidate)


@pytest.mark.parametrize(
    "baseline_text",
    [
        "not json",
        '{"schema_version": 2, "projection_version": "xray-full-v1", "sha256": "' + "0" * 64 + '"}',
        '{"schema_version": 1, "projection_version": "xray-full-v1", "sha256": "bad"}',
        '{"schema_version": 1, "projection_version": "xray-full-v1", "sha256": "'
        + "0" * 64
        + '", "extra": 1}',
    ],
)
def test_malformed_baseline_fails_closed(tmp_path: Path, baseline_text: str) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(baseline_text, encoding="utf-8")
    runtime = Runtime(config())

    with pytest.raises(XrayValidationError, match="baseline"):
        make_provider(runtime, path).apply(CandidateConfig(config(), "A"))

    assert runtime.install_calls == 0
    assert runtime.reload_calls == 0


def test_malformed_disk_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    seed_baseline(path, config())
    runtime = Runtime(config())
    runtime._current = None
    runtime._exists = True

    with pytest.raises(XrayValidationError, match="projected safely"):
        make_provider(runtime, path).apply(CandidateConfig(config(), "A"))
    assert runtime.install_calls == 0


def test_mapping_key_order_does_not_drift(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    value = config()
    seed_baseline(path, value)
    reordered = {key: value[key] for key in reversed(tuple(value))}
    runtime = Runtime(reordered)

    assert make_provider(runtime, path).apply(CandidateConfig(value, "A")).applied


@pytest.mark.parametrize("field", ["route", "outbound", "reality", "clients", "unknown"])
def test_repo_owned_field_drift_is_blocked(tmp_path: Path, field: str) -> None:
    path = tmp_path / "baseline.json"
    value = config()
    seed_baseline(path, value)
    drifted = deepcopy(value)
    if field == "route":
        cast(list[dict[str, object]], cast(dict[str, object], drifted["routing"])["rules"])[1][
            "user"
        ] = ["other"]
    elif field == "outbound":
        cast(list[dict[str, object]], drifted["outbounds"])[1]["tag"] = "other"
    elif field == "reality":
        reality = cast(
            dict[str, object],
            cast(
                dict[str, object],
                cast(list[dict[str, object]], drifted["inbounds"])[0]["streamSettings"],
            )["realitySettings"],
        )
        reality["dest"] = "other.example:443"
    elif field == "clients":
        cast(dict[str, object], cast(list[dict[str, object]], drifted["inbounds"])[0]["settings"])[
            "clients"
        ] = [{"id": "runtime-client"}]
    else:
        cast(dict[str, object], drifted["log"])["unknown"] = True
    runtime = Runtime(drifted)

    with pytest.raises(XrayValidationError, match="config|projected"):
        make_provider(runtime, path).apply(CandidateConfig(value, "A"))
    assert runtime.install_calls == 0


def test_install_failure_does_not_advance_baseline(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    initial = config()
    seed_baseline(path, initial)
    runtime = Runtime(initial, install_error=RuntimeError("install failed"))

    with pytest.raises(XrayReloadError):
        make_provider(runtime, path).apply(
            CandidateConfig(config(routes=(("route-a", "egress-b"),)), "B")
        )
    assert XrayAppliedStateStore(path).load() == repo_owned_xray_fingerprint(initial)


def test_first_reload_failure_does_not_advance_baseline(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    initial = config()
    seed_baseline(path, initial)
    runtime = Runtime(initial, reload_errors={1: RuntimeError("reload failed")})

    with pytest.raises(XrayReloadError):
        make_provider(runtime, path).apply(
            CandidateConfig(config(routes=(("route-a", "egress-b"),)), "B")
        )
    assert runtime._current == initial
    assert XrayAppliedStateStore(path).load() == repo_owned_xray_fingerprint(initial)


def test_rollback_failure_does_not_advance_baseline(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    initial = config()
    seed_baseline(path, initial)
    runtime = Runtime(initial, health=[False], restore_error=RuntimeError("restore failed"))

    with pytest.raises(XrayReloadError, match="unknown"):
        make_provider(runtime, path).apply(
            CandidateConfig(config(routes=(("route-a", "egress-b"),)), "B")
        )
    assert XrayAppliedStateStore(path).load() == repo_owned_xray_fingerprint(initial)


def test_post_reload_mismatch_does_not_advance_baseline(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    initial = config()
    seed_baseline(path, initial)

    def external_writer(current: dict[str, object]) -> None:
        cast(dict[str, object], current["log"])["loglevel"] = "error"

    runtime = Runtime(initial, health=[True, True], mutate_after_install=external_writer)
    candidate = config(routes=(("route-a", "egress-b"),))

    with pytest.raises(XrayReloadError):
        make_provider(runtime, path).apply(CandidateConfig(candidate, "B"))
    assert XrayAppliedStateStore(path).load() == repo_owned_xray_fingerprint(initial)


class FailingStore(XrayAppliedStateStore):
    def save(self, _config: Mapping[str, object]) -> None:
        raise XrayBaselineError("simulated baseline persistence failure")


def test_baseline_persistence_failure_rolls_runtime_back_and_keeps_old_baseline(
    tmp_path: Path,
) -> None:
    path = tmp_path / "baseline.json"
    initial = config()
    seed_baseline(path, initial)
    runtime = Runtime(initial, health=[True, True])
    candidate = config(routes=(("route-a", "egress-b"),))

    with pytest.raises(XrayReloadError, match="baseline persistence"):
        XrayFileProvider(runtime, baseline_store=FailingStore(path)).apply(
            CandidateConfig(candidate, "B")
        )
    assert runtime._current == initial
    assert XrayAppliedStateStore(path).load() == repo_owned_xray_fingerprint(initial)


def test_external_writer_simulation_is_caught_before_install(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    initial = config()
    seed_baseline(path, initial)
    runtime = Runtime(initial)
    # Simulate Marzban PUT /api/core/config without using the repo provider.
    runtime._current = config(routes=(("route-a", "egress-b"),))
    desired = config(routes=(("route-a", "egress-c"),))

    with pytest.raises(XrayValidationError, match="drift"):
        make_provider(runtime, path).apply(CandidateConfig(desired, "C"))
    assert runtime.install_calls == 0
    assert runtime.reload_calls == 0
    assert XrayAppliedStateStore(path).load() == repo_owned_xray_fingerprint(initial)


def test_baseline_contains_only_non_secret_metadata(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    value = config()
    XrayAppliedStateStore(path).save(value)
    text = path.read_text(encoding="utf-8")

    assert set(json.loads(text)) == {"schema_version", "projection_version", "sha256"}
    assert "private-key" not in text
    assert "short-id" not in text
    assert "password-egress-a" not in text


def test_standalone_writer_uses_the_same_guard_and_advances_baseline(tmp_path: Path) -> None:
    output = tmp_path / "xray_config.json"
    baseline = tmp_path / "xray_config.last_applied.json"
    initial = config()

    write_rendered_config(initial, output_config=output, baseline_path=baseline)
    candidate = config(routes=(("route-a", "egress-b"),))
    write_rendered_config(candidate, output_config=output, baseline_path=baseline)

    assert json.loads(output.read_text(encoding="utf-8")) == candidate
    assert XrayAppliedStateStore(baseline).load() == repo_owned_xray_fingerprint(candidate)


def test_standalone_writer_blocks_external_writer_before_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "xray_config.json"
    baseline = tmp_path / "xray_config.last_applied.json"
    initial = config()
    seed_baseline(baseline, initial)
    external = config(routes=(("route-a", "egress-b"),))
    output.write_text(json.dumps(external), encoding="utf-8")
    desired = config(routes=(("route-a", "egress-c"),))

    with pytest.raises(XrayRenderError, match="writer guard"):
        write_rendered_config(desired, output_config=output, baseline_path=baseline)

    assert json.loads(output.read_text(encoding="utf-8")) == external
    assert XrayAppliedStateStore(baseline).load() == repo_owned_xray_fingerprint(initial)


def test_standalone_baseline_persistence_failure_restores_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "xray_config.json"
    baseline = tmp_path / "xray_config.last_applied.json"
    initial = config()
    seed_baseline(baseline, initial)
    output.write_text(json.dumps(initial), encoding="utf-8")

    def fail_save(_self: XrayAppliedStateStore, _value: Mapping[str, object]) -> None:
        raise XrayBaselineError("simulated persistence failure")

    monkeypatch.setattr(XrayAppliedStateStore, "save", fail_save)
    candidate = config(routes=(("route-a", "egress-b"),))

    with pytest.raises(XrayRenderError, match="rolled back"):
        write_rendered_config(candidate, output_config=output, baseline_path=baseline)

    assert json.loads(output.read_text(encoding="utf-8")) == initial
