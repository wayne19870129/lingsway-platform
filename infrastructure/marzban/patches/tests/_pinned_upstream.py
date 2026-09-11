"""Shared pinned-upstream fetch-and-verify helper for the Marzban
routing_principal patch tests.

This fetches the pinned commit's `app/models/user.py` over the network at
test time and verifies its sha256 against the hash recorded during this
PR's upstream research, instead of committing a copy of this
AGPL-3.0-licensed file into this repository's own, separately-licensed
(proprietary) root `LICENSE`. See ../README.md ("Upstream source: fetched,
not vendored") for the full rationale. Fails closed on any network error
or hash mismatch -- never a silent fallback to a stale or unverified copy.
"""

from __future__ import annotations

import hashlib
import urllib.request
from functools import lru_cache

PINNED_COMMIT = "7f396db3e703d71a28060bc9ce4a532ec64cb1f4"
UPSTREAM_USER_PY_URL = (
    f"https://raw.githubusercontent.com/Gozargah/Marzban/{PINNED_COMMIT}/app/models/user.py"
)

# Recorded via `sha256sum` against the file fetched from the URL above
# during this PR's upstream research, and cross-checked byte-identical
# against the `v0.8.4` tag ref of the same path. See ../README.md.
EXPECTED_UPSTREAM_USER_PY_SHA256 = (
    "cb92c72875bc4462d658d46c78261be0448dbd6c64c719d16887affc40b6a704"
)


class PinnedUpstreamFetchError(Exception):
    """The pinned upstream source could not be fetched or verified.

    Always raised, never swallowed into a fallback -- callers must fail
    the test, not silently proceed with unverified or absent content.
    """


@lru_cache(maxsize=1)
def fetch_pinned_upstream_user_py() -> bytes:
    """Fetch and sha256-verify the pinned commit's `app/models/user.py`.

    Cached for the lifetime of the test process so multiple fixtures/tests
    in one run only hit the network once.
    """
    try:
        with urllib.request.urlopen(UPSTREAM_USER_PY_URL, timeout=20) as response:  # noqa: S310
            content = response.read()
    except OSError as exc:
        raise PinnedUpstreamFetchError(
            f"failed to fetch the pinned upstream app/models/user.py from "
            f"{UPSTREAM_USER_PY_URL}: {exc}"
        ) from exc

    digest = hashlib.sha256(content).hexdigest()
    if digest != EXPECTED_UPSTREAM_USER_PY_SHA256:
        raise PinnedUpstreamFetchError(
            "fetched app/models/user.py sha256 mismatch: expected "
            f"{EXPECTED_UPSTREAM_USER_PY_SHA256}, got {digest}. Refusing to "
            "treat unverified content as the pinned upstream source."
        )
    return content
