"""Mihomo forwarder provider with file and hot-reload rollback protection."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn, Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yaml

from backend.app.providers.base import (
    ApplyResult,
    CandidateConfig,
    DesiredForwarderState,
    ForwarderProvider,
    HealthReport,
)
from backend.app.providers.forwarder.mihomo_projection import compose_mihomo_document


class MihomoRuntimeError(RuntimeError):
    """Raised when Mihomo installation or reload fails."""


class MihomoRuntime(Protocol):
    """Filesystem/runtime boundary kept injectable for deterministic tests."""

    def backup(self) -> Path: ...

    def install(self, document: bytes) -> None: ...

    def reload(self) -> None: ...

    def restore(self, backup: Path) -> None: ...

    def health(self) -> HealthReport: ...


@dataclass(slots=True)
class LocalMihomoRuntime:
    config_path: Path
    backup_dir: Path
    api_url: str
    api_secret: str
    runtime_config_path: str | None = None

    def backup(self) -> Path:
        if not self.config_path.exists():
            raise MihomoRuntimeError("current Mihomo config does not exist")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        destination = self.backup_dir / f"{self.config_path.name}.{stamp}.bak"
        shutil.copy2(self.config_path, destination)
        return destination

    def install(self, document: bytes) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix(f"{self.config_path.suffix}.tmp")
        temporary.write_bytes(document)
        temporary.replace(self.config_path)
        with suppress(OSError):
            self.config_path.chmod(0o600)

    def reload(self) -> None:
        api = self.api_url.rstrip("/") + "/configs?" + urlencode({"force": "true"})
        payload = json.dumps(
            {"path": self.runtime_config_path or str(self.config_path)}
        ).encode()
        request = Request(
            api,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_secret}",
                "Content-Type": "application/json",
            },
            method="PUT",
        )
        try:
            with urlopen(request, timeout=10.0) as response:
                if not 200 <= response.status < 300:
                    raise MihomoRuntimeError(f"Mihomo reload returned HTTP {response.status}")
        except MihomoRuntimeError:
            raise
        except OSError as exc:
            raise MihomoRuntimeError("Mihomo hot reload failed") from exc

    def restore(self, backup: Path) -> None:
        shutil.copy2(backup, self.config_path)

    def health(self) -> HealthReport:
        api = self.api_url.rstrip("/") + "/version"
        request = Request(
            api,
            headers={"Authorization": f"Bearer {self.api_secret}"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=10.0) as response:
                healthy = 200 <= response.status < 300
        except OSError:
            healthy = False
        return HealthReport(healthy, {"mihomo_api": str(healthy)})


class MihomoForwarderProvider(ForwarderProvider):
    """Render from DB state and restore the exact pre-operation file on failure."""

    def __init__(self, runtime: MihomoRuntime) -> None:
        self._runtime = runtime

    def render(self, desired: DesiredForwarderState) -> CandidateConfig:
        document_data, version = compose_mihomo_document(desired)
        document = yaml.safe_dump(document_data, sort_keys=False).encode()
        try:
            parsed = yaml.safe_load(document)
        except yaml.YAMLError as exc:
            raise MihomoRuntimeError("rendered Mihomo document is invalid YAML") from exc
        if not isinstance(parsed, Mapping):
            raise MihomoRuntimeError("rendered Mihomo document is not a mapping")
        return CandidateConfig(dict(parsed), version)

    def apply(self, candidate: CandidateConfig) -> ApplyResult:
        backup = self._runtime.backup()
        document = yaml.safe_dump(candidate.content, sort_keys=False).encode()

        # `install()`/`reload()` succeeding without raising is not the same
        # as the reload having actually taken effect -- a reload command
        # can return cleanly while Mihomo's API is unreachable or the new
        # config left it in a broken state. `report` is only trusted once
        # `health()` has been called against the *post-reload* runtime.
        try:
            self._runtime.install(document)
            self._runtime.reload()
            report = self._runtime.health()
        except Exception as exc:
            self._rollback(backup, failure_summary="Mihomo activation failed", cause=exc)

        if not report.healthy:
            self._rollback(
                backup,
                failure_summary="Mihomo post-reload health check reported unhealthy",
                cause=None,
            )

        return ApplyResult(True, candidate.version)

    def _rollback(
        self, backup: Path, *, failure_summary: str, cause: Exception | None
    ) -> NoReturn:
        """Restore the previous config and re-verify health; always raises.

        A restored file plus a reload command that returns cleanly is not
        proof of recovery -- this only reports the rollback itself as safe
        once `health()` confirms it, and otherwise fails closed rather than
        silently claiming the previous known-good config is running again.
        """
        try:
            self._runtime.restore(backup)
            self._runtime.reload()
            rollback_report = self._runtime.health()
        except Exception as rollback_exc:
            raise MihomoRuntimeError(
                f"{failure_summary} and rollback failed; configuration "
                "state is unknown"
            ) from rollback_exc
        if not rollback_report.healthy:
            raise MihomoRuntimeError(
                f"{failure_summary}; rollback restored the previous config "
                "but the runtime is unhealthy afterwards"
            ) from cause
        raise MihomoRuntimeError(f"{failure_summary}; configuration restored") from cause

    def health(self) -> HealthReport:
        return self._runtime.health()


def local_runtime_from_env() -> LocalMihomoRuntime:
    target = Path(os.environ.get("MIHOMO_CONFIG_PATH", "data/mihomo/config.yaml")).resolve()
    backup_dir = Path(
        os.environ.get("MIHOMO_BACKUP_DIR", str(target.parent / "backups"))
    ).resolve()
    api_secret = os.environ.get("MIHOMO_API_SECRET", "").strip()
    if not api_secret:
        raise MihomoRuntimeError("MIHOMO_API_SECRET is required")
    return LocalMihomoRuntime(
        config_path=target,
        backup_dir=backup_dir,
        api_url=os.environ.get("MIHOMO_API_URL", "http://mihomo:9090"),
        api_secret=api_secret,
        runtime_config_path=os.environ.get("MIHOMO_RUNTIME_CONFIG_PATH"),
    )
