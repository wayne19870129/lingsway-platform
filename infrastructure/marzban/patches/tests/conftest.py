"""Shared harness for the Marzban routing_principal patch contract tests.

See ../README.md for exactly what is real upstream Marzban code (the
patched `app/models/user.py`) versus what is a deliberately minimal
TestClient-harness stub (everything else imported below).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import types
from collections.abc import Iterator
from enum import Enum
from pathlib import Path

import pytest
from _pinned_upstream import PinnedUpstreamFetchError, fetch_pinned_upstream_user_py

PATCH_DIR = Path(__file__).resolve().parent.parent
APPLY_SCRIPT = PATCH_DIR / "apply_patch.sh"


def _fetch_pinned_upstream_user_py_or_fail() -> bytes:
    try:
        return fetch_pinned_upstream_user_py()
    except PinnedUpstreamFetchError as exc:
        pytest.fail(str(exc))

_STUB_MODULE_NAMES = (
    "app",
    "app.xray",
    "app.models",
    "app.models.admin",
    "app.models.proxy",
    "app.subscription",
    "app.subscription.share",
    "app.utils",
    "app.utils.jwt",
    "config",
)


def _install_stub_dependencies() -> None:
    """Register minimal stand-ins for app/models/user.py's *unrelated*
    transitive imports (app.xray runtime state, app.models.admin's real
    Admin/DB-backed model, app.models.proxy's real xray_api/protobuf-backed
    proxy account types, app.subscription.share's real link-rendering
    templates, app.utils.jwt's real JWT signing, config's real
    env-driven settings). None of these are part of the routing_principal
    contract; the patched UserResponse class itself is genuine upstream
    code, imported unmodified except for the patch under test.
    """
    from pydantic import BaseModel, ConfigDict

    app_module = types.ModuleType("app")
    xray_module = types.ModuleType("app.xray")
    xray_module.config = types.SimpleNamespace(inbounds_by_protocol={}, inbounds_by_tag={})
    app_module.xray = xray_module

    models_module = types.ModuleType("app.models")

    admin_module = types.ModuleType("app.models.admin")

    class Admin(BaseModel):  # noqa: D101 - harness stub, not real Marzban Admin
        model_config = ConfigDict(from_attributes=True, extra="allow")
        username: str = ""
        is_sudo: bool = True

    admin_module.Admin = Admin
    models_module.admin = admin_module

    proxy_module = types.ModuleType("app.models.proxy")

    class ProxySettings(BaseModel):  # noqa: D101 - harness stub
        model_config = ConfigDict(extra="allow")

        @classmethod
        def from_dict(cls, proxy_type: object, _dict: dict) -> ProxySettings:
            del proxy_type
            return cls.model_validate(_dict)

    class ProxyTypes(str, Enum):  # noqa: D101, UP042 - matches real ProxyTypes(str, Enum) shape
        VMess = "vmess"
        VLESS = "vless"
        Trojan = "trojan"
        Shadowsocks = "shadowsocks"

    proxy_module.ProxySettings = ProxySettings
    proxy_module.ProxyTypes = ProxyTypes
    models_module.proxy = proxy_module

    app_module.models = models_module

    subscription_module = types.ModuleType("app.subscription")
    share_module = types.ModuleType("app.subscription.share")

    def generate_v2ray_links(*_args: object, **_kwargs: object) -> list[str]:
        raise AssertionError(
            "generate_v2ray_links() must never be called by the contract "
            "tests: every dbuser fixture supplies a non-empty `links` so "
            "UserResponse.validate_links() short-circuits. If this fires, "
            "a test fixture regressed, not the real Marzban code."
        )

    share_module.generate_v2ray_links = generate_v2ray_links
    subscription_module.share = share_module
    app_module.subscription = subscription_module

    utils_module = types.ModuleType("app.utils")
    jwt_module = types.ModuleType("app.utils.jwt")

    def create_subscription_token(*_args: object, **_kwargs: object) -> str:
        raise AssertionError(
            "create_subscription_token() must never be called by the "
            "contract tests: every dbuser fixture supplies a non-empty "
            "`subscription_url` so UserResponse.validate_subscription_url() "
            "short-circuits. If this fires, a test fixture regressed, not "
            "the real Marzban code."
        )

    jwt_module.create_subscription_token = create_subscription_token
    utils_module.jwt = jwt_module
    app_module.utils = utils_module

    config_module = types.ModuleType("config")
    config_module.XRAY_SUBSCRIPTION_PATH = "sub"
    config_module.XRAY_SUBSCRIPTION_URL_PREFIX = "https://harness.invalid"

    for name, module in (
        ("app", app_module),
        ("app.xray", xray_module),
        ("app.models", models_module),
        ("app.models.admin", admin_module),
        ("app.models.proxy", proxy_module),
        ("app.subscription", subscription_module),
        ("app.subscription.share", share_module),
        ("app.utils", utils_module),
        ("app.utils.jwt", jwt_module),
        ("config", config_module),
    ):
        sys.modules[name] = module


def _import_user_module(target: Path, *, module_name: str) -> Iterator[types.ModuleType]:
    saved_modules = {name: sys.modules.get(name) for name in _STUB_MODULE_NAMES}
    _install_stub_dependencies()
    saved_target_module = sys.modules.pop(module_name, None)
    try:
        spec = importlib.util.spec_from_file_location(module_name, target)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(module_name, None)
        if saved_target_module is not None:
            sys.modules[module_name] = saved_target_module
        for name, original in saved_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


@pytest.fixture
def patched_user_response_module(tmp_path: Path) -> Iterator[types.ModuleType]:
    """Apply the real patch to a fresh copy of the fetched-and-verified
    pinned source (via apply_patch.sh, not a hand-edit) and import the
    result."""
    source_root = tmp_path / "marzban-source"
    target = source_root / "app" / "models" / "user.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(_fetch_pinned_upstream_user_py_or_fail())

    result = subprocess.run(
        [str(APPLY_SCRIPT), str(source_root)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(
            "apply_patch.sh failed to apply the patch to the fetched "
            f"pinned source (this should never happen unless the pinned "
            f"upstream commit itself drifted from the patch):\n{result.stdout}\n{result.stderr}"
        )

    yield from _import_user_module(target, module_name="lingsway_marzban_patched_user_module")


@pytest.fixture
def unpatched_user_response_module(tmp_path: Path) -> Iterator[types.ModuleType]:
    """Import the fetched-and-verified pinned source as-is, with the patch
    NOT applied -- used to prove that unpatched source is correctly NOT
    contract-ready (no routing_principal, no importable `id` field on
    UserResponse)."""
    source_root = tmp_path / "marzban-source-unpatched"
    target = source_root / "app" / "models" / "user.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(_fetch_pinned_upstream_user_py_or_fail())

    yield from _import_user_module(target, module_name="lingsway_marzban_unpatched_user_module")
