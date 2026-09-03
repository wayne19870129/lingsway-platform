"""Health endpoints exposed under the versioned API prefix."""

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["operations"])


@router.get("/health")
def api_health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/egress")
def egress_chain_health() -> dict[str, str]:
    """Report the independent end-to-end residential egress probe state."""
    path = Path("/app/data/gateway-probe/status.json")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(status_code=503, detail="egress probe unavailable") from None
    if not state.get("healthy"):
        raise HTTPException(status_code=503, detail=state.get("detail", "egress unhealthy"))
    return {"status": "ok", "observed_ip": str(state.get("observed_ip", ""))}
