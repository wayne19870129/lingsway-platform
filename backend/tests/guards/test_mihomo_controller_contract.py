from __future__ import annotations

from pathlib import Path

from backend.app.providers.base import (
    DesiredForwarderState,
    ForwarderListenerDTO,
)
from backend.app.providers.forwarder.mihomo_projection import compose_mihomo_document

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_mihomo_controller_address_matches_checked_in_compose_topology() -> None:
    transport = (REPOSITORY_ROOT / "infrastructure/compose/compose.transport.yml").read_text()
    mihomo_service = transport.split("  mihomo:", 1)[1].split("  marzban:", 1)[0]
    assert "ipv4_address: 172.30.0.10" in mihomo_service
    assert "ports:" not in mihomo_service

    base = (REPOSITORY_ROOT / "infrastructure/compose/compose.base.yml").read_text()
    backend_api = base.split("  backend-api:", 1)[1].split("  traffic-attribution:", 1)[0]
    attribution = base.split("  traffic-attribution:", 1)[1]
    assert "MIHOMO_API_URL: http://mihomo:9090" in backend_api
    assert "MIHOMO_API_URL: http://mihomo:9090" in attribution

    state = DesiredForwarderState(
        listener_specs=(ForwarderListenerDTO("listener", "socks", "127.0.0.1", 10001, "BLOCK"),),
        rules=({"match": "MATCH", "target": "BLOCK"},),
        deployment_constants={
            "external-controller": "172.30.0.10:9090",
            "api-secret-ref": "mihomo/api-secret",
            "api-secret-revision": 1,
        },
    )
    document, _ = compose_mihomo_document(state)
    assert document["external-controller"] == "172.30.0.10:9090"
