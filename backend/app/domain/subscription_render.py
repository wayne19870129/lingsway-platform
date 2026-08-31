import base64
import json
from contextlib import suppress
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse


@dataclass(frozen=True, slots=True)
class RenderedSubscription:
    content: bytes
    media_type: str
    client_format: str


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


def render_subscription(connection_links: list[str], user_agent: str) -> RenderedSubscription:
    client_format = detect_client_format(user_agent)
    if client_format == "URI_BASE64":
        content = base64.b64encode("\n".join(connection_links).encode())
        return RenderedSubscription(content, "text/plain; charset=utf-8", client_format)

    proxies = [_clash_proxy(link, index) for index, link in enumerate(connection_links, 1)]
    rules = CORE_RULES if _is_mihomo(user_agent) else ("MATCH,SUBSCRIPTION",)
    content = _render_clash_yaml(proxies, rules).encode()
    return RenderedSubscription(content, "text/yaml; charset=utf-8", client_format)


def detect_client_format(user_agent: str) -> str:
    normalized = " ".join(user_agent.lower().replace("_", " ").split())
    if any(marker in normalized for marker in ("v2ray", "v2rayn", "sing-box", "singbox")):
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


def _is_mihomo(user_agent: str) -> bool:
    normalized = " ".join(user_agent.lower().replace("_", " ").split())
    return any(
        marker in normalized
        for marker in ("clashmeta", "clash meta", "clash.meta", "mihomo")
    )


def _decode_base64(value: str) -> str:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()


def _clash_proxy(link: str, index: int) -> dict[str, object]:
    if link.startswith("vmess://"):
        body = json.loads(_decode_base64(link.removeprefix("vmess://")))
        return {
            "name": str(body.get("ps") or f"proxy-{index}"),
            "type": "vmess",
            "server": str(body["add"]),
            "port": int(body["port"]),
            "uuid": str(body["id"]),
            "alterId": int(body.get("aid") or 0),
            "cipher": str(body.get("scy") or "auto"),
            "udp": True,
        }

    parsed = urlparse(link)
    if parsed.scheme not in {"ss", "vless", "trojan"} or not parsed.hostname or not parsed.port:
        raise ValueError(f"unsupported connection URI: {parsed.scheme or 'missing'}")
    query = parse_qs(parsed.query)
    proxy: dict[str, object] = {
        "name": unquote(parsed.fragment) or f"proxy-{index}",
        "type": parsed.scheme,
        "server": parsed.hostname,
        "port": parsed.port,
        "udp": True,
    }
    credential = unquote(parsed.username or "")
    if parsed.scheme == "ss":
        with suppress(ValueError, UnicodeDecodeError):
            credential = _decode_base64(credential)
        cipher, separator, password = credential.partition(":")
        if not separator:
            raise ValueError("invalid Shadowsocks URI")
        proxy.update({"cipher": cipher, "password": password})
        return proxy
    if parsed.scheme == "vless":
        proxy.update({"uuid": credential, "encryption": query.get("encryption", ["none"])[0]})
    else:
        proxy["password"] = credential
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
