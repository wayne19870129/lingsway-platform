import logging
from datetime import UTC, datetime

import pytest

from backend.app.core.logging import CredentialRedactionFilter
from backend.app.core.secrets import SecretStoreError
from backend.app.infra import credential_resolver
from backend.app.infra.mihomo_generation import build_projection_source_manifest
from backend.app.infra.mihomo_materialization import VerifiedMaterialization
from backend.app.models import MihomoProjectionGeneration, MihomoTransportMaterialization
from backend.app.providers.base import DesiredForwarderState, ForwarderEgressProxyDTO
from backend.app.providers.forwarder.mihomo import ControllerSecretSnapshot
from backend.app.providers.forwarder.mihomo_projection import TransportMaterialization


class CredentialRecord(logging.LogRecord):
    password: str


def test_known_credential_fields_are_redacted_before_formatting() -> None:
    record = CredentialRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        {"api_key": "aaaa11111111bbbb", "event": "safe"},
        (),
        None,
    )
    record.password = "left-secret-right"
    assert CredentialRedactionFilter().filter(record)
    assert record.msg == {"api_key": "aaaa****bbbb", "event": "safe"}
    assert record.password == "left****ight"
    rendered = logging.Formatter("%(message)s %(password)s").format(record)
    assert "aaaa11111111bbbb" not in rendered
    assert "left-secret-right" not in rendered


def test_mihomo_materialization_repr_is_secret_safe() -> None:
    value = VerifiedMaterialization(
        1, "provider", 2, "cache-token", "a" * 64, datetime.now(UTC), b"SENTINEL_YAML"
    )
    rendered = repr(value)
    assert "SENTINEL_YAML" not in rendered
    assert "cache-token" not in rendered


def test_mihomo_b2_b1_sensitive_values_never_render_or_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    materialized = "MATERIALIZED_YAML_SECRET_SENTINEL"
    controller = "CONTROLLER_SECRET_SENTINEL"
    token = "SUBSCRIPTION_TOKEN_SENTINEL"
    verified = VerifiedMaterialization(
        1, "provider", 2, "cache-identity", "a" * 64, datetime.now(UTC), materialized.encode()
    )
    transport = TransportMaterialization(
        1, "provider", 2, "cache-identity", "a" * 64, datetime.now(UTC), {"proxies": [materialized]}
    )
    secret = ControllerSecretSnapshot(token, 7, controller)
    receipt = MihomoTransportMaterialization(owner_record_id=1, provider_code="provider")
    generation = MihomoProjectionGeneration(
        revision=1, manifest_version="1", desired_fingerprint="a" * 64
    )
    objects = (verified, transport, secret, receipt, generation)
    caplog.set_level(logging.INFO)
    logger = logging.getLogger("mihomo-b2-b1")
    for obj in objects:
        logger.info("%r", obj)
    manifest = build_projection_source_manifest(
        DesiredForwarderState(
            transport_materializations=(transport,),
            deployment_constants={"api-secret-ref": "mihomo/controller"},
        )
    )
    rendered = " ".join(repr(obj) for obj in objects) + repr(manifest)
    for sentinel in (materialized, controller, token):
        assert sentinel not in rendered
        assert sentinel not in caplog.text


def test_egress_proxy_repr_redacts_credential_ref() -> None:
    value = ForwarderEgressProxyDTO(
        name="egress-a",
        protocol="socks5",
        host="192.0.2.10",
        port=1080,
        credential_secret_ref="OPAQUE_REF_SENTINEL",
    )
    assert "OPAQUE_REF_SENTINEL" not in repr(value)


def test_mihomo_controller_resolver_failure_is_secret_safe(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    controller = "CONTROLLER_SECRET_SENTINEL"
    ref = "SECRET_REF_SENTINEL"
    token = "SUBSCRIPTION_TOKEN_SENTINEL"

    def fail(*_: object) -> object:
        raise SecretStoreError(f"{controller} {ref} {token}")

    monkeypatch.setattr(credential_resolver, "reveal_secret_snapshot_for_purpose", fail)
    resolver = credential_resolver.SqlMihomoControllerSecretResolver(object())  # type: ignore[arg-type]
    caplog.set_level(logging.INFO)
    with pytest.raises(Exception) as exc_info:
        resolver.resolve(ref)
    assert "CREDENTIAL_RESOLUTION_FAILED" in str(exc_info.value)
    for sentinel in (controller, ref, token):
        assert sentinel not in str(exc_info.value)
        assert sentinel not in repr(exc_info.value)
        assert sentinel not in caplog.text
