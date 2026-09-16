"""File-backed Xray provider enforcing the nine-step safe reload sequence."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
from contextlib import contextmanager
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator, NoReturn, Protocol

import httpx

from backend.app.core.config import Settings
from backend.app.providers.marzban_tls import build_marzban_tls_verify
from backend.app.providers.base import (
    ApplyResult,
    CandidateConfig,
    CredentialResolver,
    DesiredRoutingState,
    HealthReport,
    ValidationResult,
)
from backend.app.providers.gateway.xray_baseline import (
    XrayAppliedStateStore,
    XrayBaselineError,
    ensure_current_matches_baseline,
)
from backend.app.providers.gateway.xray_composition import (
    XrayCompositionError,
    XrayDeploymentConfig,
    XrayFullConfigInput,
    XrayRenderResolver,
    XrayStaticSkeleton,
    compose_xray_config,
    repo_owned_projection_errors,
    validate_repo_owned_xray_config,
)


class XrayValidationError(RuntimeError):
    pass


class XrayReloadError(RuntimeError):
    pass


class XrayRuntime(Protocol):
    def backup(self) -> object: ...
    def config_exists(self) -> bool: ...
    def current(self) -> Mapping[str, object]: ...
    def xray_test(self, content: Mapping[str, object]) -> bool: ...
    def install(self, content: Mapping[str, object]) -> None: ...
    def reload(self) -> None: ...
    def health(self) -> HealthReport: ...
    def restore(self, backup: object) -> None: ...


AuditCallback = Callable[[str, Mapping[str, object]], None]


class XrayFileProvider:
    @classmethod
    def from_template(
        cls,
        runtime: XrayRuntime,
        template: Mapping[str, object],
        settings: Settings,
        *,
        disable_user: Callable[[str], None] = lambda _username: None,
        alert: Callable[[str], None] = lambda _message: None,
        audit: AuditCallback = lambda _event, _details: None,
        baseline_store: XrayAppliedStateStore | None = None,
    ) -> XrayFileProvider:
        """Build a provider from validated process-stable template/Settings projections."""

        return cls(
            runtime,
            static_skeleton=XrayStaticSkeleton.from_template(template),
            deployment_config=XrayDeploymentConfig.from_settings(settings),
            disable_user=disable_user,
            alert=alert,
            audit=audit,
            baseline_store=baseline_store,
        )

    def __init__(
        self,
        runtime: XrayRuntime,
        *,
        static_skeleton: XrayStaticSkeleton | None = None,
        deployment_config: XrayDeploymentConfig | None = None,
        disable_user: Callable[[str], None] = lambda _username: None,
        alert: Callable[[str], None] = lambda _message: None,
        audit: AuditCallback = lambda _event, _details: None,
        baseline_store: XrayAppliedStateStore | None = None,
    ) -> None:
        self._runtime = runtime
        # The skeleton is optional only so validation/apply-only callers can
        # construct the provider. A live render must receive the validated
        # STATIC_TEMPLATE projection from from_template().
        self._static_skeleton = static_skeleton
        self._deployment_config = deployment_config
        self._disable_user = disable_user
        self._alert = alert
        self._audit = audit
        self._baseline_store = baseline_store or _baseline_store_for_runtime(runtime)

    def render(
        self, desired: DesiredRoutingState, resolver: CredentialResolver
    ) -> CandidateConfig:
        skeleton = self._static_skeleton
        if skeleton is None:
            raise XrayValidationError("Xray static skeleton is not configured")
        if self._deployment_config is None:
            raise XrayValidationError("Xray deployment configuration is not configured")
        if not isinstance(resolver, XrayRenderResolver):
            raise XrayValidationError("Xray render resolver capability is required")

        try:
            reality = resolver.resolve_reality_identity()
            credentials = {
                outbound.credential_secret_ref: resolver.resolve(
                    outbound.credential_secret_ref
                )
                for outbound in desired.outbounds
            }
            input_value = XrayFullConfigInput(
                skeleton,
                self._deployment_config,
                reality,
                desired,
                credentials,
            )
            return compose_xray_config(input_value)
        except XrayCompositionError as exc:
            raise XrayValidationError(str(exc)) from None

    def validate(
        self, candidate: CandidateConfig, *, new_username: str | None = None
    ) -> ValidationResult:
        try:
            parsed = json.loads(json.dumps(candidate.content))
        except (TypeError, ValueError):
            return ValidationResult(False, ("candidate is not JSON-serializable",))
        if not isinstance(parsed, Mapping):
            return ValidationResult(False, ("candidate Xray config must be an object",))
        errors = list(validate_repo_owned_xray_config(parsed))
        candidate_users = _routed_users(parsed)
        if new_username is not None and new_username not in candidate_users:
            errors.append("candidate is missing the new user route")
        if not errors and not self._runtime.xray_test(parsed):
            errors.append("xray run -test failed")
        return ValidationResult(not errors, tuple(errors))

    def apply(self, candidate: CandidateConfig, *, new_username: str | None = None) -> ApplyResult:
        backup = self._runtime.backup()
        self._audit("backup_created", {"version": candidate.version})
        if self._baseline_store is not None:
            try:
                ensure_current_matches_baseline(
                    self._baseline_store,
                    config_exists=self._runtime.config_exists,
                    current=self._runtime.current,
                )
            except XrayBaselineError as exc:
                self._audit("writer_guard_rejected", {"reason": str(exc)})
                self._alert("Xray writer guard rejected the candidate")
                raise XrayValidationError(str(exc)) from None
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

        try:
            observed = self._runtime.current()
            post_reload_errors = list(
                repo_owned_projection_errors(candidate.content, observed)
            )
        except Exception:
            post_reload_errors = ["observed repo-owned Xray config is unavailable"]
        if report.healthy and not post_reload_errors:
            if self._baseline_store is not None:
                try:
                    self._baseline_store.save(candidate.content)
                except XrayBaselineError:
                    self._audit("baseline_persistence_failed", {})
                    self._rollback(
                        backup,
                        new_username=new_username,
                        failure_summary=(
                            "Xray apply succeeded but its writer baseline could not "
                            "be persisted; previous config restored"
                        ),
                        reload_error_message=(
                            "Xray writer baseline persistence failed after apply"
                        ),
                    )
            self._audit("health_ok", report.details)
            return ApplyResult(True, candidate.version)

        if post_reload_errors:
            self._audit("post_reload_projection_failed", {"errors": post_reload_errors})

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
            self._fail_after_rollback_failure(
                new_username,
                alert_message=(
                    "Xray rollback failed: could not restore the previous "
                    "config; system state is unknown"
                ),
                error_message=(
                    "rollback failed: restore() raised; system state is "
                    "unknown and unverified"
                ),
                cause=restore_exc,
            )

        try:
            self._runtime.reload()
            self._audit("rollback_reload", {})
        except Exception as reload_exc:
            self._audit("rollback_reload_failed", {"error": str(reload_exc)})
            self._fail_after_rollback_failure(
                new_username,
                alert_message=(
                    "Xray rollback failed: reload after restore raised; "
                    "runtime state is unknown"
                ),
                error_message=(
                    "rollback failed: reload() after restore raised; runtime "
                    "state is unknown and unverified"
                ),
                cause=reload_exc,
            )

        try:
            rollback_health = self._runtime.health()
        except Exception as health_exc:
            self._audit("rollback_health_check_failed", {"error": str(health_exc)})
            self._fail_after_rollback_failure(
                new_username,
                alert_message=(
                    "Xray rollback failed: health check after restore raised; "
                    "runtime state is unknown"
                ),
                error_message=(
                    "rollback failed: health() after restore raised; runtime "
                    "state is unknown and unverified"
                ),
                cause=health_exc,
            )

        self._audit("rollback_reverified", {"healthy": rollback_health.healthy})
        if not rollback_health.healthy:
            self._fail_after_rollback_failure(
                new_username,
                alert_message=(
                    "Xray rollback restored the previous config but the "
                    "runtime is unhealthy afterwards"
                ),
                error_message=(
                    "rollback restore succeeded but post-rollback health "
                    "check reported unhealthy"
                ),
                cause=RuntimeError("post-rollback health reported unhealthy"),
            )

        self._finish_with_alert(new_username, failure_summary)
        raise XrayReloadError(reload_error_message)

    def _fail_after_rollback_failure(
        self,
        new_username: str | None,
        *,
        alert_message: str,
        error_message: str,
        cause: BaseException,
    ) -> NoReturn:
        if self._baseline_store is not None:
            try:
                self._baseline_store.mark_degraded()
            except Exception as marker_exc:
                self._audit("baseline_degraded_marker_failed", {})
                self._finish_with_alert(
                    new_username,
                    "Xray rollback state is unknown and the degraded baseline "
                    "marker could not be persisted; manual intervention required",
                )
                raise XrayReloadError(
                    "rollback failed and degraded baseline marker could not be persisted"
                ) from marker_exc
            self._audit("baseline_degraded", {})
        self._finish_with_alert(new_username, alert_message)
        raise XrayReloadError(error_message) from cause

    def _finish_with_alert(self, new_username: str | None, message: str) -> None:
        if new_username is not None:
            self._disable_user(new_username)
            self._audit("new_user_disabled", {"username": new_username})
        self._alert(message)
        self._audit("alert_sent", {})

    def health(self) -> HealthReport:
        return self._runtime.health()


def _routed_users(config: Mapping[str, object]) -> set[str]:
    users: set[str] = set()
    routing = config.get("routing")
    rules = routing.get("rules") if isinstance(routing, Mapping) else None
    for rule in rules if isinstance(rules, list) else []:
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
    baseline_path: Path | None = None

    def __post_init__(self) -> None:
        if self.baseline_path is None:
            self.baseline_path = self.config_path.with_name(
                f"{self.config_path.stem}.last_applied.json"
            )

    def backup(self) -> object:
        if not self.config_exists():
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        destination = self.backup_dir / (
            f"xray_config.{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.json"
        )
        shutil.copy2(self.config_path, destination)
        return destination

    def config_exists(self) -> bool:
        try:
            self.config_path.stat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise XrayValidationError("cannot determine whether Xray config exists") from exc
        return True

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
        if backup is None:
            self.config_path.unlink(missing_ok=True)
            return
        if not isinstance(backup, Path):
            raise TypeError("backup must be a Path")
        shutil.copy2(backup, self.config_path)


class MarzbanXrayControlError(RuntimeError):
    """Stable, secret-safe failure from the Marzban Xray control API."""


@dataclass(slots=True)
class MarzbanXrayRuntime(LocalXrayRuntime):
    """Concrete Xray runtime controlled by the Marzban admin API.

    File backup, candidate validation, installation, and rollback storage are
    inherited from LocalXrayRuntime.  Only activation and health differ:
    Marzban owns the Xray process, so this runtime never invokes systemd or
    Docker.  Authentication state is local to one control operation and is
    never stored on the runtime object.
    """

    control_base_url: str = ""
    admin_username: str = ""
    admin_password: str = ""
    verify_tls: bool = True
    ca_cert_path: str = "/app/data/marzban/internal.crt"
    health_host: str = "marzban"
    health_port: int = 8443
    timeout_seconds: float = 10.0

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(config_path={str(self.config_path)!r}, "
            f"control_base_url={self.control_base_url!r}, "
            f"health_host={self.health_host!r}, health_port={self.health_port!r})"
        )

    @contextmanager
    def _client(self) -> Iterator[httpx.Client]:
        verify = build_marzban_tls_verify(
            verify_tls=self.verify_tls, ca_cert_path=self.ca_cert_path
        )
        with httpx.Client(verify=verify, timeout=self.timeout_seconds) as client:
            yield client

    def _authenticate(self, client: httpx.Client) -> str:
        try:
            response = client.post(
                f"{self.control_base_url.rstrip('/')}/api/admin/token",
                data={
                    "username": self.admin_username,
                    "password": self.admin_password,
                },
            )
        except httpx.HTTPError as exc:
            raise MarzbanXrayControlError(
                "Marzban Xray authentication failed at the transport layer"
            ) from exc
        if response.status_code != 200:
            raise MarzbanXrayControlError("Marzban Xray authentication was rejected")
        try:
            payload = response.json()
        except (ValueError, TypeError) as exc:
            raise MarzbanXrayControlError(
                "Marzban Xray authentication returned invalid JSON"
            ) from exc
        if not isinstance(payload, Mapping):
            raise MarzbanXrayControlError(
                "Marzban Xray authentication returned an invalid payload"
            )
        token = payload.get("access_token")
        token_type = payload.get("token_type")
        if not isinstance(token, str) or not token.strip():
            raise MarzbanXrayControlError(
                "Marzban Xray authentication response is missing access_token"
            )
        if token_type is not None and not (
            isinstance(token_type, str) and token_type.lower() == "bearer"
        ):
            raise MarzbanXrayControlError(
                "Marzban Xray authentication response token_type was not bearer"
            )
        return token

    def _request(
        self,
        client: httpx.Client,
        method: str,
        path: str,
        token: str,
        *,
        json_body: Mapping[str, object] | None = None,
    ) -> httpx.Response:
        try:
            return client.request(
                method,
                f"{self.control_base_url.rstrip('/')}{path}",
                headers={"Authorization": f"Bearer {token}"},
                json=json_body,
            )
        except httpx.HTTPError as exc:
            raise MarzbanXrayControlError(
                "Marzban Xray control request failed at the transport layer"
            ) from exc

    def reload(self) -> None:
        """Activate the exact installed file through Marzban's control API.

        A 401 is safe to re-authenticate once because the pinned Marzban
        admin dependency rejects it before the route body executes.  A
        transport failure is never retried, because PUT may have reached the
        server before the failure was observed.
        """

        candidate = self.current()
        token: str | None = None
        try:
            with self._client() as client:
                token = self._authenticate(client)
                response = self._request(
                    client,
                    "PUT",
                    "/api/core/config",
                    token,
                    json_body=candidate,
                )
                if response.status_code == 401:
                    token = self._authenticate(client)
                    response = self._request(
                        client,
                        "PUT",
                        "/api/core/config",
                        token,
                        json_body=candidate,
                    )
                if response.status_code != 200:
                    raise MarzbanXrayControlError(
                        "Marzban rejected the Xray configuration activation"
                    )
                try:
                    accepted = response.json()
                except (ValueError, TypeError) as exc:
                    raise MarzbanXrayControlError(
                        "Marzban Xray activation returned invalid JSON"
                    ) from exc
                if accepted != candidate:
                    raise MarzbanXrayControlError(
                        "Marzban Xray activation response did not match the candidate"
                    )
        finally:
            token = None

    def health(self) -> HealthReport:
        api_ok = False
        core_started = False
        port_ok = False
        token: str | None = None
        try:
            with self._client() as client:
                token = self._authenticate(client)
                response = self._request(client, "GET", "/api/core", token)
                if response.status_code == 200:
                    try:
                        payload = response.json()
                    except (ValueError, TypeError):
                        payload = None
                    if isinstance(payload, Mapping):
                        api_ok = True
                        core_started = payload.get("started") is True
        except (MarzbanXrayControlError, httpx.HTTPError, OSError, ValueError):
            pass
        finally:
            token = None

        try:
            with socket.create_connection(
                (self.health_host, self.health_port), timeout=2
            ):
                port_ok = True
        except OSError:
            pass

        return HealthReport(
            api_ok and core_started and port_ok,
            {
                "marzban_control_api": str(api_ok),
                "xray_core_started": str(core_started),
                "port_8443": str(port_ok),
                "health_host": self.health_host,
            },
        )


def _baseline_store_for_runtime(runtime: XrayRuntime) -> XrayAppliedStateStore | None:
    if isinstance(runtime, LocalXrayRuntime):
        if runtime.baseline_path is None:
            raise XrayValidationError("Xray baseline path is not configured")
        return XrayAppliedStateStore(runtime.baseline_path)
    return None

