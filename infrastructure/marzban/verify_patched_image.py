#!/usr/bin/env python3
"""Fail-closed deployment-side identity verifier for a patched Marzban
image (TASK-T16 Phase 2C2).

Before Phase 2C2, a real Lingsway deployment could silently start
`gozargah/marzban:v0.8.4` (the upstream, unpatched image) with
`ACCOUNTING_PROVIDER=marzban` already selected -- nothing checked that the
image about to run actually carried the `routing_principal` source patch
(ADR-016 Decision 3) that `MarzbanAccountingProvider` depends on. An image
*tag* proves nothing: `MARZBAN_IMAGE` (or the compose default) is just a
string a caller controls, not evidence of what was actually built.

This script is the fail-closed gate: it inspects a candidate image's OCI
labels via `docker inspect` and checks them against
`pinned_upstream_manifest.REQUIRED_IMAGE_LABELS` -- the same constants
`build_patched_image.sh` attaches when it builds a genuinely patched
image. A missing `docker inspect`, an image that does not exist locally,
or any label mismatch all fail closed (non-zero exit, no fallback to "tag
looks right, assume it's fine").

Usage:
    python infrastructure/marzban/verify_patched_image.py <image-ref>

Exit code 0 only when every required label is present with its exact
expected value. Called from `deploy/lib/40_stack_up.sh` before Marzban is
started, and from the `marzban-image-contract` CI job after a real build.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_PATCHES_DIR = Path(__file__).resolve().parent / "patches"
if str(_PATCHES_DIR) not in sys.path:
    sys.path.insert(0, str(_PATCHES_DIR))

from pinned_upstream_manifest import REQUIRED_IMAGE_LABELS, validate_image_labels  # noqa: E402


class ImageInspectionError(Exception):
    """`docker inspect` could not be run, or returned something this
    verifier cannot make sense of. Always surfaced as a fail-closed
    error, never swallowed into "treat as unverifiable, allow anyway"."""


def read_image_labels(image_ref: str, *, docker_bin: str = "docker") -> dict[str, str | None]:
    """Return the OCI labels `docker inspect` reports for `image_ref`.

    Raises `ImageInspectionError` for every failure mode (docker binary
    missing, image not present locally, malformed/non-JSON output) --
    never returns a fabricated empty dict to paper over an inspection
    that didn't actually happen, since `validate_image_labels({})` and
    `validate_image_labels(None)` both already fail closed for missing
    labels; the distinction matters for accurate diagnostics only, not
    for whether the image is trusted.
    """
    try:
        result = subprocess.run(  # noqa: S603
            [docker_bin, "inspect", "--format", "{{json .Config.Labels}}", image_ref],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ImageInspectionError(f"'{docker_bin}' is not available: {exc}") from exc

    if result.returncode != 0:
        raise ImageInspectionError(
            f"'{docker_bin} inspect {image_ref}' failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )

    raw = result.stdout.strip()
    try:
        labels = json.loads(raw) if raw else None
    except json.JSONDecodeError as exc:
        raise ImageInspectionError(
            f"'{docker_bin} inspect {image_ref}' returned non-JSON label output: {raw!r}"
        ) from exc

    if labels is not None and not isinstance(labels, dict):
        raise ImageInspectionError(
            f"'{docker_bin} inspect {image_ref}' returned unexpected label shape: {labels!r}"
        )
    return labels or {}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: verify_patched_image.py <image-ref>", file=sys.stderr)
        return 2

    image_ref = argv[0]
    try:
        labels = read_image_labels(image_ref)
    except ImageInspectionError as exc:
        print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
        print(
            "Refusing to treat an image whose identity could not be "
            "inspected as a verified patched Marzban build.",
            file=sys.stderr,
        )
        return 1

    problems = validate_image_labels(labels)
    if problems:
        for message in problems:
            print(f"FAIL-CLOSED: {message}", file=sys.stderr)
        print(
            f"\nimage identity verification FAILED for {image_ref!r} "
            f"({len(problems)} problem(s)). This image does not prove it "
            "carries the routing_principal patch (ADR-016 Decision 3) -- "
            "refusing to let deployment start it as the Marzban accounting "
            "backend. Required labels:\n  "
            + "\n  ".join(f"{k}={v}" for k, v in REQUIRED_IMAGE_LABELS.items()),
            file=sys.stderr,
        )
        return 1

    print(f"OK: {image_ref!r} carries the verified patched-Marzban identity labels.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
