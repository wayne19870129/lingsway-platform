"""Registry-backed scheduler entry point.

The legacy worker depended on accounting and transport modules that were not
migrated into this repository.  This entry point keeps the Compose contract
and provider assembly explicit while those job handlers are migrated under
their own review; it never instantiates a concrete provider implementation.
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable

from backend.app.core.config import get_settings
from backend.app.providers.registry import ProviderRegistry, build_registry


def build_scheduler_registry() -> ProviderRegistry:
    """Build the complete provider set from environment configuration."""
    return build_registry(get_settings())


def run_batch(registry_factory: Callable[[], ProviderRegistry] = build_scheduler_registry) -> int:
    """Run one scheduler tick and return the number of processed jobs.

    Job handlers are intentionally not invented during this migration.  The
    registry construction is still performed on every tick so invalid runtime
    provider configuration fails closed before the process reports readiness.
    """
    registry_factory()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run registry-backed scheduler jobs")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    while True:
        run_batch()
        if not args.loop:
            return
        time.sleep(max(args.interval, 10))


if __name__ == "__main__":
    main()
