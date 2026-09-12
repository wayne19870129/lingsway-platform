"""Administrator query and operational endpoints."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import and_, func, select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from backend.app.core.config import get_settings
from backend.app.dependencies import AdminCustomer, DbSession
from backend.app.domain.capacity import CapacityExceededError
from backend.app.domain.ordering import BillingCommand, BillingOrderType, PaymentConfirmation
from backend.app.domain.provisioning import (
    ProvisionRequest,
    ProvisionRunStore,
    ProvisionStatus,
    ProvisionStep,
)
from backend.app.infra.gateway_route_lock import gateway_route_binding_write
from backend.app.infra.provisioning_state import SqlAlchemyProvisioningState
from backend.app.models import (
    AuditLog,
    Customer,
    EgressBinding,
    EgressEndpoint,
    GatewayMetric,
    Job,
    JobStatus,
    Order,
    OrderStatus,
    PaymentStatus,
    Plan,
    PlanStatus,
    ReconcileState,
    Subscription,
    SubscriptionStatus,
    Ticket,
    TransportProviderRecord,
    UsagePeriod,
    UsagePeriodStatus,
    WebshareSubuser,
)
from backend.app.providers.base import (
    CredentialDTO,
    DesiredForwarderState,
    DesiredRoutingState,
    EgressEndpointDTO,
    TenantDTO,
)
from backend.app.providers.registry import build_registry
from backend.app.schemas.admin import PaymentConfirmation as PaymentConfirmationRequest
from backend.app.schemas.admin import ProvisionResult
from backend.app.schemas.public import SubscriptionRead
from backend.app.services import confirm_payment_and_provision

router = APIRouter(tags=["admin"])

ADMIN_CAPACITY_PLAN_CODES = ("PLAN_50GB", "PLAN_100GB", "PLAN_200GB", "PLAN_500GB")


class _SqlAlchemyOrderState:
    """Adapter for DB-only transitions around the external provisioning saga."""

    def __init__(self, db: Session) -> None:
        self.db = db

    @contextmanager
    def transaction(self) -> Iterator[None]:
        try:
            yield
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def _order(self, order_id: str) -> Order:
        order = self.db.get(Order, int(order_id))
        if order is None:
            raise ValueError("Order not found")
        return order

    def record_payment(self, order_id: str, receipt: PaymentConfirmation) -> None:
        order = self._order(order_id)
        order.payment_status = PaymentStatus.PAID
        order.payment_provider = receipt.provider_code
        order.payment_reference = receipt.external_reference
        order.paid_at = receipt.paid_at
        order.status = OrderStatus.PAID

    def reject_capacity(self, order_id: str, reason: str) -> None:
        order = self._order(order_id)
        self.db.add(
            AuditLog(
                action="ORDER_CAPACITY_REJECTED",
                entity_type="ORDER",
                entity_id=str(order.id),
                result="FAILED",
                detail=reason,
            )
        )

    def prepare_purchase(self, command: BillingCommand) -> None:
        order = self._order(command.order_id)
        plan = self.db.get(Plan, order.plan_id)
        if plan is None:
            raise ValueError("Order plan is not available")
        subscription = self.db.scalar(
            select(Subscription).where(Subscription.order_id == order.id)
        )
        now = datetime.now(UTC)
        if subscription is None:
            subscription = Subscription(
                subscription_no=f"SUB-{now:%Y%m%d}-{order.id:06d}",
                customer_id=order.customer_id,
                plan_id=plan.id,
                order_id=order.id,
                status=SubscriptionStatus.PROVISIONING,
                route_group_code=plan.route_group_code,
                service_expire_at=now + timedelta(days=plan.duration_days),
                reconcile_state=ReconcileState.PENDING,
            )
            self.db.add(subscription)
            self.db.flush()
            period = UsagePeriod(
                subscription_id=subscription.id,
                period_start=now,
                period_end=subscription.service_expire_at,
                used_bytes=0,
                quota_bytes=plan.traffic_limit_bytes,
                status=UsagePeriodStatus.ACTIVE,
            )
            self.db.add(period)
            self.db.flush()
            subscription.current_period_id = period.id
            order.target_subscription_id = subscription.id

    def _subscription(self, command: BillingCommand) -> Subscription:
        if command.subscription_id is not None:
            subscription = self.db.get(Subscription, int(command.subscription_id))
        else:
            subscription = self.db.scalar(
                select(Subscription).where(Subscription.order_id == int(command.order_id))
            )
        if subscription is None:
            raise ValueError("Subscription not found")
        return subscription

    def apply_renewal(self, command: BillingCommand) -> None:
        del command
        raise ValueError("Non-purchase billing is not handled by provisioning")

    def apply_upgrade(self, command: BillingCommand) -> None:
        del command
        raise ValueError("Non-purchase billing is not handled by provisioning")

    def apply_addon(self, command: BillingCommand) -> None:
        del command
        raise ValueError("Non-purchase billing is not handled by provisioning")

    def activate_subscription(self, command: BillingCommand) -> None:
        subscription = self._subscription(command)
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.reconcile_state = ReconcileState.SYNCED
        order = self._order(command.order_id)
        order.status = OrderStatus.ACTIVATED
        order.activated_at = order.activated_at or datetime.now(UTC)

    def mark_provision_pending(self, command: BillingCommand, error: str) -> None:
        order = self._order(command.order_id)
        order.status = OrderStatus.PAID
        order.payment_status = PaymentStatus.PAID
        subscription = self._subscription(command)
        subscription.provision_error = error

    def mark_provision_failed(self, command: BillingCommand, error: str) -> None:
        order = self._order(command.order_id)
        order.status = OrderStatus.PAID
        order.payment_status = PaymentStatus.PAID
        subscription = self._subscription(command)
        subscription.status = SubscriptionStatus.PROVISION_FAILED
        subscription.provision_error = error


class _SqlAlchemyProvisionRuns(ProvisionRunStore):
    """Persist saga progress in the existing generic jobs table.

    Uses its **own** dedicated ``Session``, opened from the same engine
    as the request-scoped ``db`` but never shared with it, committing
    immediately after every write. Run/job status is an audit trail that
    must survive a rollback of the request's own business-data
    transaction: ADR-017's lock-acquisition-failure path (and any other
    mid-saga failure) rolls back phase-A's endpoint/credential/forwarder
    mutations on ``db``, but the run's FAILED status -- and, for a
    freshly-started run, the run row's own INSERT from :meth:`start` --
    must still be durably recorded. Sharing ``db`` would mean that same
    rollback silently erases the run's own status (or, worse, the run
    row itself, since a same-session ``start()`` is never independently
    committed before the saga's eventual commit/rollback).

    This intentionally does **not** generalize to other ``Job`` users --
    only this ``ProvisionRunStore`` adapter's writes are decoupled like
    this; the ``jobs`` table and its model are unchanged.

    Two different reliability contracts apply here, per
    ``ProvisionRunStore``'s own docstring (ADR-017 run-persistence-
    ordering revision):

    - :meth:`record_step`, :meth:`record_external_id`, and
      :meth:`mark_notification_failed` are best-effort progress
      bookkeeping: any failure writing them (including this dedicated
      session's own commit) is caught and swallowed here, never
      propagated. Before this, a transient failure persisting a routine
      "step succeeded" marker -- itself unrelated to whether the step's
      real external side effect succeeded -- could escape
      ``ProvisioningService`` and skip that side effect's own
      compensation (e.g. a committed accounting user never getting
      disabled because the progress-write exception replaced the
      original success path before reaching the caller's own failure
      handling).
    - :meth:`start` and :meth:`mark_status` remain fully raising: a
      caller relies on their success (or failure) actually reflecting
      whether the run/terminal-status write happened.
    """

    def __init__(self, db: Session) -> None:
        bind = db.get_bind()
        engine = bind.engine if isinstance(bind, Connection) else bind
        self.db = Session(engine, expire_on_commit=False)

    def close(self) -> None:
        self.db.close()

    def _job(self, run_id: str) -> Job:
        job = self.db.get(Job, int(run_id))
        if job is None:
            raise ValueError("Provision run not found")
        return job

    def _payload(self, job: Job) -> dict[str, object]:
        payload = json.loads(job.payload_json or "{}")
        return payload if isinstance(payload, dict) else {}

    def start(self, request: ProvisionRequest) -> str:
        dedupe_key = f"provision:{request.order_id}"
        job = self.db.scalar(select(Job).where(Job.dedupe_key == dedupe_key))
        if job is None:
            job = Job(
                job_type="PROVISION",
                dedupe_key=dedupe_key,
                payload_json=json.dumps({"order_id": request.order_id, "steps": {}}),
                status=JobStatus.RUNNING,
                attempts=1,
            )
            self.db.add(job)
        else:
            job.status = JobStatus.RUNNING
            job.attempts += 1
        self.db.commit()
        return str(job.id)

    def record_step(self, run_id: str, step: ProvisionStep, status: str) -> None:
        try:
            job = self._job(run_id)
            payload = self._payload(job)
            steps = payload.setdefault("steps", {})
            if isinstance(steps, dict):
                steps[str(int(step))] = status
            job.payload_json = json.dumps(payload, sort_keys=True)
            self.db.commit()
        except Exception:
            # Best-effort progress bookkeeping (ProvisionRunStore's
            # contract) -- never let this block the caller's own
            # external-side-effect compensation. Roll back so this
            # dedicated session stays usable for the next write.
            self.db.rollback()

    def record_external_id(self, run_id: str, kind: str, external_id: str) -> None:
        try:
            job = self._job(run_id)
            payload = self._payload(job)
            external_ids = payload.setdefault("external_ids", {})
            if isinstance(external_ids, dict):
                external_ids[kind] = external_id
            job.payload_json = json.dumps(payload, sort_keys=True)
            self.db.commit()
        except Exception:
            self.db.rollback()

    def mark_status(
        self, run_id: str, status: ProvisionStatus, error: str | None = None
    ) -> None:
        # Deliberately raising, unlike record_step/record_external_id/
        # mark_notification_failed above -- see this class's docstring and
        # ProvisionRunStore's own contract. Terminal status callers
        # (ProvisioningService._failed / mark_apply_gateway_succeeded)
        # already sequence this call correctly relative to the business
        # transaction it reports on; masking a failure to persist it would
        # hide that the audit trail and the real outcome have diverged.
        try:
            job = self._job(run_id)
            mapped = {
                ProvisionStatus.RUNNING: JobStatus.RUNNING,
                ProvisionStatus.PENDING_MANUAL: JobStatus.PENDING,
                ProvisionStatus.FAILED: JobStatus.FAILED,
                ProvisionStatus.SUCCEEDED: JobStatus.SUCCEEDED,
            }[status]
            job.status = mapped
            # `last_error_code` is a short String(80) column, not a full
            # message store -- a real MySQL DataError here (discovered via
            # this adapter's own integration test forcing a genuine
            # lock-acquisition failure) would otherwise abort this commit
            # and lose the FAILED status entirely. Truncate defensively
            # rather than widen the schema: this is a persistence-boundary
            # concern, not a change to what error text callers construct.
            job.last_error_code = error[:80] if error else error
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def mark_notification_failed(self, run_id: str, error: str) -> None:
        try:
            job = self._job(run_id)
            payload = self._payload(job)
            payload["notification_error"] = error
            job.payload_json = json.dumps(payload, sort_keys=True)
            self.db.commit()
        except Exception:
            self.db.rollback()


class _OrderProvisioningState:
    """Resolve the subscription created by the preceding DB phase lazily."""

    def __init__(self, db: Session, order_id: int) -> None:
        self.db = db
        self.order_id = order_id

    def _delegate(self) -> SqlAlchemyProvisioningState:
        subscription_id = self.db.scalar(
            select(Subscription.id).where(Subscription.order_id == self.order_id)
        )
        if subscription_id is None:
            raise ValueError("Provisioning subscription was not created")
        return SqlAlchemyProvisioningState(self.db, subscription_id)

    def reject_capacity(self, order_id: str, reason: str) -> None:
        del order_id, reason

    def allocate_endpoint(self, customer_id: str) -> EgressEndpointDTO:
        return self._delegate().allocate_endpoint(customer_id)

    def release_endpoint(self, endpoint_id: str) -> None:
        self._delegate().release_endpoint(endpoint_id)

    def save_credentials(
        self, tenant: TenantDTO, endpoint: EgressEndpointDTO, credential: CredentialDTO
    ) -> str:
        return self._delegate().save_credentials(tenant, endpoint, credential)

    def rollback_database(self) -> None:
        self.db.rollback()

    @contextmanager
    def gateway_route_binding_lock(self) -> Iterator[None]:
        with gateway_route_binding_write(self.db):
            yield

    def current_forwarder_state(self) -> DesiredForwarderState:
        return self._delegate().current_forwarder_state()

    def desired_forwarder_state(
        self, endpoint: EgressEndpointDTO, tenant: TenantDTO, secret_ref: str
    ) -> DesiredForwarderState:
        return self._delegate().desired_forwarder_state(endpoint, tenant, secret_ref)

    def desired_routing_state(
        self,
        request: ProvisionRequest,
        endpoint: EgressEndpointDTO,
        tenant: TenantDTO,
        routing_principal: str,
    ) -> DesiredRoutingState:
        return self._delegate().desired_routing_state(
            request, endpoint, tenant, routing_principal
        )

    def store_subscription_token(self, customer_id: str, raw_token: str) -> None:
        self._delegate().store_subscription_token(customer_id, raw_token)

def _admin_metrics(db: Session) -> dict[str, object]:
    latest_gateway = db.scalar(select(GatewayMetric).order_by(GatewayMetric.collected_at.desc()))
    providers = list(
        db.scalars(select(TransportProviderRecord).order_by(TransportProviderRecord.code))
    )

    def provider_level(provider: TransportProviderRecord) -> str:
        if provider.quota_bytes is None or provider.used_bytes is None:
            return "UNKNOWN"
        percent = (
            (provider.used_bytes * 100) // provider.quota_bytes
            if provider.quota_bytes
            else 100
        )
        if percent >= 95:
            return "EMERGENCY"
        if percent >= 80:
            return "PROCURE"
        if percent >= 60:
            return "REMINDER"
        return "OK"

    return {
        "total_users": db.scalar(select(func.count()).select_from(Customer)) or 0,
        "active_subscriptions": db.scalar(
            select(func.count()).select_from(Subscription).where(
                Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE])
            )
        )
        or 0,
        "pending_orders": db.scalar(
            select(func.count()).select_from(Order).where(Order.status == OrderStatus.PENDING)
        )
        or 0,
        "open_tickets": db.scalar(
            select(func.count()).select_from(Ticket).where(Ticket.status == "OPEN")
        )
        or 0,
        "providers": [
            {
                "code": item.code,
                "status": item.status.value,
                "enabled": item.enabled,
                "quota_bytes": item.quota_bytes,
                "used_bytes": item.used_bytes,
                "remaining_bytes": (
                    max(0, item.quota_bytes - item.used_bytes)
                    if item.quota_bytes is not None and item.used_bytes is not None
                    else None
                ),
                "measured_at": item.measured_at.isoformat() if item.measured_at else None,
                "capacity_level": provider_level(item),
            }
            for item in providers
        ],
        "gateway": None
        if latest_gateway is None
        else {
            "code": latest_gateway.gateway_code,
            "cpu_percent": latest_gateway.cpu_percent,
            "ram_percent": latest_gateway.ram_percent,
            "disk_percent": latest_gateway.disk_percent,
            "collected_at": latest_gateway.collected_at.isoformat(),
        },
        "generated_at": datetime.now(UTC).isoformat(),
    }


@router.get("/admin/orders")
def admin_orders(db: DbSession, _: AdminCustomer) -> list[dict[str, object]]:
    orders = db.execute(
        select(Order, Customer, Plan)
        .join(Customer, Customer.id == Order.customer_id)
        .join(Plan, Plan.id == Order.plan_id)
        .where(Order.status.in_([OrderStatus.PENDING, OrderStatus.PAID]))
        .order_by(Order.id.desc())
    ).all()
    notice_rows = db.scalars(
        select(AuditLog)
        .where(
            AuditLog.action == "ORDER_PAYMENT_NOTICE",
            AuditLog.entity_type == "ORDER",
            AuditLog.result == "SUCCESS",
        )
        .order_by(AuditLog.id.desc())
    ).all()
    notices: dict[str, AuditLog] = {}
    for row in notice_rows:
        notices.setdefault(row.entity_id, row)
    return [
        {
            "id": order.id,
            "order_no": order.order_no,
            "customer_email": customer.email,
            "plan_code": plan.plan_code,
            "plan_name": plan.name,
            "traffic_limit_bytes": plan.traffic_limit_bytes,
            "amount": str(order.amount),
            "currency": order.currency,
            "status": order.status.value,
            "payment_status": order.payment_status.value,
            "created_at": order.created_at.isoformat(),
            "payment_notice_at": notices[str(order.id)].created_at.isoformat()
            if str(order.id) in notices
            else None,
        }
        for order, customer, plan in orders
    ]


@router.get("/admin/customers")
def admin_customers(db: DbSession, _: AdminCustomer) -> list[dict[str, object]]:
    customers = list(db.scalars(select(Customer).order_by(Customer.id.desc())))
    subscriptions = list(db.scalars(select(Subscription).order_by(Subscription.id.desc())))
    current_by_customer: dict[int, Subscription] = {}
    for subscription in subscriptions:
        current_by_customer.setdefault(subscription.customer_id, subscription)
    active_bindings = db.execute(
        select(EgressBinding, EgressEndpoint)
        .join(EgressEndpoint, EgressEndpoint.id == EgressBinding.egress_id)
        .where(EgressBinding.released_at.is_(None))
    ).all()
    egress_by_subscription = {
        binding.subscription_id: endpoint for binding, endpoint in active_bindings
    }
    webshare_by_subscription = {
        row.subscription_id: row for row in db.scalars(select(WebshareSubuser)).all()
    }
    result: list[dict[str, object]] = []
    for customer in customers:
        current_subscription = current_by_customer.get(customer.id)
        period = current_subscription.current_period if current_subscription else None
        egress = (
            egress_by_subscription.get(current_subscription.id)
            if current_subscription
            else None
        )
        webshare = (
            webshare_by_subscription.get(current_subscription.id)
            if current_subscription
            else None
        )
        result.append(
            {
                "id": customer.id,
                "customer_no": customer.customer_no,
                "email": customer.email,
                "status": customer.status.value,
                "subscription_status": (
                    current_subscription.status.value if current_subscription else None
                ),
                "subscription_no": (
                    current_subscription.subscription_no if current_subscription else None
                ),
                "plan_id": current_subscription.plan_id if current_subscription else None,
                "egress_ip": egress.host if egress else None,
                "egress_code": egress.code if egress else None,
                "egress_location": egress.location if egress else None,
                "egress_isp": egress.isp if egress else None,
                "egress_ip_type": egress.ip_type if egress else None,
                "used_bytes": period.used_bytes if period else 0,
                "quota_bytes": period.quota_bytes if period else 0,
                "webshare_proxy_limit_gb": str(webshare.proxy_limit_gb) if webshare else None,
                "service_expire_at": (
                    current_subscription.service_expire_at.isoformat()
                    if current_subscription
                    else None
                ),
                "last_usage_synced_at": current_subscription.last_usage_synced_at.isoformat()
                if current_subscription and current_subscription.last_usage_synced_at
                else None,
            }
        )
    return result


@router.get("/admin/egress")
def admin_egress(db: DbSession, _: AdminCustomer) -> list[dict[str, object]]:
    rows = db.execute(
        select(EgressEndpoint, EgressBinding, Subscription, Customer)
        .where(EgressEndpoint.status.in_(["AVAILABLE", "ASSIGNED"]))
        .outerjoin(
            EgressBinding,
            and_(
                EgressBinding.egress_id == EgressEndpoint.id,
                EgressBinding.released_at.is_(None),
            ),
        )
        .outerjoin(Subscription, Subscription.id == EgressBinding.subscription_id)
        .outerjoin(Customer, Customer.id == Subscription.customer_id)
        .order_by(EgressEndpoint.id)
    ).all()
    return [
        {
            "id": endpoint.id,
            "code": endpoint.code,
            "ip": endpoint.host,
            "port": endpoint.port,
            "location": endpoint.location,
            "isp": endpoint.isp,
            "ip_type": endpoint.ip_type,
            "status": endpoint.status,
            "capacity": endpoint.capacity,
            "current_count": endpoint.current_count,
            "listen_port": endpoint.mihomo_listen_port,
            "customer_email": customer.email if customer else None,
            "subscription_no": subscription.subscription_no if subscription else None,
            "assigned_at": binding.assigned_at.isoformat() if binding else None,
        }
        for endpoint, binding, subscription, customer in rows
    ]


@router.get("/admin/capacity")
def admin_capacity(db: DbSession, _: AdminCustomer) -> dict[str, object]:
    settings = get_settings()
    allocated = db.scalar(select(func.coalesce(func.sum(WebshareSubuser.proxy_limit_gb), 0)))
    allocated_gb = float(allocated or 0)
    total_gb = float(settings.webshare_total_quota_gb)
    reserve_gb = float(settings.webshare_ops_reserve_gb)
    remaining_gb = max(0.0, total_gb - allocated_gb - reserve_gb)
    available_ips = db.scalar(
        select(func.count())
        .select_from(EgressEndpoint)
        .where(
            EgressEndpoint.status == "AVAILABLE",
            EgressEndpoint.purpose == "PRODUCTION",
            EgressEndpoint.capacity == 1,
            EgressEndpoint.current_count == 0,
        )
    ) or 0
    plans = list(
        db.scalars(
            select(Plan)
                .where(
                    Plan.status == PlanStatus.ACTIVE,
                    Plan.plan_code.in_(ADMIN_CAPACITY_PLAN_CODES),
                )
            .order_by(Plan.id)
        )
    )
    sellable = {
        plan.plan_code: min(
            int(remaining_gb // (plan.traffic_limit_bytes / (1024**3))), int(available_ips)
        )
        for plan in plans
    }
    plan_sizes = [plan.traffic_limit_bytes / (1024**3) for plan in plans]
    if remaining_gb <= 0:
        capacity_level = "EXHAUSTED"
    elif remaining_gb < min(plan_sizes, default=50):
        capacity_level = "LOW"
    elif remaining_gb <= total_gb * 0.2:
        capacity_level = "WATCH"
    else:
        capacity_level = "OK"
    return {
        "total_quota_gb": total_gb,
        "allocated_gb": allocated_gb,
        "ops_reserve_gb": reserve_gb,
        "remaining_gb": remaining_gb,
        "available_ip_count": int(available_ips),
        "capacity_level": capacity_level,
        "sellable_by_plan": sellable,
    }


@router.get("/admin/metrics")
def admin_metrics(db: DbSession, _: AdminCustomer) -> dict[str, object]:
    return _admin_metrics(db)


@router.post("/admin/orders/{order_id}/confirm-payment", response_model=ProvisionResult)
def admin_confirm_payment(
    order_id: int,
    data: PaymentConfirmationRequest,
    db: DbSession,
    _: AdminCustomer,
) -> ProvisionResult:
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    try:
        order_type = BillingOrderType(str(order.order_type))
        if order_type is not BillingOrderType.PURCHASE:
            raise ValueError("Only purchase orders are handled by provisioning")
        plan = db.get(Plan, order.plan_id)
        if plan is None:
            raise ValueError("Order plan is not available")
        command = BillingCommand(
            order_id=str(order.id),
            subscription_id=None,
            order_type=order_type,
            plan_id=str(plan.id),
        )
        request = ProvisionRequest(
            order_id=str(order.id),
            customer_id=str(order.customer_id),
            username=f"sub-{order.id}",
            quota_gb=Decimal(plan.traffic_limit_bytes) / Decimal(1024**3),
            thread_limit=1,
            expire_at=datetime.now(UTC) + timedelta(days=plan.duration_days),
            subscription_domain=get_settings().subscription_domain
            or get_settings().subscription_base_url,
        )
        state = _OrderProvisioningState(db, order_id)
        runs = _SqlAlchemyProvisionRuns(db)
        try:
            outcome = confirm_payment_and_provision(
                command,
                request,
                order,
                state,
                runs,
                _SqlAlchemyOrderState(db),
                data.payment_reference,
            )
        finally:
            runs.close()
        subscription = db.scalar(select(Subscription).where(Subscription.order_id == order.id))
        if outcome is None or subscription is None:
            raise RuntimeError("Provisioning did not return a purchase subscription")
        if subscription.accounting_user_id is None:
            subscription.accounting_user_id = request.username
            db.commit()
        db.refresh(order)
        db.refresh(subscription)
        return ProvisionResult.model_validate(
            {
                "order": order,
                "subscription": subscription,
                "subscription_url": outcome.subscription_url,
            }
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    except CapacityExceededError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from None


@router.get("/admin/accounting/health")
def admin_accounting_health(_: AdminCustomer) -> dict[str, str]:
    healthy = build_registry(get_settings()).transport.health_check()
    return {"status": "ok" if healthy else "unavailable"}


@router.post("/admin/subscriptions/{subscription_id}/sync-usage", response_model=SubscriptionRead)
def admin_sync_usage(subscription_id: int, db: DbSession, _: AdminCustomer) -> Subscription:
    subscription = db.get(Subscription, subscription_id)
    if subscription is None:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if not subscription.accounting_user_id:
        raise HTTPException(status_code=409, detail="Subscription has no accounting identity")
    try:
        usage = build_registry(get_settings()).accounting.get_usage(subscription.accounting_user_id)
        period = subscription.current_period
        if period is None:
            raise ValueError("Subscription has no active usage period")
        period.used_bytes = usage.used_bytes
        subscription.last_usage_synced_at = usage.measured_at
        db.commit()
        db.refresh(subscription)
        return subscription
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail="Accounting sync failed") from exc
