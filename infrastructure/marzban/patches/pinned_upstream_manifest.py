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
