from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from backend.app.providers.base import DesiredForwarderState
from backend.app.providers.forwarder.mihomo import MihomoForwarderProvider, MihomoRuntime
from backend.app.providers.forwarder.mihomo_projection import (
    MihomoProjectionError,
    TransportMaterialization,
    TransportMaterializationReference,
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
        transport_references=(TransportMaterializationReference(1, "A", 1, "cache/1"),),
        deployment_constants={"mode": "rule"},
    )


def test_same_snapshot_is_deterministic_and_secret_neutral() -> None:
    state = snapshot()
    first, first_fingerprint = compose_mihomo_document(state)
    second, second_fingerprint = compose_mihomo_document(state)
    assert first == second
    assert first_fingerprint == second_fingerprint
    assert "secret" not in repr(first).lower()
    assert "token" not in first_fingerprint.lower()
    assert "S04A_SECRET_SENTINEL_DO_NOT_LEAK" not in repr(
        materialization(1, "A", {"secret": "S04A_SECRET_SENTINEL_DO_NOT_LEAK"})
    )


def test_transport_proof_changes_projection_identity() -> None:
    state = snapshot()
    _, baseline = compose_mihomo_document(state)
    changed_hash = materialization(1, "A", {"content": "different"})
    changed = DesiredForwarderState(
        listeners=state.listeners,
        proxies=state.proxies,
        proxy_groups=state.proxy_groups,
        rules=state.rules,
        dns=state.dns,
        policy=state.policy,
        transport_materializations=(changed_hash,),
        transport_references=state.transport_references,
        deployment_constants=state.deployment_constants,
        snapshot_revision=2,
        snapshot_identity="next",
    )
    _, changed_fingerprint = compose_mihomo_document(changed)
    assert changed_fingerprint != baseline


def test_transport_reference_must_match_materialization_exactly() -> None:
    state = snapshot()
    mismatched = DesiredForwarderState(
        listeners=state.listeners,
        proxies=state.proxies,
        proxy_groups=state.proxy_groups,
        rules=state.rules,
        dns=state.dns,
        policy=state.policy,
        transport_materializations=state.transport_materializations,
        transport_references=(TransportMaterializationReference(9, "A", 1, "cache/1"),),
    )
    with pytest.raises(MihomoProjectionError, match="PROOF_MISMATCH"):
        compose_mihomo_document(mismatched)


def test_snapshot_defensively_freezes_nested_input() -> None:
    listeners = {"listener": "127.0.0.1:1"}
    proxy = {"name": "p", "type": "ss"}
    state = DesiredForwarderState(listeners=listeners, proxies=(proxy,))
    listeners["other"] = "127.0.0.1:2"
    proxy["server"] = "secret-sentinel"
    assert "other" not in state.listeners
    assert "server" not in state.proxies[0]
    with pytest.raises(TypeError):
        state.listeners["x"] = "y"  # type: ignore[index]


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
    provider = MihomoForwarderProvider(runtime=cast(MihomoRuntime, object()))
    state = snapshot()
    candidate = provider.render(state)
    listeners = cast(list[dict[str, object]], candidate.content["listeners"])
    assert listeners[0]["name"] == "listener-a"
    assert candidate.version == compose_mihomo_document(state)[1]
