from __future__ import annotations

import json
from pathlib import Path

from backend.app.infra.mihomo_reconciliation import MIHOMO_RECONCILE_JOB_TYPE


def test_reconciliation_does_not_activate_production_mihomo() -> None:
    source = Path("backend/app/infra/mihomo_reconciliation.py").read_text()
    assert "FORWARDER_PROVIDER" not in source
    assert "LocalMihomoRuntime" not in source
    assert "SessionLocal" in source


def test_job_payload_contract_is_identifier_only() -> None:
    payload = {
        "operation_id": "operation-1",
        "operation_kind": "RECONCILE",
        "snapshot_revision": 1,
    }
    encoded = json.dumps(payload)
    assert MIHOMO_RECONCILE_JOB_TYPE == "MIHOMO_RECONCILE"
    assert "secret" not in encoded.lower()
    assert "token" not in encoded.lower()
    assert "subscription_url" not in encoded.lower()


def test_only_transport_sync_is_a_materialization_receipt_producer() -> None:
    for path in (
        Path("backend/app/infra/mihomo_materialization.py"),
        Path("backend/app/infra/mihomo_reconciliation.py"),
        Path("backend/app/providers/forwarder/mihomo.py"),
    ):
        source = path.read_text()
        assert "MihomoTransportMaterialization(" not in source
        assert ".add(MihomoTransportMaterialization" not in source
    source = Path("backend/app/workers/transport_sync.py").read_text()
    assert "MihomoTransportMaterialization(" in source
