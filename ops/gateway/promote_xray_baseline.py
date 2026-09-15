"""Promote a post-start verified Xray file to the applied baseline."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from pathlib import Path

from backend.app.providers.gateway.xray_baseline import (
    XrayAppliedStateStore,
    XrayBaselineError,
)
from backend.app.providers.gateway.xray_composition import repo_owned_xray_fingerprint

DEFAULT_CONFIG_PATH = Path("/app/data/marzban/xray_config.json")


def promote_applied_baseline(
    expected_sha: str,
    *,
    config_path: Path,
    baseline_path: Path,
) -> str:
    """Persist APPLIED only when the verified file is exactly this candidate."""

    store = XrayAppliedStateStore(baseline_path)
    existing = store.load_state()
    if existing is not None and existing.state == "degraded":
        raise XrayBaselineError(
            "Xray baseline is degraded; explicit reconciliation is required before promotion"
        )
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise XrayBaselineError("verified Xray config cannot be read safely") from exc
    if not isinstance(value, Mapping):
        raise XrayBaselineError("verified Xray config must be an object")
    observed_sha = repo_owned_xray_fingerprint(value)
    if observed_sha != expected_sha:
        raise XrayBaselineError(
            "verified Xray config differs from the rendered candidate; refusing baseline promotion"
        )
    store.save(value)
    return observed_sha


def _default_baseline_path(config_path: Path) -> Path:
    return config_path.with_name(f"{config_path.stem}.last_applied.json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.getenv("XRAY_RUNTIME_CONFIG_PATH", str(DEFAULT_CONFIG_PATH))),
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
    )
    args = parser.parse_args()
    baseline_path = args.baseline or Path(
        os.getenv(
            "XRAY_APPLIED_STATE_PATH",
            str(_default_baseline_path(args.config)),
        )
    )
    try:
        observed_sha = promote_applied_baseline(
            args.expected_sha,
            config_path=args.config,
            baseline_path=baseline_path,
        )
    except XrayBaselineError as exc:
        raise SystemExit(str(exc)) from None
    print(f"promoted verified Xray baseline sha={observed_sha}")


if __name__ == "__main__":
    main()

