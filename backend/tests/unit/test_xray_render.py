from ops.gateway.render_xray_routes import render_config


def test_empty_database_render_has_only_block_and_terminal_guards() -> None:
    base = {
        "inbounds": [
            {
                "tag": "vless-reality",
                "protocol": "vless",
                "streamSettings": {"security": "reality"},
            }
        ],
        "outbounds": [],
        "routing": {"domainStrategy": "AsIs", "rules": []},
    }

    rendered = render_config(base, [])

    assert rendered["outbounds"] == [{"tag": "BLOCK", "protocol": "blackhole"}]
    assert rendered["routing"]["rules"] == [
        {"type": "field", "ip": ["geoip:private"], "outboundTag": "BLOCK"},
        {"type": "field", "network": "tcp,udp", "outboundTag": "BLOCK"},
    ]
    assert all(rule.get("outboundTag") != "DIRECT" for rule in rendered["routing"]["rules"])
