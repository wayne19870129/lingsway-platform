"""Canonical pinned-upstream manifest for Marzban commit
`7f396db3e703d71a28060bc9ce4a532ec64cb1f4` (tag `v0.8.4`).

Single source of truth for which upstream files this repository's
`routing_principal` patch/contract depends on, and their verified content
hashes. Both `verify_pinned_upstream.py` (the CI-facing verifier) and the
pytest contract-test fixtures (`tests/_pinned_upstream.py`) import from
here so the pinned commit and expected hashes are recorded in exactly one
place -- never re-typed or independently re-derived.
"""

from __future__ import annotations

import hashlib
import urllib.request
from functools import cache

PINNED_COMMIT = "7f396db3e703d71a28060bc9ce4a532ec64cb1f4"
PINNED_TAG = "v0.8.4"
_RAW_BASE = f"https://raw.githubusercontent.com/Gozargah/Marzban/{PINNED_COMMIT}"

# path (relative to the Marzban repo root) -> expected sha256, verified
# against a direct fetch of this exact commit during TASK-T16 Phase 2B4's
# upstream research (see ../README.md).
PINNED_FILES: dict[str, str] = {
    "app/models/user.py": "cb92c72875bc4462d658d46c78261be0448dbd6c64c719d16887affc40b6a704",
    "app/xray/operations.py": "227500604dd67c3add1968d5511deb1b8d1263c07bee5dadc7206fd9ebe33cf7",
    "requirements.txt": "61ecb9f6ff098b95eea5a52f13cbc0b50333c16376d6bd7ea5c445c8ad2a9bc6",
}

# The exact Xray routing-identity formula `app/xray/operations.py`
# computes internally (add_user/remove_user/update_user), which this
# repository's patch (`routing_principal`) must keep matching
# byte-for-byte. A hash mismatch on operations.py already implies this
# changed, but checking the literal formula too gives a direct,
# human-readable diagnostic if that file's pin is ever intentionally
# relaxed without re-deriving this fact.
XRAY_EMAIL_FORMULA = 'f"{dbuser.id}.{dbuser.username}"'

# The pinned dependency versions this patch and its contract tests were
# verified against; see ADR-016 and ../README.md.
PINNED_DEPENDENCIES: dict[str, str] = {
    "pydantic": "2.10.4",
    "fastapi": "0.115.2",
    "starlette": "0.40.0",
    "SQLAlchemy": "2.0.36",
}


# Identity of the routing_principal source patch (TASK-T16 Phase 2B4),
# and of the contract that build_patched_image.sh's patched image must
# prove it was built from -- see ../README.md and ../build_patched_image.sh.
PATCH_ID = "0001-expose-routing-principal"
ROUTING_PRINCIPAL_PATCH_MARKER = "applied"
IMAGE_CONTRACT_VERSION = "1"

# Immutable OCI label keys `build_patched_image.sh` attaches to a patched
# image, and `verify_patched_image.py` checks a candidate image against
# before any deployment is allowed to start it (TASK-T16 Phase 2C2). Kept
# here, next to the values they must match, so neither the build script
# nor the deployment-side verifier can drift from a second, re-typed copy
# of these facts.
IMAGE_LABEL_UPSTREAM_COMMIT = "org.lingsway.marzban.upstream-commit"
IMAGE_LABEL_UPSTREAM_TAG = "org.lingsway.marzban.upstream-tag"
IMAGE_LABEL_ROUTING_PRINCIPAL_PATCH = "org.lingsway.marzban.routing-principal-patch"
IMAGE_LABEL_PATCH_ID = "org.lingsway.marzban.patch-id"
IMAGE_LABEL_CONTRACT_VERSION = "org.lingsway.marzban.contract-version"

# The exact, deterministic label values a genuinely patched image must
# carry. No timestamp/build-host/other non-deterministic label is part of
# this contract -- identity verification must be able to run the same way
# for the same source on any machine, at any time.
REQUIRED_IMAGE_LABELS: dict[str, str] = {
    IMAGE_LABEL_UPSTREAM_COMMIT: PINNED_COMMIT,
    IMAGE_LABEL_UPSTREAM_TAG: PINNED_TAG,
    IMAGE_LABEL_ROUTING_PRINCIPAL_PATCH: ROUTING_PRINCIPAL_PATCH_MARKER,
    IMAGE_LABEL_PATCH_ID: PATCH_ID,
    IMAGE_LABEL_CONTRACT_VERSION: IMAGE_CONTRACT_VERSION,
}


def validate_image_labels(labels: dict[str, str | None] | None) -> list[str]:
    """Return problem descriptions for any `REQUIRED_IMAGE_LABELS` entry
    missing from `labels` or not matching its exact expected value. Empty
    list means the image's identity labels fully match this repository's
    pinned commit + patch contract.

    Fail-closed by construction: `labels=None` (e.g. an image with no
    `Config.Labels` at all) is treated the same as an image carrying none
    of the required labels -- every one is reported missing, never
    skipped or treated as "unverifiable, so allow it".
    """
    present = labels or {}
    problems: list[str] = []
    for key, expected in REQUIRED_IMAGE_LABELS.items():
        actual = present.get(key)
        if actual != expected:
            problems.append(
                f"image label {key!r} = {actual!r}, expected {expected!r} -- "
                "refusing to treat this image as the verified patched "
                "Marzban build"
            )
    return problems


def check_pinned_content(path: str, content: bytes) -> list[str]:
    """Verify one `PINNED_FILES` entry's actual content against the
    *additional* contract the two files that carry one still hold: the
    exact Xray email formula (`app/xray/operations.py`) and the pinned
    dependency versions (`requirements.txt`).

    Deliberately does **not** check the sha256 hash itself -- callers
    fall into one of two cases, and each already owns that check the way
    it needs to: `verify_pinned_upstream.py` only ever passes content
    `fetch_pinned_file()` already hash-verified (a mismatch there raises
    `PinnedUpstreamFetchError` before this function is ever called), while
    `verify_local_source_tree.py` checks the hash itself directly against
    `PINNED_FILES` and, unlike the network path, keeps checking this
    function's formula/dependency contract even when the hash already
    failed -- so a local source tree that's wrong in more than one way
    gets every problem reported, not just the first.

    Shared by both callers so the check *logic* -- not just the constants
    above -- lives in exactly one place. Never raises for an expected
    failure (missing formula, dependency drift); returns them as strings
    so callers can report every problem found instead of stopping at the
    first.
    """
    if path not in PINNED_FILES:
        raise KeyError(f"{path!r} is not a recorded pinned file; add it to PINNED_FILES first")

    problems: list[str] = []
    text = content.decode("utf-8")
    if path == "app/xray/operations.py" and XRAY_EMAIL_FORMULA not in text:
        problems.append(
            "pinned app/xray/operations.py no longer contains the exact "
            f"Xray client email formula {XRAY_EMAIL_FORMULA!r} this "
            "repository's routing_principal patch must keep matching"
        )
    if path == "requirements.txt":
        problems.extend(check_dependency_versions(text))
    return problems


def check_dependency_versions(requirements_text: str) -> list[str]:
    """Return problem descriptions for any pinned dependency in
    `PINNED_DEPENDENCIES` whose exact `name==version` line is missing
    from `requirements_text`. Empty list means all pins still hold."""
    lines = {line.strip() for line in requirements_text.splitlines()}
    problems = []
    for package, version in PINNED_DEPENDENCIES.items():
        expected_line = f"{package}=={version}"
        if expected_line not in lines:
            problems.append(
                f"requirements.txt no longer pins {expected_line!r} exactly "
                "(this repository's Marzban patch/contract was verified "
                "against that exact version)"
            )
    return problems


class PinnedUpstreamFetchError(Exception):
    """A pinned upstream file could not be fetched or failed verification.

    Always raised, never swallowed into a fallback -- callers must fail,
    not silently proceed with unverified or absent content.
    """


@cache
def fetch_pinned_file(path: str) -> bytes:
    """Fetch and sha256-verify one of `PINNED_FILES`.

    Cached for the lifetime of the process so repeated callers (multiple
    tests, or the verifier checking several files) only hit the network
    once per path.
    """
    if path not in PINNED_FILES:
        raise KeyError(f"{path!r} is not a recorded pinned file; add it to PINNED_FILES first")

    url = f"{_RAW_BASE}/{path}"
    try:
        with urllib.request.urlopen(url, timeout=20) as response:  # noqa: S310
            content = response.read()
    except OSError as exc:
        raise PinnedUpstreamFetchError(f"failed to fetch pinned {path} from {url}: {exc}") from exc

    digest = hashlib.sha256(content).hexdigest()
    expected = PINNED_FILES[path]
    if digest != expected:
        raise PinnedUpstreamFetchError(
            f"fetched {path} sha256 mismatch: expected {expected}, got {digest}. "
            "Refusing to treat unverified content as the pinned upstream source."
        )
    return content
