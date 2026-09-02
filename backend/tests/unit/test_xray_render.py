from types import SimpleNamespace

import ops.gateway.render_xray_routes as renderer
from ops.gateway.render_xray_routes import render_config


def test_xray_private_key_parser_accepts_xray_cli_labels(monkeypatch) -> None:
    monkeypatch.setattr(
        renderer.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="PrivateKey: generated-key\n",
            stderr="",
        ),
    )

    assert renderer._x25519_private_key("xray") == "generated-key"


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
