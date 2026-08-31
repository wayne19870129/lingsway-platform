"""File-backed Xray provider enforcing the nine-step safe reload sequence."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from backend.app.providers.base import (
    ApplyResult,
    CandidateConfig,
    DesiredRoutingState,
    HealthReport,
    ValidationResult,
)


class XrayValidationError(RuntimeError):
    pass


class XrayReloadError(RuntimeError):
    pass


class XrayRuntime(Protocol):
    def backup(self) -> object: ...
    def current(self) -> Mapping[str, object]: ...
    def xray_test(self, content: Mapping[str, object]) -> bool: ...
    def install(self, content: Mapping[str, object]) -> None: ...
    def reload(self) -> None: ...
    def health(self) -> HealthReport: ...
    def restore(self, backup: object) -> None: ...


AuditCallback = Callable[[str, Mapping[str, object]], None]


class XrayFileProvider:
    def __init__(
        self,
        runtime: XrayRuntime,
        *,
        disable_user: Callable[[str], None] = lambda _username: None,
        alert: Callable[[str], None] = lambda _message: None,
        audit: AuditCallback = lambda _event, _details: None,
    ) -> None:
        self._runtime = runtime
        self._disable_user = disable_user
        self._alert = alert
        self._audit = audit

    def render(self, desired: DesiredRoutingState) -> CandidateConfig:
        rules: list[dict[str, object]] = [
            {"type": "field", "ip": ["geoip:private"], "outboundTag": "BLOCK"}
        ]
        rules.extend(
            {"type": "field", "user": [user], "outboundTag": outbound}
            for user, outbound in sorted(desired.user_routes.items())
        )
        rules.append(
            {"type": "field", "network": "tcp,udp", "outboundTag": "BLOCK"}
        )
        return CandidateConfig(
            content={
                "routing": {"rules": rules},
                "outbounds": [
                    {"tag": tag, "protocol": "blackhole" if tag == "BLOCK" else "socks"}
                    for tag in desired.outbound_tags
                ],
            },
            version=datetime.now(UTC).strftime("%Y%m%d%H%M%S%f"),
        )

    def validate(self, candidate: CandidateConfig) -> ValidationResult:
        errors: list[str] = []
        try:
            parsed = json.loads(json.dumps(candidate.content))
        except (TypeError, ValueError) as exc:
            return ValidationResult(False, (f"invalid JSON: {exc}",))
        if not self._runtime.xray_test(candidate.content):
            errors.append("xray run -test failed")
        errors.extend(_preservation_errors(self._runtime.current(), parsed))
        return ValidationResult(not errors, tuple(errors))

    def apply(self, candidate: CandidateConfig, *, new_username: str | None = None) -> ApplyResult:
        backup = self._runtime.backup()
        self._audit("backup_created", {"version": candidate.version})
        validation = self.validate(candidate)
        if not validation.valid:
            self._audit("candidate_rejected", {"errors": validation.errors})
            self._alert("Xray candidate rejected before reload")
            raise XrayValidationError("; ".join(validation.errors))

        self._runtime.install(candidate.content)
        self._runtime.reload()
        self._audit("reload", {"version": candidate.version})
        report = self._runtime.health()
        if report.healthy:
            self._audit("health_ok", report.details)
            return ApplyResult(True, candidate.version)

        self._runtime.restore(backup)
        self._audit("backup_restored", {})
        self._runtime.reload()
        self._audit("rollback_reload", {})
        rollback_health = self._runtime.health()
        self._audit("rollback_reverified", {"healthy": rollback_health.healthy})
        if new_username is not None:
            self._disable_user(new_username)
            self._audit("new_user_disabled", {"username": new_username})
        self._alert("Xray health check failed; backup restored")
        self._audit("alert_sent", {})
        raise XrayReloadError("post-reload health check failed")

    def health(self) -> HealthReport:
        return self._runtime.health()


def _preservation_errors(
    current: Mapping[str, object], candidate: Mapping[str, object]
) -> list[str]:
    errors: list[str] = []
    current_outbounds = _outbound_tags(current)
    candidate_outbounds = _outbound_tags(candidate)
    if missing := current_outbounds - candidate_outbounds:
        errors.append(f"missing existing outbounds: {sorted(missing)}")
    current_users = _routed_users(current)
    candidate_users = _routed_users(candidate)
    if missing := current_users - candidate_users:
        errors.append(f"missing existing users: {sorted(missing)}")
    rules = _rules(candidate)
    private = {"type": "field", "ip": ["geoip:private"], "outboundTag": "BLOCK"}
    if not rules or rules[0] != private:
        errors.append("private BLOCK rule must be first")
    fallback = {"type": "field", "network": "tcp,udp", "outboundTag": "BLOCK"}
    if not rules or rules[-1] != fallback:
        errors.append("tcp,udp BLOCK fallback must be last")
    return errors


def _rules(config: Mapping[str, object]) -> list[object]:
    routing = config.get("routing")
    if not isinstance(routing, Mapping):
        return []
    rules = routing.get("rules")
    return list(rules) if isinstance(rules, list) else []


def _outbound_tags(config: Mapping[str, object]) -> set[str]:
    outbounds = config.get("outbounds")
    if not isinstance(outbounds, list):
        return set()
    return {
        tag
        for item in outbounds
        if isinstance(item, Mapping) and isinstance((tag := item.get("tag")), str)
    }


def _routed_users(config: Mapping[str, object]) -> set[str]:
    users: set[str] = set()
    for rule in _rules(config):
        if isinstance(rule, Mapping) and isinstance(rule.get("user"), list):
            users.update(user for user in rule["user"] if isinstance(user, str))
    return users


@dataclass(slots=True)
class LocalXrayRuntime:
    config_path: Path
    backup_dir: Path
    xray_binary: Path = Path("/usr/local/bin/xray")
    asset_dir: Path = Path("/usr/local/share/xray")
    reload_command: tuple[str, ...] = ("systemctl", "reload", "xray")
    marzban_health: Callable[[], bool] = lambda: True

    def backup(self) -> object:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        destination = self.backup_dir / (
            f"xray_config.{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.json"
        )
        shutil.copy2(self.config_path, destination)
        return destination

    def current(self) -> Mapping[str, object]:
        value = json.loads(self.config_path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise XrayValidationError("current Xray config must be an object")
        return value

    def xray_test(self, content: Mapping[str, object]) -> bool:
        with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as fh:
            json.dump(content, fh)
            candidate_path = Path(fh.name)
        try:
            env = {**os.environ, "XRAY_LOCATION_ASSET": str(self.asset_dir)}
            result = subprocess.run(
                [str(self.xray_binary), "run", "-test", "-c", str(candidate_path)],
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            return result.returncode == 0
        finally:
            candidate_path.unlink(missing_ok=True)

    def install(self, content: Mapping[str, object]) -> None:
        temporary = self.config_path.with_suffix(".candidate.json")
        temporary.write_text(json.dumps(content, indent=2), encoding="utf-8")
        temporary.replace(self.config_path)

    def reload(self) -> None:
        subprocess.run(self.reload_command, check=True)

    def health(self) -> HealthReport:
        port_ok = False
        try:
            with socket.create_connection(("127.0.0.1", 8443), timeout=2):
                port_ok = True
        except OSError:
            pass
        marzban_ok = self.marzban_health()
        healthy = marzban_ok and port_ok
        return HealthReport(
            healthy,
            {"marzban": str(marzban_ok), "port_8443": str(port_ok)},
        )

    def restore(self, backup: object) -> None:
        if not isinstance(backup, Path):
            raise TypeError("backup must be a Path")
        shutil.copy2(backup, self.config_path)
