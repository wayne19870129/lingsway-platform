from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from backend.app.infra import credential_resolver
from backend.app.models import EgressEndpoint
from backend.app.providers.base import (
    CandidateConfig,
    CredentialDTO,
    DesiredForwarderState,
    EgressCredentialSnapshot,
    ForwarderEgressProxyDTO,
    ForwarderListenerDTO,
    ProjectionTemplate,
)
from backend.app.providers.forwarder.mihomo import (
    ControllerSecretSnapshot,
    MihomoCandidateConfig,
    MihomoFinalizationResolvers,
    MihomoForwarderProvider,
    MihomoRuntime,
    MihomoRuntimeError,
)
from backend.app.providers.forwarder.mihomo_projection import (
    MihomoProjectionError,
    TransportMaterialization,
    TransportMaterializationReference,
    compose_mihomo_document,
)
from backend.tests.unit.test_mihomo_reconciliation import seed_mihomo_loader_graph


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


class Resolver:
    def __init__(self, snapshot: ControllerSecretSnapshot) -> None:
        self.snapshot = snapshot
        self.refs: list[str] = []
        self.controller = self
        self.egress = self

    def resolve(self, secret_ref: str) -> ControllerSecretSnapshot:
        self.refs.append(secret_ref)
        return self.snapshot

    def resolve_egress_credential(self, secret_ref: str) -> EgressCredentialSnapshot:
        raise AssertionError(f"unexpected egress credential resolution: {secret_ref}")


class EgressResolver:
    def __init__(self, snapshot: EgressCredentialSnapshot) -> None:
        self.snapshot = snapshot

    def resolve_egress_credential(self, _secret_ref: str) -> EgressCredentialSnapshot:
        return self.snapshot


def snapshot() -> DesiredForwarderState:
    return DesiredForwarderState(
        listener_specs=(ForwarderListenerDTO("listener-a", "socks", "127.0.0.1", 10001, "BLOCK"),),
        proxies=({"name": "proxy-a", "type": "ss", "server": "example.invalid"},),
        proxy_groups=({"name": "AUTO", "type": "select", "proxies": ["proxy-a"]},),
        rules=({"match": "MATCH", "target": "BLOCK"},),
        dns={"enable": True},
        transport_materializations=(
            materialization(
                1,
                "A",
                {"proxies": [{"name": "materialized-a", "type": "ss"}]},
            ),
        ),
        transport_references=(TransportMaterializationReference(1, "A", 1, "cache/1"),),
        deployment_constants={
            "mode": "rule",
            "external-controller": "172.30.0.10:9090",
            "api-secret-ref": "mihomo/api-secret",
            "api-secret-revision": 1,
        },
    )


def mihomo_constants(**overrides: object) -> dict[str, object]:
    return {
        "external-controller": "172.30.0.10:9090",
        "api-secret-ref": "mihomo/api-secret",
        "api-secret-revision": 1,
        **overrides,
    }


def egress_snapshot() -> DesiredForwarderState:
    return replace(
        snapshot(),
        egress_proxies=(
            ForwarderEgressProxyDTO(
                name="egress-a",
                protocol="socks5",
                host="192.0.2.10",
                port=1080,
                credential_secret_ref="egress/ref",
                credential_revision=3,
                transport_proxy_name="materialized-a",
                transport_node_identity=("materialized-a", "192.0.2.20", 443),
            ),
        ),
    )


def test_same_snapshot_is_deterministic_and_secret_neutral() -> None:
    state = snapshot()
    first, first_fingerprint = compose_mihomo_document(state)
    second, second_fingerprint = compose_mihomo_document(state)
    assert first == second
    assert first_fingerprint == second_fingerprint
    assert "S04A_SECRET_SENTINEL_DO_NOT_LEAK" not in repr(first)
    assert "token" not in first_fingerprint.lower()
    assert "S04A_SECRET_SENTINEL_DO_NOT_LEAK" not in repr(
        materialization(1, "A", {"secret": "S04A_SECRET_SENTINEL_DO_NOT_LEAK"})
    )


def test_canonical_mihomo_document_contract() -> None:
    document, _ = compose_mihomo_document(snapshot())
    assert set(document) == {
        "ipv6",
        "unified-delay",
        "tcp-concurrent",
        "mixed-port",
        "allow-lan",
        "bind-address",
        "mode",
        "log-level",
        "external-controller",
        "listeners",
        "proxies",
        "proxy-groups",
        "rules",
        "dns",
    }
    document, _ = compose_mihomo_document(snapshot())
    assert "api-secret-ref" not in document
    assert "secret-ref" not in document
    assert document["mode"] == "rule"
    assert document["external-controller"] == "172.30.0.10:9090"
    assert document["rules"] == ["MATCH,REJECT"]
    listeners = cast(list[dict[str, object]], document["listeners"])
    assert listeners[0]["port"] == 10001
    assert listeners[0]["proxy"] == "REJECT"


def test_dns_enable_requires_boolean() -> None:
    invalid = DesiredForwarderState(
        listener_specs=snapshot().listener_specs,
        proxies=snapshot().proxies,
        proxy_groups=snapshot().proxy_groups,
        rules=snapshot().rules,
        dns={"enable": "true"},
        transport_materializations=snapshot().transport_materializations,
        transport_references=snapshot().transport_references,
        deployment_constants=mihomo_constants(),
    )
    with pytest.raises(MihomoProjectionError, match="MIHOMO_DNS_INVALID"):
        compose_mihomo_document(invalid)


@pytest.mark.parametrize(
    ("kind", "value", "expected"),
    [
        ("DOMAIN", "example.com", "DOMAIN,example.com,REJECT"),
        ("DOMAIN-SUFFIX", "example.com", "DOMAIN-SUFFIX,example.com,REJECT"),
        ("IP-CIDR", "192.0.2.0/24", "IP-CIDR,192.0.2.0/24,REJECT"),
    ],
)
def test_supported_rule_kinds_serialize_canonically(kind: str, value: str, expected: str) -> None:
    state = DesiredForwarderState(
        listener_specs=(ForwarderListenerDTO("listener", "socks", "127.0.0.1", 7891, "BLOCK"),),
        rules=(
            {"match": kind, "value": value, "target": "BLOCK"},
            {"match": "MATCH", "target": "BLOCK"},
        ),
        deployment_constants=mihomo_constants(),
    )
    document, _ = compose_mihomo_document(state)
    assert document["rules"] == [expected, "MATCH,REJECT"]


def test_unsupported_rule_kind_fails_closed() -> None:
    state = DesiredForwarderState(
        listener_specs=(ForwarderListenerDTO("listener", "socks", "127.0.0.1", 7891, "BLOCK"),),
        rules=({"match": "TYPO", "value": "x", "target": "BLOCK"},),
        deployment_constants=mihomo_constants(),
    )
    with pytest.raises(MihomoProjectionError, match="RULE_TYPE_UNSUPPORTED"):
        compose_mihomo_document(state)


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("DOMAIN", "example..com"),
        ("DOMAIN-SUFFIX", ".example.com"),
        ("DOMAIN", "example,com"),
        ("DOMAIN-SUFFIX", "example\n.com"),
        ("IP-CIDR", "192.0.2.999/24"),
    ],
)
def test_malformed_rule_values_fail_closed(kind: str, value: str) -> None:
    state = DesiredForwarderState(
        listener_specs=(ForwarderListenerDTO("listener", "socks", "127.0.0.1", 7891, "BLOCK"),),
        rules=(
            {"match": kind, "value": value, "target": "BLOCK"},
            {"match": "MATCH", "target": "BLOCK"},
        ),
        deployment_constants=mihomo_constants(),
    )
    with pytest.raises(MihomoProjectionError, match="RULE_VALUE_INVALID"):
        compose_mihomo_document(state)


def test_controller_identity_is_required_by_composer() -> None:
    state = snapshot()
    for constants in (
        {},
        {"api-secret-ref": "   ", "api-secret-revision": 1},
        {"api-secret-ref": "mihomo/api-secret"},
        {"api-secret-ref": "mihomo/api-secret", "api-secret-revision": True},
    ):
        invalid = DesiredForwarderState(
            listener_specs=state.listener_specs,
            proxies=state.proxies,
            proxy_groups=state.proxy_groups,
            rules=state.rules,
            dns=state.dns,
            transport_materializations=state.transport_materializations,
            transport_references=state.transport_references,
            deployment_constants={"external-controller": "172.30.0.10:9090", **constants},
        )
        with pytest.raises(MihomoProjectionError):
            compose_mihomo_document(invalid)


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("block", "BLOCK_TARGET_INVALID"),
        ("Block", "BLOCK_TARGET_INVALID"),
        (" BLOCK ", "BLOCK_TARGET_INVALID"),
    ],
)
def test_logical_block_requires_exact_canonical_spelling(target: str, expected: str) -> None:
    invalid = DesiredForwarderState(
        listener_specs=(ForwarderListenerDTO("listener", "socks", "127.0.0.1", 10001, target),),
        rules=({"match": "MATCH", "target": target},),
        deployment_constants=mihomo_constants(),
    )
    with pytest.raises(MihomoProjectionError, match=expected):
        compose_mihomo_document(invalid)


@pytest.mark.parametrize(
    "name",
    [
        "DIRECT",
        "direct",
        "REJECT",
        "REJECT-DROP",
        "PASS",
        "PASS-RULE",
        "COMPATIBLE",
        "BLOCK",
        "block",
    ],
)
def test_mihomo_builtin_and_logical_identities_are_reserved(name: str) -> None:
    with pytest.raises(MihomoProjectionError, match="RESERVED_TARGET_IDENTITY") as error:
        compose_mihomo_document(
            DesiredForwarderState(
                listener_specs=(
                    ForwarderListenerDTO("listener", "socks", "127.0.0.1", 10001, "BLOCK"),
                ),
                proxies=({"name": name, "type": "ss"},),
                rules=({"match": "MATCH", "target": "BLOCK"},),
                deployment_constants={
                    "external-controller": "172.30.0.10:9090",
                    "api-secret-ref": "mihomo/api-secret",
                    "api-secret-revision": 1,
                },
            )
        )
    assert name not in str(error.value)


@pytest.mark.parametrize("name", ["REJECT", "block"])
def test_mihomo_builtin_identity_is_reserved_for_groups(name: str) -> None:
    with pytest.raises(MihomoProjectionError, match="RESERVED_TARGET_IDENTITY"):
        compose_mihomo_document(
            DesiredForwarderState(
                listener_specs=(
                    ForwarderListenerDTO("listener", "socks", "127.0.0.1", 10001, "BLOCK"),
                ),
                proxies=({"name": "proxy", "type": "ss"},),
                proxy_groups=({"name": name, "type": "select", "proxies": ("proxy",)},),
                rules=({"match": "MATCH", "target": "BLOCK"},),
                deployment_constants={
                    "external-controller": "172.30.0.10:9090",
                    "api-secret-ref": "mihomo/api-secret",
                    "api-secret-revision": 1,
                },
            )
        )


@pytest.mark.parametrize(
    ("constants", "expected"),
    [
        ({"mode": "direct"}, "MODE_INVALID"),
        ({"mode": "global"}, "MODE_INVALID"),
        ({"mode": "unexpected"}, "MODE_INVALID"),
        ({"mixed-port": 0}, "MIXED_PORT_INVALID"),
        ({"mixed-port": True}, "MIXED_PORT_INVALID"),
        ({"mixed-port": 65536}, "MIXED_PORT_INVALID"),
        ({"allow-lan": "true"}, "ALLOW_LAN_INVALID"),
        ({"log-level": "trace"}, "LOG_LEVEL_INVALID"),
    ],
)
def test_deployment_constants_are_validated_fail_closed(
    constants: dict[str, object], expected: str
) -> None:
    state = snapshot()
    invalid = DesiredForwarderState(
        listener_specs=state.listener_specs,
        proxies=state.proxies,
        proxy_groups=state.proxy_groups,
        rules=state.rules,
        dns=state.dns,
        transport_materializations=state.transport_materializations,
        transport_references=state.transport_references,
        deployment_constants={
            **dict(state.deployment_constants),
            **constants,
        },
    )
    with pytest.raises(MihomoProjectionError, match=expected):
        compose_mihomo_document(invalid)


def test_mixed_port_listener_collision_fails_closed() -> None:
    state = snapshot()
    invalid = DesiredForwarderState(
        listener_specs=state.listener_specs,
        proxies=state.proxies,
        proxy_groups=state.proxy_groups,
        rules=state.rules,
        dns=state.dns,
        transport_materializations=state.transport_materializations,
        transport_references=state.transport_references,
        deployment_constants={
            **dict(state.deployment_constants),
            "mixed-port": 10001,
        },
    )
    with pytest.raises(MihomoProjectionError, match="LISTENER_PORT_COLLISION"):
        compose_mihomo_document(invalid)


@pytest.mark.parametrize("ref", [" mihomo/api-secret", "mihomo/api-secret ", "mihomo/secret\nref"])
def test_controller_secret_ref_must_be_canonical(ref: str) -> None:
    state = snapshot()
    invalid = DesiredForwarderState(
        listener_specs=state.listener_specs,
        proxies=state.proxies,
        proxy_groups=state.proxy_groups,
        rules=state.rules,
        dns=state.dns,
        transport_materializations=state.transport_materializations,
        transport_references=state.transport_references,
        deployment_constants={
            "external-controller": "172.30.0.10:9090",
            "api-secret-ref": ref,
            "api-secret-revision": 1,
        },
    )
    with pytest.raises(MihomoProjectionError, match="CONTROLLER_SECRET_REF_INVALID"):
        compose_mihomo_document(invalid)


@pytest.mark.parametrize(
    "domain",
    [
        "münich.example",
        "a" * 64 + ".example",
        ".".join(["a" * 63] * 5),
    ],
)
def test_domain_canonicalization_is_ascii_and_bounded(domain: str) -> None:
    state = DesiredForwarderState(
        listener_specs=(ForwarderListenerDTO("listener", "socks", "127.0.0.1", 10001, "BLOCK"),),
        rules=(
            {"match": "DOMAIN", "value": domain, "target": "BLOCK"},
            {"match": "MATCH", "target": "BLOCK"},
        ),
        deployment_constants=mihomo_constants(),
    )
    with pytest.raises(MihomoProjectionError, match="RULE_VALUE_INVALID"):
        compose_mihomo_document(state)


def test_ascii_domain_is_lowercased_deterministically() -> None:
    state = DesiredForwarderState(
        listener_specs=(ForwarderListenerDTO("listener", "socks", "127.0.0.1", 10001, "BLOCK"),),
        rules=(
            {"match": "DOMAIN", "value": "EXAMPLE.COM", "target": "BLOCK"},
            {"match": "MATCH", "target": "BLOCK"},
        ),
        deployment_constants=mihomo_constants(),
    )
    document, _ = compose_mihomo_document(state)
    assert document["rules"] == ["DOMAIN,example.com,REJECT", "MATCH,REJECT"]


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1:9090",
        "0.0.0.0:9090",
        "mihomo:9090",
        "172.30.0.10:0",
        "172.30.0.10:65536",
        " 172.30.0.10:9090",
        "172.30.0.10:9090 ",
        "172.30.0.10:90\n90",
    ],
)
def test_controller_address_is_reachable_and_canonical(address: str) -> None:
    state = snapshot()
    invalid = DesiredForwarderState(
        listener_specs=state.listener_specs,
        proxies=state.proxies,
        proxy_groups=state.proxy_groups,
        rules=state.rules,
        dns=state.dns,
        transport_materializations=state.transport_materializations,
        transport_references=state.transport_references,
        deployment_constants={
            **dict(state.deployment_constants),
            "external-controller": address,
        },
    )
    with pytest.raises(MihomoProjectionError, match="CONTROLLER_ADDRESS_INVALID"):
        compose_mihomo_document(invalid)


def test_controller_address_is_required() -> None:
    state = snapshot()
    invalid_constants = dict(state.deployment_constants)
    invalid_constants.pop("external-controller")
    invalid = DesiredForwarderState(
        listener_specs=state.listener_specs,
        proxies=state.proxies,
        proxy_groups=state.proxy_groups,
        rules=state.rules,
        dns=state.dns,
        transport_materializations=state.transport_materializations,
        transport_references=state.transport_references,
        deployment_constants=invalid_constants,
    )
    with pytest.raises(MihomoProjectionError, match="CONTROLLER_ADDRESS_INVALID"):
        compose_mihomo_document(invalid)


def test_transport_proof_changes_projection_identity() -> None:
    state = snapshot()
    _, baseline = compose_mihomo_document(state)
    changed_hash = materialization(1, "A", {"proxies": [{"name": "changed", "type": "ss"}]})
    changed = DesiredForwarderState(
        listener_specs=state.listener_specs,
        proxies=state.proxies,
        proxy_groups=state.proxy_groups,
        rules=state.rules,
        dns=state.dns,
        transport_materializations=(changed_hash,),
        transport_references=state.transport_references,
        deployment_constants=state.deployment_constants,
        snapshot_revision=2,
        snapshot_identity="next",
    )
    _, changed_fingerprint = compose_mihomo_document(changed)
    assert changed_fingerprint != baseline


@pytest.mark.parametrize(
    "proxies, groups, expected",
    [
        (({"name": "same", "type": "ss"}, {"name": "same", "type": "ss"}), (), "DUPLICATE_PROXY"),
        (
            ({"name": "proxy", "type": "ss"},),
            (
                {"name": "same", "type": "select", "proxies": ("proxy",)},
                {"name": "same", "type": "select", "proxies": ("proxy",)},
            ),
            "DUPLICATE_PROXY_GROUP",
        ),
        (
            ({"name": "same", "type": "ss"},),
            ({"name": "same", "type": "select", "proxies": ("same",)},),
            "IDENTITY_COLLISION",
        ),
    ],
)
def test_proxy_and_group_identity_is_unambiguous(
    proxies: tuple[dict[str, object], ...],
    groups: tuple[dict[str, object], ...],
    expected: str,
) -> None:
    state = DesiredForwarderState(
        listener_specs=(ForwarderListenerDTO("listener", "socks", "127.0.0.1", 7891, "BLOCK"),),
        proxies=proxies,
        proxy_groups=groups,
        rules=({"match": "MATCH", "target": "BLOCK"},),
        deployment_constants=mihomo_constants(),
    )
    with pytest.raises(MihomoProjectionError, match=expected):
        compose_mihomo_document(state)


def test_transport_reference_must_match_materialization_exactly() -> None:
    state = snapshot()
    mismatched = DesiredForwarderState(
        listener_specs=state.listener_specs,
        proxies=state.proxies,
        proxy_groups=state.proxy_groups,
        rules=state.rules,
        dns=state.dns,
        transport_materializations=state.transport_materializations,
        transport_references=(TransportMaterializationReference(9, "A", 1, "cache/1"),),
    )
    with pytest.raises(MihomoProjectionError, match="OWNERSHIP_MISMATCH"):
        compose_mihomo_document(mismatched)


def test_transport_reference_owners_are_one_to_one() -> None:
    first = materialization(1, "A", {"proxies": []})
    second = materialization(2, "B", {"proxies": []})
    state = DesiredForwarderState(
        rules=({"match": "MATCH", "target": "BLOCK"},),
        transport_materializations=(first, second),
        transport_references=(TransportMaterializationReference(1, "A", 1, "cache/1"),) * 2,
    )
    with pytest.raises(MihomoProjectionError, match="DUPLICATE_TRANSPORT_REFERENCE"):
        compose_mihomo_document(state)


def test_materialized_content_is_rendered_into_candidate() -> None:
    document, _ = compose_mihomo_document(snapshot())
    proxies = document["proxies"]
    assert isinstance(proxies, list)
    proxy_items = cast(list[dict[str, object]], proxies)
    assert {item["name"] for item in proxy_items} >= {"materialized-a"}


@pytest.mark.parametrize(
    "rules, expected",
    [
        (({"match": "MATCH", "target": "DIRECT"},), "DIRECT_FALLBACK"),
        ((), "FALLBACK_RULE_INVALID"),
        (
            ({"match": "MATCH", "target": "BLOCK"}, {"match": "MATCH", "target": "BLOCK"}),
            "FALLBACK_RULE_INVALID",
        ),
    ],
)
def test_fallback_policy_is_fail_closed(
    rules: tuple[dict[str, object], ...], expected: str
) -> None:
    with pytest.raises(MihomoProjectionError, match=expected):
        compose_mihomo_document(
            DesiredForwarderState(
                listener_specs=(
                    ForwarderListenerDTO("listener", "socks", "127.0.0.1", 7891, "BLOCK"),
                ),
                rules=rules,
                transport_materializations=(materialization(1, "A", {"proxies": []}),),
                transport_references=(TransportMaterializationReference(1, "A", 1, "cache/1"),),
                deployment_constants={
                    "external-controller": "172.30.0.10:9090",
                    "api-secret-ref": "mihomo/api-secret",
                    "api-secret-revision": 1,
                },
            )
        )


def test_snapshot_repr_redacts_nested_content() -> None:
    state = DesiredForwarderState(proxies=({"password": "S04A_SECRET_SENTINEL_DO_NOT_LEAK"},))
    assert "S04A_SECRET_SENTINEL_DO_NOT_LEAK" not in repr(state)


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
    assert document["listeners"] == [
        {
            "name": "listener-a",
            "type": "socks",
            "port": 10001,
            "proxy": "REJECT",
            "listen": "127.0.0.1",
        }
    ]
    with pytest.raises(MihomoProjectionError, match="LISTENER_SPEC_REQUIRED"):
        compose_mihomo_document(
            DesiredForwarderState(
                deployment_constants={
                    "external-controller": "172.30.0.10:9090",
                    "api-secret-ref": "mihomo/api-secret",
                    "api-secret-revision": 1,
                }
            )
        )


@pytest.mark.parametrize(
    "mutator, expected",
    [
        (
            lambda state: DesiredForwarderState(transport_materializations=(object(),)),
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


def test_mihomo_document_contains_egress_transport_binding() -> None:
    state = replace(
        egress_snapshot(),
        listener_specs=(
            ForwarderListenerDTO("listener-a", "socks", "127.0.0.1", 10001, "egress-a"),
        ),
    )
    document, _ = compose_mihomo_document(state)
    proxies = cast(list[dict[str, object]], document["proxies"])
    egress = next(proxy for proxy in proxies if proxy["name"] == "egress-a")
    assert egress["dialer-proxy"] == "materialized-a"
    listeners = cast(list[dict[str, object]], document["listeners"])
    assert listeners[0]["proxy"] == "egress-a"

    without_assignment = replace(
        egress_snapshot(),
        egress_proxies=(
            replace(
                egress_snapshot().egress_proxies[0],
                transport_proxy_name=None,
                transport_node_identity=None,
            ),
        ),
    )
    document_without, _ = compose_mihomo_document(without_assignment)
    egress_without = next(
        proxy for proxy in cast(list[dict[str, object]], document_without["proxies"])
        if proxy["name"] == "egress-a"
    )
    assert "dialer-proxy" not in egress_without


def test_egress_proxy_name_collision_fails_closed() -> None:
    with pytest.raises(MihomoProjectionError, match="MIHOMO_DUPLICATE_PROXY_IDENTITY"):
        compose_mihomo_document(
            replace(
                egress_snapshot(),
                egress_proxies=(
                    replace(egress_snapshot().egress_proxies[0], name="materialized-a"),
                ),
            )
        )


def test_unsupported_egress_protocol_fails_closed() -> None:
    with pytest.raises(MihomoProjectionError, match="MIHOMO_EGRESS_PROTOCOL_UNSUPPORTED"):
        compose_mihomo_document(
            replace(
                egress_snapshot(),
                egress_proxies=(replace(egress_snapshot().egress_proxies[0], protocol="http"),),
            )
        )


def test_finalization_resolves_egress_credential_at_provider_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = MihomoForwarderProvider(runtime=cast(MihomoRuntime, object()))
    engine, session, loader = seed_mihomo_loader_graph(tmp_path)

    def fail_plaintext(*_args: object, **_kwargs: object) -> str:
        pytest.fail("egress plaintext must not be resolved during loading")

    monkeypatch.setattr(credential_resolver, "decrypt_secret", fail_plaintext)
    endpoint = session.get(EgressEndpoint, 1)
    assert endpoint is not None
    endpoint.protocol = "socks5"
    session.commit()
    state = loader.load_current(session)
    state = replace(state, transport_materializations=(), transport_references=())
    session.close()
    engine.dispose()
    template = provider.render(state)
    assert template.egress_credentials[0].proxy_name == "egress-a"
    assert template.egress_credentials[0].secret_ref == "egress/a"
    assert template.egress_credentials[0].revision == 3
    assert "username" not in repr(template.content)
    assert "password" not in repr(template.content)
    candidate = provider.finalize(
        template,
        MihomoFinalizationResolvers(
                controller=Resolver(ControllerSecretSnapshot("mihomo/api-secret", 7, "controller")),
            egress=EgressResolver(
                EgressCredentialSnapshot("egress/a", 3, CredentialDTO("user", "pass"))
            ),
        ),
    )
    proxy = next(
        item for item in cast(tuple[Mapping[str, object], ...], candidate.content["proxies"])
        if item["name"] == "egress-a"
    )
    assert proxy["username"] == "user"
    assert proxy["password"] == "pass"


def test_provider_render_uses_desired_snapshot_not_external_renderer() -> None:
    provider = MihomoForwarderProvider(runtime=cast(MihomoRuntime, object()))
    state = snapshot()
    candidate = provider.render(state)
    listeners = cast(list[dict[str, object]], candidate.content["listeners"])
    assert listeners[0]["name"] == "listener-a"
    assert candidate.version == compose_mihomo_document(state)[1]


def test_template_secret_identity_and_finalization_boundary() -> None:
    provider = MihomoForwarderProvider(runtime=cast(MihomoRuntime, object()))
    template = provider.render(snapshot())
    assert isinstance(template, ProjectionTemplate)
    assert template.controller_secret_ref == "mihomo/api-secret"
    assert template.controller_secret_revision == 1
    assert "S04A_SECRET_SENTINEL_DO_NOT_LEAK" not in repr(template)
    resolver = Resolver(
        ControllerSecretSnapshot("mihomo/api-secret", 1, "S04A_SECRET_SENTINEL_DO_NOT_LEAK")
    )
    candidate = provider.finalize(
        template, MihomoFinalizationResolvers(controller=resolver, egress=resolver)
    )
    assert resolver.refs == ["mihomo/api-secret"]
    assert isinstance(candidate, MihomoCandidateConfig)
    assert candidate.content["secret"] == "S04A_SECRET_SENTINEL_DO_NOT_LEAK"
    assert "S04A_SECRET_SENTINEL_DO_NOT_LEAK" not in repr(candidate)


def test_finalization_identity_and_blank_secret_fail_closed() -> None:
    provider = MihomoForwarderProvider(runtime=cast(MihomoRuntime, object()))
    template = provider.render(snapshot())
    with pytest.raises(MihomoRuntimeError, match="identity mismatch"):
        provider.finalize(
            template,
            MihomoFinalizationResolvers(
                controller=Resolver(ControllerSecretSnapshot("other", 1, "secret")),
                egress=Resolver(ControllerSecretSnapshot("other", 1, "secret")),
            ),
        )
    with pytest.raises(MihomoRuntimeError, match="resolution failed"):
        provider.finalize(
            template,
            MihomoFinalizationResolvers(
                controller=Resolver(ControllerSecretSnapshot("mihomo/api-secret", 1, "   ")),
                egress=Resolver(ControllerSecretSnapshot("mihomo/api-secret", 1, "   ")),
            ),
        )


def test_missing_secret_revision_fails_closed() -> None:
    provider = MihomoForwarderProvider(runtime=cast(MihomoRuntime, object()))
    state = DesiredForwarderState(
        listener_specs=(ForwarderListenerDTO("listener", "socks", "127.0.0.1", 7891, "BLOCK"),),
        rules=({"match": "MATCH", "target": "BLOCK"},),
        deployment_constants={
            "external-controller": "172.30.0.10:9090",
            "api-secret-ref": "mihomo/api-secret",
        },
    )
    with pytest.raises(MihomoProjectionError, match="SECRET_REVISION_INVALID"):
        provider.render(state)


def test_finalized_candidate_content_is_immutable() -> None:
    provider = MihomoForwarderProvider(runtime=cast(MihomoRuntime, object()))
    template = provider.render(snapshot())
    candidate = provider.finalize(
        template,
        MihomoFinalizationResolvers(
            controller=Resolver(ControllerSecretSnapshot("mihomo/api-secret", 1, "runtime-secret")),
            egress=Resolver(ControllerSecretSnapshot("mihomo/api-secret", 1, "runtime-secret")),
        ),
    )
    with pytest.raises(TypeError):
        candidate.content["secret"] = "changed"  # type: ignore[index]


def test_generic_candidate_and_template_cannot_be_applied() -> None:
    class Runtime:
        def controller_secret_matches(self, secret: str) -> bool:
            return False

        def backup(self) -> object:
            raise AssertionError("backup must not run")

    provider = MihomoForwarderProvider(runtime=cast(MihomoRuntime, Runtime()))
    with pytest.raises(MihomoRuntimeError, match="non-finalized"):
        provider.apply(cast(MihomoCandidateConfig, CandidateConfig({}, "v1")))
    with pytest.raises(MihomoRuntimeError, match="non-finalized"):
        provider.apply(cast(MihomoCandidateConfig, provider.render(snapshot())))
