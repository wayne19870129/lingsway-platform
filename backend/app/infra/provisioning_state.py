"""SQLAlchemy adapter implementing the domain provisioning state protocol."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from backend.app.core.config import get_settings
from backend.app.core.secrets import put_secret, reveal_secret
from backend.app.domain.provisioning import ProvisioningState, ProvisionRequest
from backend.app.infra.gateway_route_lock import (
    GatewayRouteBindingLockError,
    gateway_route_binding_write,
    session_holds_gateway_route_binding_lock,
)
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
    XrayOutboundDTO,
)


class ProvisioningStateError(RuntimeError):
    """Raised when DB-backed provisioning state cannot be completed safely."""


class DesiredRoutingStateError(ProvisioningStateError):
    """Stable, secret-safe failure for an invalid active routing snapshot."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


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
        # ADR-017 production-bypass guard: this is the sole real writer of
        # GatewayRouteBinding reachable from ProvisioningService. Refuse to
        # mutate if the caller reached here without holding the ADR-016
        # named lock on this exact session -- e.g. a convenience API
        # (services.provision(), or ProvisioningService.provision_apply_gateway()
        # called directly) pointed at a real SqlAlchemyProvisioningState
        # outside confirm_payment_and_provision()'s lock-wrapped call path.
        if not session_holds_gateway_route_binding_lock(self.db):
            raise GatewayRouteBindingLockError(
                "ensure_gateway_route_binding() called without the ADR-016 "
                "named lock held on this session; refusing to mutate "
                "GatewayRouteBinding outside gateway_route_binding_lock()"
            )
        endpoint_id = _db_id(endpoint.endpoint_id, "endpoint_id")
        binding = self.db.scalar(
            select(GatewayRouteBinding)
            .where(
                GatewayRouteBinding.subscription_id == self.subscription_id,
                GatewayRouteBinding.released_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
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

    def _full_desired_routing_snapshot(self) -> DesiredRoutingState:
        return full_desired_routing_snapshot(self.db)

    def desired_routing_state(
        self,
        request: ProvisionRequest,
        endpoint: EgressEndpointDTO,
        tenant: TenantDTO,
        routing_principal: str,
    ) -> DesiredRoutingState:
        del request, tenant  # ADR-016: routing_principal replaces request.username here.
        self.ensure_gateway_route_binding(routing_principal, endpoint)
        self.db.flush()
        return self._full_desired_routing_snapshot()

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

def full_desired_routing_snapshot(db: Session) -> DesiredRoutingState:
    """Build the complete active routing state with current DB reads.

    Every read here is performed on the caller-owned provisioning Session.
    Locking reads are intentional: the named lock serializes route writers,
    while these reads independently bypass an older MySQL REPEATABLE READ
    view and refresh any stale ORM identity-map entries.
    """
    statement = (
        select(GatewayRouteBinding, EgressEndpoint, EgressBinding)
        .outerjoin(
            EgressEndpoint, EgressEndpoint.id == GatewayRouteBinding.egress_id
        )
        .outerjoin(
            EgressBinding,
            and_(
                EgressBinding.subscription_id == GatewayRouteBinding.subscription_id,
                EgressBinding.egress_id == GatewayRouteBinding.egress_id,
                EgressBinding.released_at.is_(None),
            ),
        )
        .where(
            GatewayRouteBinding.enabled.is_(True),
            GatewayRouteBinding.released_at.is_(None),
        )
        .order_by(
            GatewayRouteBinding.outbound_tag,
            GatewayRouteBinding.gateway_principal,
            GatewayRouteBinding.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    rows = db.execute(statement).all()

    # Read only active bindings belonging to subscriptions represented by
    # the authoritative route set.  This current-read consistency check
    # catches a same-subscription binding pointing at another egress, but
    # does not lock unrelated Phase-A bindings for subscriptions that have
    # no active GatewayRouteBinding yet.
    route_subscription_ids = {route.subscription_id for route, _, _ in rows}
    active_bindings = (
        db.scalars(
            select(EgressBinding)
            .where(
                EgressBinding.released_at.is_(None),
                EgressBinding.subscription_id.in_(route_subscription_ids),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        if route_subscription_ids
        else []
    )
    bindings_by_subscription: dict[int, list[EgressBinding]] = {}
    for binding in active_bindings:
        bindings_by_subscription.setdefault(binding.subscription_id, []).append(binding)

    user_routes: dict[str, str] = {}
    outbounds_by_tag: dict[str, XrayOutboundDTO] = {}
    for route, endpoint, joined_binding in rows:
        if not isinstance(route.gateway_principal, str) or not route.gateway_principal.strip():
            raise DesiredRoutingStateError("ROUTE_PRINCIPAL_INVALID")
        if not isinstance(route.outbound_tag, str) or not route.outbound_tag.strip():
            raise DesiredRoutingStateError("ROUTE_TAG_INVALID")
        if route.outbound_tag == "BLOCK":
            raise DesiredRoutingStateError("ROUTE_TAG_RESERVED")
        if route.gateway_principal in user_routes:
            raise DesiredRoutingStateError("ROUTE_PRINCIPAL_DUPLICATE")

        if endpoint is None:
            raise DesiredRoutingStateError("ROUTE_ENDPOINT_MISSING")
        bindings = bindings_by_subscription.get(route.subscription_id, [])
        if any(binding.egress_id != route.egress_id for binding in bindings):
            raise DesiredRoutingStateError("ROUTE_BINDING_EGRESS_MISMATCH")
        if joined_binding is not None and joined_binding.egress_id != route.egress_id:
            raise DesiredRoutingStateError("ROUTE_BINDING_EGRESS_MISMATCH")

        if get_settings().forwarder_provider == "mihomo":
            listen_port = endpoint.mihomo_listen_port
            if (
                not isinstance(listen_port, int)
                or isinstance(listen_port, bool)
                or not 1 <= listen_port <= 65535
            ):
                raise DesiredRoutingStateError("MIHOMO_LISTEN_PORT_INVALID")
            outbound = XrayOutboundDTO(
                tag=route.outbound_tag,
                host="127.0.0.1",
                port=listen_port,
                protocol="socks",
                credential_secret_ref=None,
            )
        else:
            if not isinstance(endpoint.host, str) or not endpoint.host.strip():
                raise DesiredRoutingStateError("ENDPOINT_HOST_INVALID")
            if (
                not isinstance(endpoint.port, int)
                or isinstance(endpoint.port, bool)
                or not 1 <= endpoint.port <= 65535
            ):
                raise DesiredRoutingStateError("ENDPOINT_PORT_INVALID")
            if not isinstance(endpoint.protocol, str) or endpoint.protocol.lower() not in {
                "socks",
                "socks5",
            }:
                raise DesiredRoutingStateError("ENDPOINT_PROTOCOL_UNSUPPORTED")

            selected_ref = endpoint.credential_secret_ref
            if (
                joined_binding is not None
                and joined_binding.credential_secret_ref is not None
                and joined_binding.credential_secret_ref != ""
            ):
                selected_ref = joined_binding.credential_secret_ref
            if not isinstance(selected_ref, str) or selected_ref == "":
                raise DesiredRoutingStateError("CREDENTIAL_REF_INVALID")

            outbound = XrayOutboundDTO(
                tag=route.outbound_tag,
                host=endpoint.host,
                port=endpoint.port,
                protocol=endpoint.protocol,
                credential_secret_ref=selected_ref,
            )
        existing = outbounds_by_tag.get(outbound.tag)
        if existing is not None:
            raise DesiredRoutingStateError("ROUTE_TAG_DUPLICATE")
        outbounds_by_tag[outbound.tag] = outbound
        user_routes[route.gateway_principal] = route.outbound_tag

    return DesiredRoutingState(
        user_routes=user_routes,
        outbounds=tuple(outbounds_by_tag[tag] for tag in sorted(outbounds_by_tag)),
    )

