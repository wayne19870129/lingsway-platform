from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backend.app.providers.base import DesiredForwarderState
from backend.app.providers.forwarder.mihomo import MihomoForwarderProvider
from backend.app.providers.forwarder.mihomo_projection import (
    MihomoProjectionError,
    TransportMaterialization,
    compose_mihomo_document,
)


def materialization(owner: int, code: str, content: dict[str, object]) -> TransportMaterialization:
    import hashlib
    import json

    digest = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return TransportMaterialization(
        owner,
        code,
        1,
        f"cache/{owner}",
        digest,
        datetime.now(UTC) + timedelta(minutes=5),
        content,
    )


def snapshot() -> DesiredForwarderState:
    return DesiredForwarderState(
        listeners={"listener-a": "127.0.0.1:10001"},
        proxies=({"name": "proxy-a", "type": "ss", "server": "example.invalid"},),
        proxy_groups=({"name": "AUTO", "type": "select", "proxies": ["proxy-a"]},),
        rules=({"match": "MATCH", "target": "BLOCK"},),
        dns={"enable": True},
        policy={"mode": "strict"},
        transport_materializations=(materialization(1, "A", {"content": "cache-a"}),),
        deployment_constants={"mode": "rule"},
    )


def test_same_snapshot_is_deterministic_and_secret_neutral() -> None:
    first, first_fingerprint = compose_mihomo_document(snapshot())
    second, second_fingerprint = compose_mihomo_document(snapshot())
    assert first == second
    assert first_fingerprint == second_fingerprint
    assert "secret" not in repr(first).lower()
    assert "token" not in first_fingerprint.lower()


def test_deleted_desired_entries_are_not_retained() -> None:
    document, _ = compose_mihomo_document(snapshot())
    assert document["listeners"] == [{"name": "listener-a", "listen": "127.0.0.1:10001"}]
    empty, _ = compose_mihomo_document(DesiredForwarderState())
    assert empty["listeners"] == []
    assert empty["proxies"] == []


@pytest.mark.parametrize(
    "mutator, expected",
    [
        (
            lambda state: DesiredForwarderState(
                transport_materializations=(object(),)
            ),
            "MATERIALIZATION_INVALID",
        ),
        (
            lambda state: DesiredForwarderState(
                transport_materializations=(
                    TransportMaterialization(
                        1,
                        "A",
                        1,
                        "cache/a",
                        "0",
                        datetime.now(UTC) + timedelta(minutes=1),
                        {"content": "x"},
                    ),
                )
            ),
            "CACHE_HASH_MISMATCH",
        ),
        (
            lambda state: DesiredForwarderState(
                transport_materializations=(
                    TransportMaterialization(
                        1,
                        "A",
                        1,
                        "cache/a",
                        "0",
                        datetime.now(UTC) - timedelta(seconds=1),
                        {},
                    ),
                )
            ),
            "CACHE_STALE",
        ),
    ],
)
def test_transport_materialization_fail_closed(mutator: object, expected: str) -> None:
    with pytest.raises(MihomoProjectionError, match=expected):
        compose_mihomo_document(mutator(snapshot()))  # type: ignore[operator]


def test_provider_render_uses_desired_snapshot_not_external_renderer() -> None:
    def forbidden_renderer() -> bytes:
        raise AssertionError("external renderer must not be called")

    provider = MihomoForwarderProvider(forbidden_renderer, runtime=object())
    candidate = provider.render(snapshot())
    assert candidate.content["listeners"][0]["name"] == "listener-a"
    assert candidate.version == compose_mihomo_document(snapshot())[1]
