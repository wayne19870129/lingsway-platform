import base64
import json
from contextlib import suppress
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlparse

# Target-state rules reserved for TASK-T13; they are not used by the current
# production-compatible subscription path.
PRODUCTION_RULES: tuple[str, ...] = ("MATCH,SUBSCRIPTION",)


@dataclass(frozen=True, slots=True)
class RenderedSubscription:
    content: bytes
    media_type: str
    client_format: str
    headers: dict[str, str] = field(default_factory=dict)
    rules: tuple[str, ...] = PRODUCTION_RULES


# CORE_RULES is retained as the disabled target-state definition for
# TASK-T13; the current production-compatible path does not render it.
CORE_RULES: tuple[str, ...] = (
    "DOMAIN-SUFFIX,claude.ai,SUBSCRIPTION",
    "DOMAIN-SUFFIX,anthropic.com,SUBSCRIPTION",
    "DOMAIN-SUFFIX,chatgpt.com,SUBSCRIPTION",
    "DOMAIN-SUFFIX,openai.com,SUBSCRIPTION",
    "DOMAIN-SUFFIX,oaistatic.com,SUBSCRIPTION",
    "DOMAIN-SUFFIX,oaiusercontent.com,SUBSCRIPTION",
    "DOMAIN-SUFFIX,google.com,SUBSCRIPTION",
    "DOMAIN-SUFFIX,youtube.com,SUBSCRIPTION",
    "DOMAIN-SUFFIX,netflix.com,SUBSCRIPTION",
    "GEOSITE,geolocation-!cn,SUBSCRIPTION",
    "GEOSITE,CN,DIRECT",
    "GEOIP,CN,no-resolve,DIRECT",
    "MATCH,SUBSCRIPTION",
)

def render_subscription(
    connection_links: list[str],
    user_agent: str,
    *,
    subscription_userinfo: str | None = None,
    etag: str | None = None,
) -> RenderedSubscription:
    client_format = detect_client_format(user_agent)
    headers = {
        key: value
        for key, value in {
            "Subscription-Userinfo": subscription_userinfo,
            "ETag": etag,
        }.items()
        if value is not None
    }
    if client_format == "URI_BASE64":
        content = base64.b64encode("\n".join(connection_links).encode())
        return RenderedSubscription(
            content,
            "text/plain; charset=utf-8",
            client_format,
            headers,
            PRODUCTION_RULES,
        )

    used_names: set[str] = set()
    proxies = [
        _clash_proxy(link, index, used_names)
        for index, link in enumerate(connection_links, 1)
    ]
    rules = PRODUCTION_RULES
    content = _render_clash_yaml(proxies, rules).encode()
    return RenderedSubscription(
        content,
        "text/yaml; charset=utf-8",
        client_format,
        headers,
        PRODUCTION_RULES,
    )


def detect_client_format(user_agent: str) -> str:
    normalized = " ".join(user_agent.lower().replace("_", " ").split())
    clash_markers = ("clashmeta", "clash meta", "clash.meta", "mihomo", "stash")
    if any(marker in normalized for marker in clash_markers):
        return "CLASH"
    v2ray_markers = ("v2ray", "v2rayng", "sing-box", "singbox")
    if any(marker in normalized for marker in v2ray_markers):
        return "URI_BASE64"
    return "CLASH"


def render_routing_rules(user_routes: dict[str, str]) -> tuple[dict[str, object], ...]:
    """Render gateway invariants: private BLOCK first and catch-all BLOCK last."""
    private_block: dict[str, object] = {
        "type": "field",
        "ip": ["geoip:private"],
        "outboundTag": "BLOCK",
    }
    customer_rules = tuple(
        _customer_route_rule(username, outbound)
        for username, outbound in sorted(user_routes.items())
    )
    catch_all: dict[str, object] = {
        "type": "field",
        "network": "tcp,udp",
        "outboundTag": "BLOCK",
    }
    return (private_block, *customer_rules, catch_all)


def _customer_route_rule(username: str, outbound: str) -> dict[str, object]:
    return {
        "type": "field",
        "user": [username],
        "outboundTag": outbound,
    }


def _decode_base64(value: str) -> str:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()


def _unique_name(candidate: str, used: set[str]) -> str:
    base = candidate or "proxy"
    name = base
    suffix = 2
    while name in used:
        name = f"{base}-{suffix}"
        suffix += 1
    used.add(name)
    return name


def _clash_proxy(link: str, index: int, used_names: set[str]) -> dict[str, object]:
    if link.startswith("vmess://"):
        body = json.loads(_decode_base64(link.removeprefix("vmess://")))
        vmess_proxy: dict[str, object] = {
            "name": _unique_name(str(body.get("ps") or "vmess"), used_names),
            "type": "vmess",
            "server": str(body["add"]),
            "port": int(body["port"]),
            "uuid": str(body["id"]),
            "alterId": int(body.get("aid") or 0),
            "cipher": str(body.get("scy") or "auto"),
            "udp": True,
        }
        if body.get("net"):
            vmess_proxy["network"] = str(body["net"])
        if body.get("tls") == "tls":
            vmess_proxy["tls"] = True
        if body.get("sni"):
            vmess_proxy["servername"] = str(body["sni"])
        return vmess_proxy

    parsed = urlparse(link)
    if parsed.scheme not in {"ss", "vless", "trojan"} or not parsed.hostname or not parsed.port:
        raise ValueError(f"unsupported connection URI: {parsed.scheme or 'missing'}")
    query = parse_qs(parsed.query)
    proxy: dict[str, object] = {
        "name": _unique_name(unquote(parsed.fragment) or parsed.scheme, used_names),
        "type": parsed.scheme,
        "server": parsed.hostname,
        "port": parsed.port,
        "udp": True,
    }
    credential = unquote(parsed.username or "")
    if parsed.scheme == "ss":
        with suppress(ValueError, UnicodeDecodeError):
            credential = _decode_base64(unquote(credential))
        cipher, separator, password = credential.partition(":")
        if not separator:
            raise ValueError("invalid Shadowsocks URI")
        proxy.update({"cipher": cipher, "password": password})
        return proxy
    if parsed.scheme == "vless":
        proxy["uuid"] = credential
        proxy["encryption"] = query.get("encryption", ["none"])[0] or "none"
        fingerprint = query.get("fp", [None])[0]
        if fingerprint:
            proxy["client-fingerprint"] = fingerprint
    else:
        proxy["password"] = credential
    network = query.get("type", [None])[0]
    security = query.get("security", [None])[0]
    if network:
        proxy["network"] = network
    if security in {"tls", "reality"}:
        proxy["tls"] = True
    servername = query.get("sni", query.get("serverName", [None]))[0]
    if servername:
        proxy["servername"] = servername
    flow = query.get("flow", [None])[0]
    if flow and parsed.scheme == "vless":
        proxy["flow"] = flow
    if security == "reality":
        reality_opts: dict[str, str] = {"public-key": query.get("pbk", [""])[0]}
        short_id = query.get("sid", [""])[0]
        if short_id:
            reality_opts["short-id"] = short_id
        proxy["reality-opts"] = reality_opts
    return proxy


def _render_clash_yaml(proxies: list[dict[str, object]], rules: tuple[str, ...]) -> str:
    lines = ["proxies:"]
    for proxy in proxies:
        lines.append(f"  - {json.dumps(proxy, ensure_ascii=False, separators=(',', ':'))}")
    lines.extend(("proxy-groups:", "  - name: SUBSCRIPTION", "    type: select", "    proxies:"))
    lines.extend(f"      - {json.dumps(str(proxy['name']))}" for proxy in proxies)
    lines.append("rules:")
    lines.extend(f"  - {json.dumps(rule)}" for rule in rules)
    return "\n".join(lines) + "\n"
