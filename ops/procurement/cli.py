"""Small read-only procurement CLI using the application Webshare provider."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

from backend.app.providers.egress.webshare import (
    UrllibTransport,
    WebshareProvider,
    WebshareReadOnlyAdapter,
)


def build_client(*, proxy_url: str | None = None) -> WebshareReadOnlyAdapter:
    api_key = os.environ.get("WEBSHARE_API_KEY")
    if not api_key:
        raise RuntimeError("WEBSHARE_API_KEY is required")
    provider = WebshareProvider(
        api_key,
        UrllibTransport(proxy_url=proxy_url),
    )
    return WebshareReadOnlyAdapter(provider)


def run(command: str, client: WebshareReadOnlyAdapter) -> Any:
    if command == "profile":
        return client.get("/profile/")
    if command == "subscription":
        return client.get("/subscription/")
    if command == "proxy-list":
        return client.paginate("/proxy/list/", params={"page_size": 100})
    if command == "subuser-list":
        return client.paginate("/subuser/", params={"page_size": 100})
    if command == "replacement-list":
        return client.paginate("/api/v3/proxy/replace/", params={"page_size": 100})
    raise ValueError(f"unknown read-only procurement command: {command}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Webshare procurement CLI")
    parser.add_argument(
        "command",
        choices=("profile", "subscription", "proxy-list", "subuser-list", "replacement-list"),
    )
    parser.add_argument("--proxy-url", help="proxy for this process only")
    args = parser.parse_args()
    print(json.dumps(run(args.command, build_client(proxy_url=args.proxy_url)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
