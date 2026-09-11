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
from typing import NoReturn, Protocol

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

    def validate(
        self, candidate: CandidateConfig, *, new_username: str | None = None
    ) -> ValidationResult:
        errors: list[str] = []
        try:
            parsed = json.loads(json.dumps(candidate.content))
        except (TypeError, ValueError) as exc:
            return ValidationResult(False, (f"invalid JSON: {exc}",))
        if not self._runtime.xray_test(candidate.content):
            errors.append("xray run -test failed")
        errors.extend(
            _preservation_errors(
                self._runtime.current(), parsed, new_username=new_username
            )
        )
        return ValidationResult(not errors, tuple(errors))

    def apply(self, candidate: CandidateConfig, *, new_username: str | None = None) -> ApplyResult:
        backup = self._runtime.backup()
        self._audit("backup_created", {"version": candidate.version})
        validation = self.validate(candidate, new_username=new_username)
        if not validation.valid:
            self._audit("candidate_rejected", {"errors": validation.errors})
            self._alert("Xray candidate rejected before reload")
            raise XrayValidationError("; ".join(validation.errors))

        # `install()`, this first `reload()`, and the immediately-following
        # `health()` call are the only steps that can mutate disk/runtime
        # state -- or fail to confirm it -- before any health signal is
        # available ("pre-health mutating exception" in ADR-015 Part A). Any
        # of the three raising must never leave a bare exception propagating
        # past this point: the candidate may already be on disk, the running
        # Xray process may or may not have picked it up, and the only safe
        # response is to attempt the exact same rollback path used for a
        # post-reload health failure, then fail this call closed either way.
        try:
            self._runtime.install(candidate.content)
            self._runtime.reload()
            self._audit("reload", {"version": candidate.version})
            report = self._runtime.health()
        except Exception as exc:
            self._audit("apply_mutation_failed", {"error": str(exc)})
            self._rollback(
                backup,
                new_username=new_username,
                failure_summary=(
                    "Xray candidate install, reload, or post-reload health "
                    "check raised an exception; backup restored"
                ),
                reload_error_message=(
                    "candidate install, reload, or health check raised an "
                    "exception before health could be confirmed"
                ),
            )

        post_reload_errors = _preservation_errors(
            candidate.content,
            self._runtime.current(),
            new_username=new_username,
        )
        if report.healthy and not post_reload_errors:
            self._audit("health_ok", report.details)
            return ApplyResult(True, candidate.version)

        if post_reload_errors:
            self._audit("post_reload_routes_failed", {"errors": post_reload_errors})

        self._rollback(
            backup,
            new_username=new_username,
            failure_summary="Xray health or route check failed; backup restored",
            reload_error_message="post-reload health check failed",
        )

    def _rollback(
        self,
        backup: object,
        *,
        new_username: str | None,
        failure_summary: str,
        reload_error_message: str,
    ) -> NoReturn:
        """Attempt to restore the previously backed-up config; always raises.

        This never reports a successful rollback unless `restore()`,
        `reload()`, and `health()` all completed without raising *and*
        health reports healthy. Any failure along this path (an exception
        from any of those three calls, or a healthy=False result) leaves
        the running system's state unverified/unknown -- it must be treated
        as fail-closed, never as an equivalent to a confirmed-good rollback,
        and must never be recorded as `rollback_reverified` succeeding.
        """
        try:
            self._runtime.restore(backup)
            self._audit("backup_restored", {})
        except Exception as restore_exc:
            self._audit("rollback_restore_failed", {"error": str(restore_exc)})
            self._finish_with_alert(
                new_username,
                "Xray rollback failed: could not restore the previous "
                "config; system state is unknown",
            )
            raise XrayReloadError(
                "rollback failed: restore() raised; system state is "
                "unknown and unverified"
            ) from restore_exc

        try:
            self._runtime.reload()
            self._audit("rollback_reload", {})
        except Exception as reload_exc:
            self._audit("rollback_reload_failed", {"error": str(reload_exc)})
            self._finish_with_alert(
                new_username,
                "Xray rollback failed: reload after restore raised; "
                "runtime state is unknown",
            )
            raise XrayReloadError(
                "rollback failed: reload() after restore raised; runtime "
                "state is unknown and unverified"
            ) from reload_exc

        try:
            rollback_health = self._runtime.health()
        except Exception as health_exc:
            self._audit("rollback_health_check_failed", {"error": str(health_exc)})
            self._finish_with_alert(
                new_username,
                "Xray rollback failed: health check after restore raised; "
                "runtime state is unknown",
            )
            raise XrayReloadError(
                "rollback failed: health() after restore raised; runtime "
                "state is unknown and unverified"
            ) from health_exc

        self._audit("rollback_reverified", {"healthy": rollback_health.healthy})
        if not rollback_health.healthy:
            self._finish_with_alert(
                new_username,
                "Xray rollback restored the previous config but the "
                "runtime is unhealthy afterwards",
            )
            raise XrayReloadError(
                "rollback restore succeeded but post-rollback health "
                "check reported unhealthy"
            )

        self._finish_with_alert(new_username, failure_summary)
        raise XrayReloadError(reload_error_message)

    def _finish_with_alert(self, new_username: str | None, message: str) -> None:
        if new_username is not None:
            self._disable_user(new_username)
            self._audit("new_user_disabled", {"username": new_username})
        self._alert(message)
        self._audit("alert_sent", {})

    def health(self) -> HealthReport:
        return self._runtime.health()


def _preservation_errors(
    current: Mapping[str, object],
    candidate: Mapping[str, object],
    *,
    new_username: str | None = None,
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
    current_clients = _inbound_clients(current)
    candidate_clients = _inbound_clients(candidate)
    if missing := current_clients - candidate_clients:
        errors.append("candidate removes existing inbound clients")
    current_routes = _rule_signatures(current)
    candidate_routes = _rule_signatures(candidate)
    if missing := current_routes - candidate_routes:
        errors.append("candidate removes existing routing rules")
    referenced_outbounds = _routing_outbound_tags(candidate)
    if missing := referenced_outbounds - candidate_outbounds:
        errors.append(f"candidate references missing outbounds: {sorted(missing)}")
    rules = _rules(candidate)
    private = {"type": "field", "ip": ["geoip:private"], "outboundTag": "BLOCK"}
    if not rules or rules[0] != private:
        errors.append("private BLOCK rule must be first")
    fallback = {"type": "field", "network": "tcp,udp", "outboundTag": "BLOCK"}
    if not rules or rules[-1] != fallback:
        errors.append("tcp,udp BLOCK fallback must be last")
    if any(
        isinstance(rule, Mapping) and rule.get("outboundTag") == "DIRECT"
        for rule in rules
    ):
        errors.append("DIRECT fallback is not permitted")
    if new_username is not None and new_username not in candidate_users:
        errors.append(f"candidate is missing the new user route: {new_username}")
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


def _inbound_clients(config: Mapping[str, object]) -> set[str]:
    clients: set[str] = set()
    inbounds = config.get("inbounds")
    if not isinstance(inbounds, list):
        return clients
    for inbound in inbounds:
        if not isinstance(inbound, Mapping):
            continue
        settings = inbound.get("settings")
        if not isinstance(settings, Mapping):
            continue
        rows = settings.get("clients")
        if isinstance(rows, list):
            clients.update(
                json.dumps(row, sort_keys=True, separators=(",", ":"))
                for row in rows
                if isinstance(row, Mapping)
            )
    return clients


def _rule_signatures(config: Mapping[str, object]) -> set[str]:
    return {
        json.dumps(rule, sort_keys=True, separators=(",", ":"))
        for rule in _rules(config)
        if isinstance(rule, Mapping)
    }


def _routing_outbound_tags(config: Mapping[str, object]) -> set[str]:
    return {
        outbound
        for rule in _rules(config)
        if isinstance(rule, Mapping)
        and isinstance((outbound := rule.get("outboundTag")), str)
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
