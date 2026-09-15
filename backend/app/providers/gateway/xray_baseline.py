"""Concrete, secret-safe persistence for the shared Xray writer baseline."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from backend.app.providers.gateway.xray_composition import (
    XrayCompositionError,
    repo_owned_xray_fingerprint,
)

BASELINE_SCHEMA_VERSION = 1
BASELINE_PROJECTION_VERSION = "xray-full-v1"


class XrayBaselineError(RuntimeError):
    """Raised when the persisted writer baseline cannot be trusted."""


@dataclass(frozen=True, slots=True)
class XrayAppliedStateStore:
    """Persist only the fingerprint of the complete repo-owned projection."""

    path: Path

    def load(self) -> str | None:
        try:
            raw_text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise XrayBaselineError("Xray baseline is unreadable") from exc

        try:
            raw = json.loads(raw_text)
        except (TypeError, ValueError) as exc:
            raise XrayBaselineError("Xray baseline is malformed") from exc
        if not isinstance(raw, Mapping):
            raise XrayBaselineError("Xray baseline is malformed")
        if set(raw) != {"schema_version", "projection_version", "sha256"}:
            raise XrayBaselineError("Xray baseline has an invalid schema")
        if raw["schema_version"] != BASELINE_SCHEMA_VERSION:
            raise XrayBaselineError("Xray baseline schema version is unsupported")
        if raw["projection_version"] != BASELINE_PROJECTION_VERSION:
            raise XrayBaselineError("Xray baseline projection version is unsupported")
        digest = raw["sha256"]
        if (
            not isinstance(digest, str)
            or len(digest) != hashlib.sha256().digest_size * 2
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise XrayBaselineError("Xray baseline digest is invalid")
        return digest

    def save(self, config: Mapping[str, object]) -> None:
        try:
            digest = repo_owned_xray_fingerprint(config)
        except XrayCompositionError as exc:
            raise XrayBaselineError("cannot persist an invalid Xray baseline") from exc
        payload = {
            "schema_version": BASELINE_SCHEMA_VERSION,
            "projection_version": BASELINE_PROJECTION_VERSION,
            "sha256": digest,
        }
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                encoding="utf-8",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, self.path)
            temporary = None
            _fsync_directory(self.path.parent)
        except OSError as exc:
            raise XrayBaselineError("cannot persist Xray baseline") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def ensure_current_matches_baseline(
    store: XrayAppliedStateStore,
    *,
    config_exists: Callable[[], bool],
    current: Callable[[], Mapping[str, object]],
) -> None:
    """Fail closed unless the shared file still equals the last repo apply."""

    baseline = store.load()
    try:
        exists = config_exists()
    except Exception as exc:
        raise XrayBaselineError("cannot determine whether Xray config exists") from exc

    if baseline is None:
        if exists:
            raise XrayBaselineError(
                "Xray baseline is missing for an existing config; explicit bootstrap required"
            )
        return
    if not exists:
        raise XrayBaselineError("Xray config is missing while its baseline exists")

    try:
        observed = current()
        observed_digest = repo_owned_xray_fingerprint(observed)
    except Exception as exc:
        raise XrayBaselineError("current Xray config cannot be projected safely") from exc
    if observed_digest != baseline:
        raise XrayBaselineError("Xray writer drift detected; current config differs from baseline")


def _fsync_directory(path: Path) -> None:
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, os.O_RDONLY | directory_flag)
    except (AttributeError, OSError):
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
