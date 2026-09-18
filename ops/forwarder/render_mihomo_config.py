"""Fail-closed legacy entry point; ADR-023 has one authoritative composer."""

from __future__ import annotations


def render_mihomo_document(*_args: object, **_kwargs: object) -> bytes:
    raise RuntimeError("legacy Mihomo renderer disabled; use compose_mihomo_document")


def main() -> None:
    raise RuntimeError("legacy Mihomo renderer disabled; use compose_mihomo_document")


if __name__ == "__main__":
    main()
