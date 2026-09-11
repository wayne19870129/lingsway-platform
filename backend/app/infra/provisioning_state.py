"""SQLAlchemy adapter implementing the domain provisioning state protocol."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.config import get_settings
from backend.app.core.secrets import put_secret, reveal_secret
from backend.app.domain.provisioning import ProvisioningState, ProvisionRequest
from backend.app.infra.gateway_route_lock import gateway_route_binding_write
from backend.app.models import (
    EgressBinding,
    EgressEndpoint,
    GatewayRouteBinding,
    Subscription,
)
from backend.app.providers.base import (
    CredentialDTO,
    DesiredForwarderState,
    DesiredRoutingState,
    EgressEndpointDTO,
    TenantDTO,
)


class ProvisioningStateError(RuntimeError):
    """Raised when DB-backed provisioning state cannot be completed safely."""


def _db_id(value: str, label: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ProvisioningStateError(f"{label} must be a database integer id") from exc


class SqlAlchemyProvisioningState(ProvisioningState):
    """Concrete DB state adapter; callers own commit/rollback transaction scope."""

    def __init__(
        self,
        db: Session,
        subscription_id: int,
        *,
        mihomo_config_path: Path | None = None,
    ) -> None:
        self.db = db
        self.subscription_id = subscription_id
        self.mihomo_config_path = mihomo_config_path

    def reject_capacity(self, order_id: str, reason: str) -> None:
        del order_id, reason

    def allocate_endpoint(self, customer_id: str) -> EgressEndpointDTO:
        subscription = self.db.get(Subscription, self.subscription_id)
        if subscription is None or str(subscription.customer_id) != customer_id:
            raise ProvisioningStateError("subscription/customer does not match")
        endpoint = self.db.scalar(
            select(EgressEndpoint)
            .where(
                EgressEndpoint.status == "AVAILABLE",
                EgressEndpoint.purpose == "PRODUCTION",
                EgressEndpoint.capacity == 1,
                EgressEndpoint.current_count == 0,
            )
            .order_by(EgressEndpoint.id)
            .with_for_update()
        )
        if endpoint is None:
            raise ProvisioningStateError("no dedicated egress endpoint is available")
        endpoint.current_count = 1
        endpoint.status = "ASSIGNED"
        binding = self.db.scalar(
            select(EgressBinding).where(
                EgressBinding.subscription_id == self.subscription_id,
                EgressBinding.released_at.is_(None),
            )
        )
        if binding is None:
            self.db.add(
                EgressBinding(
                    subscription_id=self.subscription_id,
                    egress_id=endpoint.id,
                )
            )
        self.db.flush()
        return EgressEndpointDTO(
            endpoint_id=str(endpoint.id),
            host=endpoint.host,
            port=endpoint.port,
            protocol=endpoint.protocol,
            public_ip=None,
        )

    def release_endpoint(self, endpoint_id: str) -> None:
        endpoint = self.db.get(EgressEndpoint, _db_id(endpoint_id, "endpoint_id"))
        if endpoint is None:
            return
        binding = self.db.scalar(
            select(EgressBinding).where(
                EgressBinding.subscription_id == self.subscription_id,
                EgressBinding.egress_id == endpoint.id,
                EgressBinding.released_at.is_(None),
            )
        )
        if binding is not None:
            binding.released_at = datetime.now(UTC)
        endpoint.current_count = 0
        endpoint.status = "AVAILABLE"
        self.db.flush()

    def save_credentials(
        self, tenant: TenantDTO, endpoint: EgressEndpointDTO, credential: CredentialDTO
    ) -> str:
        endpoint_id = _db_id(endpoint.endpoint_id, "endpoint_id")
        secret_ref = (
            f"webshare/subuser/{self.subscription_id}/egress/{endpoint_id}"
        )
        put_secret(
            self.db,
            secret_ref,
            json.dumps(
                {"username": credential.username, "password": credential.password},
                separators=(",", ":"),
            ),
            "WEBSHARE_SUBUSER_PROXY_CREDENTIAL",
        )
        binding = self.db.scalar(
            select(EgressBinding).where(
                EgressBinding.subscription_id == self.subscription_id,
                EgressBinding.egress_id == endpoint_id,
                EgressBinding.released_at.is_(None),
            )
        )
        if binding is None:
            binding = EgressBinding(
                subscription_id=self.subscription_id,
                egress_id=endpoint_id,
            )
            self.db.add(binding)
        binding.credential_secret_ref = secret_ref
        self.db.flush()
        del tenant
        return secret_ref

    def rollback_database(self) -> None:
        self.db.rollback()

    @contextmanager
    def gateway_route_binding_lock(self) -> Iterator[None]:
        """ADR-016: the mandatory concurrency contract for this table.

        Callers must perform ``ensure_gateway_route_binding()`` and their
        own eventual ``self.db.commit()``/``self.db.rollback()`` for that
        mutation *inside* this context -- see
        ``gateway_route_binding_write()`` for the exact ordering guarantee.
        """
        with gateway_route_binding_write(self.db):
            yield

    def current_forwarder_state(self) -> DesiredForwarderState:
        listeners = {
            str(endpoint.id): str(endpoint.mihomo_listen_port)
            for endpoint in self.db.scalars(
                select(EgressEndpoint).where(
                    EgressEndpoint.status.in_(["AVAILABLE", "ASSIGNED", "DEGRADED"])
                )
            )
        }
        return DesiredForwarderState(listeners)

    def desired_forwarder_state(
        self, endpoint: EgressEndpointDTO, tenant: TenantDTO, secret_ref: str
    ) -> DesiredForwarderState:
        current = dict(self.current_forwarder_state().listeners)
        current[endpoint.endpoint_id] = f"{tenant.tenant_id}:{secret_ref}"
        return DesiredForwarderState(current)

    def ensure_gateway_route_binding(
        self, gateway_principal: str, endpoint: EgressEndpointDTO
    ) -> GatewayRouteBinding:
        endpoint_id = _db_id(endpoint.endpoint_id, "endpoint_id")
        binding = self.db.scalar(
            select(GatewayRouteBinding).where(
                GatewayRouteBinding.subscription_id == self.subscription_id,
                GatewayRouteBinding.released_at.is_(None),
            )
        )
        outbound_tag = f"egress-{endpoint_id}"
        if binding is None:
            binding = GatewayRouteBinding(
                subscription_id=self.subscription_id,
                egress_id=endpoint_id,
                gateway_principal=gateway_principal,
                outbound_tag=outbound_tag,
                enabled=True,
            )
            self.db.add(binding)
        else:
            binding.egress_id = endpoint_id
            binding.gateway_principal = gateway_principal
            binding.outbound_tag = outbound_tag
            binding.enabled = True
        self.db.flush()
        return binding

    def desired_routing_state(
        self, request: ProvisionRequest, endpoint: EgressEndpointDTO, tenant: TenantDTO
    ) -> DesiredRoutingState:
        binding = self.ensure_gateway_route_binding(tenant.tenant_id, endpoint)
        return DesiredRoutingState(
            user_routes={request.username: binding.outbound_tag},
            outbound_tags=(binding.outbound_tag, "BLOCK"),
        )

    def store_subscription_token(self, customer_id: str, raw_token: str) -> None:
        subscription = self.db.get(Subscription, self.subscription_id)
        if subscription is None or str(subscription.customer_id) != customer_id:
            raise ProvisioningStateError("subscription/customer does not match")
        put_secret(
            self.db,
            f"subscription/{self.subscription_id}/token",
            raw_token,
            "SUBSCRIPTION_TOKEN",
        )
        subscription.token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        subscription.token_created_at = datetime.now(UTC)
        subscription.token_revoked_at = None
        self.db.flush()

    def read_subscription_token(self) -> str:
        subscription = self.db.get(Subscription, self.subscription_id)
        if subscription is None or not subscription.token_hash:
            raise ProvisioningStateError("subscription token is not available")
        raw_token = reveal_secret(self.db, f"subscription/{self.subscription_id}/token")
        if hashlib.sha256(raw_token.encode()).hexdigest() != subscription.token_hash:
            raise ProvisioningStateError("subscription token hash mismatch")
        return raw_token

    def current_subscription_url(self, domain: str | None = None) -> str:
        configured = (
            domain
            or get_settings().subscription_domain
            or get_settings().subscription_base_url
        )
        base = configured.strip().rstrip("/")
        if not base:
            raise ProvisioningStateError("subscription domain is required")
        if "://" not in base:
            base = f"https://{base}"
        return f"{base}/s/{self.read_subscription_token()}"
