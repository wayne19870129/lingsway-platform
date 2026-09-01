"""Render the complete Mihomo configuration from database state.

This is the migrated renderer from the legacy application.  It deliberately
reads the complete topology on every render; it never appends an incremental
fragment to the current file.
"""

from __future__ import annotations

import ipaddress
import json
import os
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass

import yaml
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from backend.app.core.secrets import reveal_secret
from backend.app.models import (
    BindingRole,
    EgressBinding,
    EgressEndpoint,
    RouteBinding,
    RouteGroup,
    TrafficRule,
    TransportProviderKind,
    TransportProviderRecord,
)

RUNTIME_REQUIRED = ("MIHOMO_API_SECRET",)

ROLE_ORDER = {
    BindingRole.PRIMARY: 0,
    BindingRole.BACKUP: 1,
    BindingRole.CANDIDATE: 2,
}


@dataclass(frozen=True, slots=True)
class ProviderSource:
    id: int
    code: str
    slug: str

    @property
    def mihomo_name(self) -> str:
        return f"transport-provider-{self.slug}"


@dataclass(frozen=True, slots=True)
class RouteSelection:
    id: int
    code: str
    provider_ids: tuple[int, ...]

    @property
    def mihomo_name(self) -> str:
        stable_code = self.code.lower().replace("_", "-")
        return f"transport-route-{stable_code}"


@dataclass(frozen=True, slots=True)
class TrafficRuleConfig:
    id: int
    rule_set: str
    match_type: str
    match_value: str
    target_egress: str
    priority: int


@dataclass(frozen=True, slots=True)
class EgressSource:
    id: int
    code: str
    protocol: str
    host: str
    port: int
    username: str
    password: str
    listen_port: int

    @property
    def mihomo_name(self) -> str:
        stable_code = self.code.lower().replace("_", "-")
        return f"egress-{stable_code}"

    @property
    def listener_name(self) -> str:
        return f"listener-{self.mihomo_name}"


def require_runtime_environment() -> dict[str, str]:
    values = {name: os.environ.get(name, "") for name in RUNTIME_REQUIRED}
    missing = [name for name, value in values.items() if not value or value == "CHANGE_ME"]
    if missing:
        raise RuntimeError(f"Missing runtime secrets: {', '.join(missing)}")
    values["MIHOMO_INTERNAL_LISTEN_ADDRESS"] = os.environ.get(
        "MIHOMO_INTERNAL_LISTEN_ADDRESS", "172.30.0.10"
    )
    values["MIHOMO_EGRESS_ROUTE_MODE"] = os.environ.get("MIHOMO_EGRESS_ROUTE_MODE", "B")
    return values


def load_transport_topology(
    db: Session,
) -> tuple[list[ProviderSource], list[RouteSelection], list[TrafficRuleConfig], list[EgressSource]]:
    records = list(
        db.scalars(
            select(TransportProviderRecord)
            .where(TransportProviderRecord.enabled.is_(True))
            .order_by(TransportProviderRecord.id)
        )
    )
    unsupported = [item.code for item in records if item.kind != TransportProviderKind.SUBSCRIPTION]
    if unsupported:
        raise ValueError(
            "Enabled SELF_HOSTED providers are reserved but not implemented: "
            + ", ".join(unsupported)
        )
    providers = [ProviderSource(item.id, item.code, item.slug) for item in records]
    enabled_provider_ids = {item.id for item in records}

    rows = db.execute(
        select(RouteBinding, RouteGroup)
        .join(RouteGroup, RouteGroup.id == RouteBinding.route_group_id)
        .where(RouteBinding.enabled.is_(True))
        .order_by(RouteGroup.id, RouteBinding.priority, RouteBinding.id)
    ).all()
    grouped: dict[tuple[int, str], list[RouteBinding]] = defaultdict(list)
    for binding, route_group in rows:
        if binding.provider_id in enabled_provider_ids:
            grouped[(route_group.id, route_group.code)].append(binding)

    routes: list[RouteSelection] = []
    for (route_id, route_code), bindings in grouped.items():
        ordered = sorted(
            bindings,
            key=lambda item: (ROLE_ORDER[item.role], item.priority, item.id),
        )
        provider_ids = tuple(dict.fromkeys(item.provider_id for item in ordered))
        routes.append(RouteSelection(route_id, route_code, provider_ids))

    traffic_rules = [
        TrafficRuleConfig(
            item.id,
            item.rule_set,
            item.match_type,
            item.match_value,
            item.target_egress,
            item.priority,
        )
        for item in db.scalars(
            select(TrafficRule)
            .where(TrafficRule.enabled.is_(True))
            .order_by(TrafficRule.priority, TrafficRule.rule_set, TrafficRule.id)
        )
    ]

    egress_sources: list[EgressSource] = []
    egress_rows = db.execute(
        select(EgressEndpoint, EgressBinding)
        .outerjoin(
            EgressBinding,
            and_(
                EgressBinding.egress_id == EgressEndpoint.id,
                EgressBinding.released_at.is_(None),
            ),
        )
        .where(EgressEndpoint.status.in_(["AVAILABLE", "ASSIGNED", "DEGRADED"]))
        .order_by(EgressEndpoint.id)
    ).all()
    for item, binding in egress_rows:
        secret_ref = (
            (binding.credential_secret_ref if binding else None)
            or item.credential_secret_ref
        )
        raw = reveal_secret(db, secret_ref)
        try:
            credentials = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Egress credential secret {item.code} is not JSON") from exc
        if not isinstance(credentials, dict):
            raise ValueError(f"Egress credential secret {item.code} is not an object")
        egress_sources.append(
            EgressSource(
                id=item.id,
                code=item.code,
                protocol=item.protocol,
                host=item.host,
                port=item.port,
                username=str(credentials["username"]),
                password=str(credentials["password"]),
                listen_port=item.mihomo_listen_port,
            )
        )
    return providers, routes, traffic_rules, egress_sources


def _provider_config(source: ProviderSource) -> dict[str, object]:
    return {
        "type": "file",
        "path": f"./providers/{source.mihomo_name}.yaml",
        "health-check": {
            "enable": True,
            "url": "https://www.gstatic.com/generate_204",
            "interval": 60,
            "timeout": 5000,
            "lazy": False,
            "expected-status": 204,
        },
    }


def _build_egress_layer(
    egress_sources: list[EgressSource],
    default_transport_route: str,
    internal_listen_address: str,
    route_mode: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    try:
        listen_ip = ipaddress.ip_address(internal_listen_address)
    except ValueError as exc:
        raise ValueError("MIHOMO_INTERNAL_LISTEN_ADDRESS must be a valid IP address") from exc
    if listen_ip.is_unspecified or listen_ip.is_loopback or not listen_ip.is_private:
        raise ValueError("Mihomo Egress listeners must bind a private Docker network IP")
    proxies: list[dict[str, object]] = []
    groups: list[dict[str, object]] = []
    listeners: list[dict[str, object]] = []
    for source in egress_sources:
        proxy: dict[str, object] = {
            "name": source.mihomo_name,
            "type": source.protocol,
            "server": source.host,
            "port": source.port,
            "username": source.username,
            "password": source.password,
            "udp": False,
        }
        if route_mode.upper() == "A":
            proxy["dialer-proxy"] = default_transport_route
        elif route_mode.upper() != "B":
            raise ValueError("MIHOMO_EGRESS_ROUTE_MODE must be A or B")
        proxies.append(proxy)
        groups.append(
            {"name": source.listener_name, "type": "select", "proxies": [source.mihomo_name]}
        )
        listeners.append(
            {
                "name": source.listener_name,
                "type": "socks",
                "port": source.listen_port,
                "proxy": source.mihomo_name,
                "listen": internal_listen_address,
            }
        )
    return proxies, groups, listeners


def _render_traffic_rules(
    traffic_rules: list[TrafficRuleConfig],
    default_transport_route: str,
    residential_target: str,
) -> list[str]:
    if not traffic_rules:
        raise ValueError("At least one enabled traffic rule is required")
    targets = {"AIRPORT": default_transport_route, "RESIDENTIAL": residential_target}
    rendered: list[str] = []
    match_count = 0
    ordered = sorted(
        traffic_rules,
        key=lambda item: (item.match_type == "MATCH", item.priority, item.rule_set, item.id),
    )
    for rule in ordered:
        target = targets.get(rule.target_egress)
        if target is None:
            raise ValueError(f"Unsupported traffic target: {rule.target_egress}")
        if rule.match_type == "MATCH":
            match_count += 1
            rendered.append(f"MATCH,{target}")
        else:
            if not rule.match_value:
                raise ValueError(f"Rule {rule.id} requires a match value")
            rendered.append(f"{rule.match_type},{rule.match_value},{target}")
    if match_count != 1:
        raise ValueError("Exactly one enabled MATCH fallback rule is required")
    return rendered


def build_config(
    providers: list[ProviderSource],
    routes: list[RouteSelection],
    runtime: dict[str, str],
    traffic_rules: list[TrafficRuleConfig],
    egress_sources: list[EgressSource] | None = None,
) -> dict[str, object]:
    provider_by_id = {item.id: item for item in providers}
    for route in routes:
        missing = [item for item in route.provider_ids if item not in provider_by_id]
        if missing:
            raise ValueError(
                f"Route {route.code} references disabled or missing providers: {missing}"
            )

    proxy_providers = {
        source.mihomo_name: _provider_config(source)
        for source in sorted(providers, key=lambda item: item.id)
    }
    sorted_routes = sorted(routes, key=lambda item: item.id)
    proxy_groups: list[dict[str, object]] = []
    for route in sorted_routes:
        airport_group_name = f"{route.mihomo_name}-airport"
        provider_names = [provider_by_id[item].mihomo_name for item in route.provider_ids]
        proxy_groups.append(
            {
                "name": airport_group_name,
                "type": "url-test",
                "use": provider_names,
                "url": "https://www.gstatic.com/generate_204",
                "interval": 60,
                "lazy": False,
                "timeout": 5000,
                "max-failed-times": 2,
                "expected-status": 204,
            }
        )
        proxy_groups.append(
            {
                "name": route.mihomo_name,
                "type": "fallback",
                "proxies": [airport_group_name, "DIRECT"],
                "url": "https://www.gstatic.com/generate_204",
                "interval": 60,
                "lazy": False,
                "timeout": 5000,
                "max-failed-times": 2,
                "expected-status": 204,
            }
        )

    if egress_sources is None:
        egress_sources = [
            EgressSource(
                id=0,
                code="EGRESS_US_01",
                protocol=runtime["RESIDENTIAL_PROXY_01_PROTOCOL"],
                host=runtime["RESIDENTIAL_PROXY_01_HOST"],
                port=int(runtime["RESIDENTIAL_PROXY_01_PORT"]),
                username=runtime["RESIDENTIAL_PROXY_01_USERNAME"],
                password=runtime["RESIDENTIAL_PROXY_01_PASSWORD"],
                listen_port=7891,
            )
        ]
    if not egress_sources:
        raise ValueError("At least one available dedicated egress is required")
    default_route = sorted_routes[0].mihomo_name if sorted_routes else egress_sources[0].mihomo_name
    proxies, egress_groups, listeners = _build_egress_layer(
        egress_sources,
        default_route,
        runtime["MIHOMO_INTERNAL_LISTEN_ADDRESS"],
        runtime.get("MIHOMO_EGRESS_ROUTE_MODE", "B"),
    )
    rules = _render_traffic_rules(traffic_rules, default_route, egress_sources[0].mihomo_name)
    return {
        "ipv6": False,
        "unified-delay": True,
        "tcp-concurrent": True,
        "mixed-port": 7890,
        "allow-lan": True,
        "bind-address": "0.0.0.0",
        "mode": "rule",
        "log-level": "warning",
        "dns": {
            "enable": True,
            "ipv6": False,
            "enhanced-mode": "fake-ip",
            "fake-ip-range": "198.18.0.1/16",
            "default-nameserver": ["1.1.1.1", "8.8.8.8"],
            "nameserver": ["https://cloudflare-dns.com/dns-query", "https://dns.google/dns-query"],
            "proxy-server-nameserver": [
                "https://cloudflare-dns.com/dns-query",
                "https://dns.google/dns-query",
            ],
        },
        "external-controller": "0.0.0.0:9090",
        "secret": runtime["MIHOMO_API_SECRET"],
        "proxy-providers": proxy_providers,
        "proxies": proxies,
        "proxy-groups": proxy_groups + egress_groups,
        "listeners": listeners,
        "rules": rules,
    }


def render_mihomo_document(db: Session) -> bytes:
    providers, routes, traffic_rules, egress_sources = load_transport_topology(db)
    config = build_config(
        providers,
        routes,
        require_runtime_environment(),
        traffic_rules,
        egress_sources,
    )
    return yaml.safe_dump(config, sort_keys=False).encode()


def config_hash(document: bytes) -> str:
    import hashlib

    return hashlib.sha256(document).hexdigest()


def main() -> None:
    from backend.app.core.database import SessionLocal

    target = os.path.abspath(os.environ.get("MIHOMO_CONFIG_PATH", "data/mihomo/config.yaml"))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with SessionLocal() as db:
        document = render_mihomo_document(db)
    temporary = f"{target}.tmp"
    with open(temporary, "wb") as handle:
        handle.write(document)
    os.replace(temporary, target)
    with suppress(OSError):
        os.chmod(target, 0o600)
    print(target)


if __name__ == "__main__":
    main()
