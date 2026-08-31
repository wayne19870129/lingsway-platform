from backend.app.domain.subscription_render import render_routing_rules


def test_gateway_routing_is_block_closed() -> None:
    rules = render_routing_rules({"customer": "egress-1"})
    assert rules[0] == {
        "type": "field",
        "ip": ["geoip:private"],
        "outboundTag": "BLOCK",
    }
    assert rules[-1] == {
        "type": "field",
        "network": "tcp,udp",
        "outboundTag": "BLOCK",
    }
    assert all(rule.get("outboundTag") != "DIRECT" for rule in rules)
