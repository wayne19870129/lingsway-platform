"""Test-facing wrapper around the shared pinned-upstream manifest.

The canonical pinned commit, file hashes, Xray email formula, and pinned
dependency versions live in `../pinned_upstream_manifest.py` (also used
by `../verify_pinned_upstream.py`, the CI-facing verifier) -- this module
only re-exports the one function the pytest fixtures need, so the pinned
commit and expected hashes are recorded in exactly one place.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PATCH_DIR = Path(__file__).resolve().parent.parent
if str(_PATCH_DIR) not in sys.path:
    sys.path.insert(0, str(_PATCH_DIR))

from pinned_upstream_manifest import (  # noqa: E402
    PinnedUpstreamFetchError as PinnedUpstreamFetchError,
)
from pinned_upstream_manifest import (  # noqa: E402
    fetch_pinned_file,
)


def fetch_pinned_upstream_user_py() -> bytes:
    return fetch_pinned_file("app/models/user.py")
