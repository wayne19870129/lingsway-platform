"""Shared failure injection for provider test doubles."""

from collections.abc import Mapping


def raise_injected(failures: Mapping[str, Exception], operation: str) -> None:
    """Raise the exception configured for an operation, if any."""
    failure = failures.get(operation)
    if failure is not None:
        raise failure
