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
    def __init__(self, *, xray_valid: bool = True, health: list[bool] | None = None) -> None:
        self.xray_valid = xray_valid
        self.health_values = health or [True]
        self.events: list[str] = []
        self.reload_calls = 0
        self._current: Mapping[str, object] = config()
        self._backup: Mapping[str, object] = self._current

    def backup(self) -> object:
        self.events.append("backup")
        self._backup = self._current
        return Path("backup.json")

    def current(self) -> Mapping[str, object]:
        return self._current

    def xray_test(self, content: Mapping[str, object]) -> bool:
        self.events.append("xray_test")
        return self.xray_valid

    def install(self, content: Mapping[str, object]) -> None:
        self.events.append("install")
        self._current = content

    def reload(self) -> None:
        self.reload_calls += 1
        self.events.append(f"reload:{self.reload_calls}")

    def health(self) -> HealthReport:
        self.events.append("health")
        value = self.health_values.pop(0)
        return HealthReport(value)

    def restore(self, backup: object) -> None:
        self.events.append("restore")
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
    provider = XrayFileProvider(
        runtime,
        disable_user=disabled.append,
        alert=alerts.append,
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
    assert audits[-5:] == [
        "backup_restored",
        "rollback_reload",
        "rollback_reverified",
        "new_user_disabled",
        "alert_sent",
    ]


def test_preservation_rejects_missing_existing_user_and_outbound() -> None:
    runtime = Runtime()
    candidate = config(users=())
    candidate["outbounds"] = [{"tag": "BLOCK", "protocol": "blackhole"}]
    result = XrayFileProvider(runtime).validate(CandidateConfig(candidate, "v3"))
    assert not result.valid
    assert any("missing existing users" in error for error in result.errors)
    assert any("missing existing outbounds" in error for error in result.errors)


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
