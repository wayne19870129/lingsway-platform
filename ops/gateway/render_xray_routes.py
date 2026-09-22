"""Render the complete Xray runtime configuration from database state."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from backend.app.core.config import get_settings
from backend.app.core.database import SessionLocal
from backend.app.infra.credential_resolver import SqlAlchemyCredentialResolver
from backend.app.infra.provisioning_state import full_desired_routing_snapshot
from backend.app.infra.xray_reality import bootstrap_reality_identity
from backend.app.providers.base import CredentialDTO, DesiredRoutingState, XrayOutboundDTO
from backend.app.providers.gateway.xray_baseline import (
    XrayAppliedStateStore,
    XrayBaselineError,
    ensure_current_matches_baseline,
)
from backend.app.providers.gateway.xray_composition import (
    XrayCompositionError,
    XrayDeploymentConfig,
    XrayFullConfigInput,
    XrayRealityConfig,
    XrayStaticSkeleton,
    compose_xray_config,
    repo_owned_xray_fingerprint,
)


class XrayRenderError(RuntimeError):
    """Raised when a complete safe runtime configuration cannot be rendered."""


BASE_CONFIG = Path(
    os.getenv("XRAY_BASE_CONFIG_PATH", "/app/infrastructure/marzban/xray_config.base.json")
)
OUTPUT_CONFIG = Path(
    os.getenv("XRAY_RUNTIME_CONFIG_PATH", "/app/data/marzban/xray_config.json")
)
BASELINE_PATH = Path(
    os.getenv(
        "XRAY_APPLIED_STATE_PATH",
        str(OUTPUT_CONFIG.with_name("xray_config.last_applied.json")),
    )
)


def _compose_desired(
    base_config: Mapping[str, Any],
    desired: DesiredRoutingState,
    credentials: Mapping[str, CredentialDTO],
    *,
    xray_log_level: str,
    reality_dest: str,
    reality_server_name: str,
    reality_identity: XrayRealityConfig,
) -> dict[str, Any]:
    try:
        skeleton = XrayStaticSkeleton.from_template(base_config)
        deployment = XrayDeploymentConfig(
            xray_log_level=xray_log_level,
            reality_dest=reality_dest,
            reality_server_names=(reality_server_name,),
        )
        candidate = compose_xray_config(
            XrayFullConfigInput(
                skeleton,
                deployment,
                reality_identity,
                desired,
                credentials,
            )
        )
    except (XrayCompositionError, TypeError, ValueError) as exc:
        raise XrayRenderError("Xray full configuration composition failed") from exc
    return dict(candidate.content)


def render_config(
    base_config: Mapping[str, Any],
    routes: Sequence[tuple[str, str, str, int, str, dict[str, str]]],
    *,
    xray_log_level: str = "warning",
    reality_dest: str = "",
    reality_server_name: str = "",
    reality_identity: XrayRealityConfig | None = None,
) -> dict[str, Any]:
    if reality_identity is None:
        raise XrayRenderError("Xray Reality identity is required")
    desired_routes: dict[str, str] = {}
    outbounds: list[XrayOutboundDTO] = []
    credentials: dict[str, CredentialDTO] = {}
    for principal, outbound_tag, protocol, port, host, credential in routes:
        ref = f"ops-render/{outbound_tag}"
        outbounds.append(XrayOutboundDTO(outbound_tag, host, port, protocol, ref))
        desired_routes[principal] = outbound_tag
        credentials[ref] = CredentialDTO(
            credential.get("username", ""), credential.get("password", "")
        )
    return _compose_desired(
        base_config,
        DesiredRoutingState(desired_routes, tuple(outbounds)),
        credentials,
        xray_log_level=xray_log_level,
        reality_dest=reality_dest,
        reality_server_name=reality_server_name,
        reality_identity=reality_identity,
    )


def render_from_database() -> Path:
    return _render_from_database_with_fingerprint()[0]


def render_from_database_with_fingerprint() -> tuple[Path, str]:
    return _render_from_database_with_fingerprint()


def _render_from_database_with_fingerprint() -> tuple[Path, str]:
    try:
        base_config = json.loads(BASE_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise XrayRenderError(f"cannot read base Xray config: {BASE_CONFIG}") from exc
    if not isinstance(base_config, dict):
        raise XrayRenderError("base Xray config must be a JSON object")
    settings = get_settings()
    with SessionLocal() as db:
        resolver = SqlAlchemyCredentialResolver(db)
        desired = full_desired_routing_snapshot(db)
        reality = resolver.resolve_reality_identity()
        credentials: dict[str, CredentialDTO] = {}
        for outbound in desired.outbounds:
            secret_ref = outbound.credential_secret_ref
            if secret_ref is None:
                continue
            credentials[secret_ref] = resolver.resolve(secret_ref)
        rendered = _compose_desired(
            base_config,
            desired,
            credentials,
            xray_log_level=settings.xray_log_level,
            reality_dest=settings.xray_reality_dest,
            reality_server_name=settings.xray_reality_server_name,
            reality_identity=reality,
        )
    return write_rendered_config(rendered), repo_owned_xray_fingerprint(rendered)


def write_rendered_config(
    rendered: Mapping[str, Any],
    *,
    output_config: Path = OUTPUT_CONFIG,
    baseline_path: Path = BASELINE_PATH,
) -> Path:
    """Guard and atomically write the shared Xray file used by deployment."""

    store = XrayAppliedStateStore(baseline_path)
    try:
        ensure_current_matches_baseline(
            store,
            config_exists=lambda: _config_exists(output_config),
            current=lambda: _read_current_config(output_config),
        )
    except XrayBaselineError as exc:
        raise XrayRenderError("Xray writer guard rejected the current config") from exc

    output_config.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if _config_exists(output_config):
        with tempfile.NamedTemporaryFile(
            dir=output_config.parent,
            prefix=f".{output_config.name}.",
            suffix=".backup",
            delete=False,
        ) as stream:
            backup = Path(stream.name)
        shutil.copy2(output_config, backup)

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=output_config.parent,
            prefix=f".{output_config.name}.",
            suffix=".tmp",
            encoding="utf-8",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(rendered, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, output_config)
        temporary = None
        return output_config
    except OSError as exc:
        raise XrayRenderError("cannot atomically write Xray runtime config") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if backup is not None:
            backup.unlink(missing_ok=True)


def _config_exists(path: Path) -> bool:
    try:
        path.stat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise XrayRenderError("cannot determine whether Xray config exists") from exc
    return True


def _read_current_config(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise XrayRenderError("current Xray config cannot be read safely") from exc
    if not isinstance(value, Mapping):
        raise XrayRenderError("current Xray config must be an object")
    return value


def main() -> None:
    with SessionLocal() as db:
        bootstrap_reality_identity(
            db,
            xray_binary=os.getenv("XRAY_BINARY", "/usr/local/bin/xray"),
        )
    output, expected_sha = render_from_database_with_fingerprint()
    settings = get_settings()
    print(
        f"rendered Xray config to {output} using {settings.app_env} settings; "
        f"expected_repo_owned_sha={expected_sha}"
    )


if __name__ == "__main__":
    main()

